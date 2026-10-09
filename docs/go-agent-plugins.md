# Go agent PE plugin system

The Go agent supports PE plugins as separate child processes. The agent fetches
the DLL over the authenticated beacon channel, sends the bytes to a short-lived
child over stdin, and maps it there. The plugin is never mapped in the beacon
process, which keeps the agent small and isolates plugin crashes. Nothing is
written to the target disk. Authorized lab use only.

## Overview

```text
Operator drops plugin DLL into RMM_logs/plugins/
        │
        ▼
GET  /api/v1/plugins              (list: name, size, SHA-256)
POST /api/v1/sessions/{id}/pe     (queue __PE_LOAD__ <plugin> <export> [input])
        │
        ▼
agent-go: execute() dispatch  ──  __PE_LOAD__ handler
        ├── fetch  GET /tools/plugins/<name>  (beacon auth)
        ├── child  agent.exe --plugin-child <export>
        └── frame  DLL bytes + input over stdin
```

Plugin images are downloaded and started per command. The parent does not cache
the DLL or load it into its own address space.

## How loading works

The runner lives in `agent-go/pe_loader_windows.go` and uses only the Go
standard library:

1. **Fetch** — the plugin bytes are downloaded through the same authenticated
   `request()` helper as every other beacon call (`X-RMM-Beacon-Token` header)
   request helper as every other beacon call.
2. **Run** — the parent starts itself with `--plugin-child` and frames the DLL
   bytes and input over stdin.
3. **Map** — the child uses the existing NT section mapper and invokes the
   requested export.
4. **Cleanup** — the parent waits up to ten minutes and returns stdout as the
   plugin result. No plugin file is created.

Discretion properties:

- No file is written to the target disk at any point.
- Code pages are **never writable and executable at the same time**.
- The mapped region is reported as `MEM_IMAGE` (a normal mapped module),
  not private `VirtualAlloc` memory.
- After loading, the writable staging alias is dropped; only the RX image
  view remains.

## Plugin ABI

Plugins export a function with this signature:

```c
int Run(const char *input, char *output, int outputCap);
```

- Return `0` on success; any other value is reported as a plugin error code.
- `input` is the optional operator-provided string (NUL-terminated, 4096
  bytes max from the API).
- Write a NUL-terminated result string into `output` (up to 1 MiB); it is
  posted back as a normal `output` result event.
- Exports with fewer parameters also work: the x64 calling convention
  ignores extra arguments, so `int Run(void)` is valid.

Minimal example plugin:

```c
#include <stdio.h>
#include <string.h>
#include <windows.h>

int Run(const char *input, char *output, int outputCap) {
    if (!DllMain) {}
    _snprintf(output, outputCap, "hello from plugin, input was: %s",
              input ? input : "(none)");
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance; (void)reason; (void)reserved;
    return TRUE;
}
```

Build (x64, relocations included by default with `/DYNAMICBASE`):

```bash
cl /LD /O2 plugin.c /Fe:hello.dll
clang -shared -o hello.dll plugin.c
```

## Constraints and limitations

| Limitation | Detail |
|------------|--------|
| Architecture | Only PE32+ amd64 images; other plugins fail with an explicit error |
| Relocations | Standard Windows DLL relocations are handled by `LoadLibrary` |
| Loader registration | The child uses the normal Windows loader, so standard module lookup and DLL initialization are available |
| Exceptions | Plugin language runtimes may use their normal Windows loader requirements inside the child |
| CFG | Follow the normal compiler and loader requirements for the plugin toolchain |
| Dependencies | DLL dependencies must be available to the child through the normal Windows loader search rules |
| Isolation | A crashing plugin terminates only the child runner; the beacon process remains alive |
| Blocking | The parent waits for the child for up to ten minutes; the beacon poll cycle is still occupied during that command |
| .NET | Managed assemblies are out of scope; build AOT native binaries instead |

Test plugins in the lab before queuing them against live sessions.

## Operator surfaces

Full parity (REST → CLI → MCP), see `docs/mcp-parity.md`:

| Surface | Usage |
|---------|-------|
| REST | `GET /api/v1/plugins` (inventory, build targets, toolchain status), `POST /api/v1/plugins/build`, `POST /api/v1/sessions/{id}/pe` with `{"plugin":"hello.dll","export":"Run","input":"…"}` |
| CLI | `rmm_cli.py pe list`, `rmm_cli.py pe build <plugin> --output <name.dll>`, `rmm_cli.py pe load hello.dll --export Run --session <id> [input]` |
| CLI (interactive) | `pe list`, `pe build <plugin> [output.dll]`, `pe <plugin> [export] [input]` |
| MCP | `list_plugins`, `build_plugin`, `queue_pe_load(session_ref, plugin, export, input)` |
| Web AI | Same tools via `rmm_tools.py` (`TOOL_HANDLERS` / `OPENAI_TOOLS`) |
| Web UI | **Deploy plugins (Go)** panel (inventory + server-side build), shell verb `pe <plugin> [export] [input]` |

Plugin output is returned through the normal `output` result event; poll
`GET /api/v1/sessions/{id}/events` or the `events` CLI command.

## Server-side builds

The web UI and CLI compile **only checked-in plugin sources** from
`agent-plugins/<name>/` (one directory per plugin, must contain `go.mod`):

- `POST /api/v1/plugins/build` with `{"name":"<plugin>","output":"<name.dll>"}`
  cross-compiles windows/amd64 with CGO (`-buildmode=c-shared`,
  `-trimpath`, `-ldflags "-s -w"`) and writes the DLL directly into the
  plugin directory, ready to serve. The output name is free — naming is an
  operator choice.
- The server requires the Go toolchain and a mingw-w64 cross compiler
  (`x86_64-w64-mingw32-gcc` by default; override with `RMM_PLUGIN_CC`).
  `GET /api/v1/plugins` reports `toolchain` availability and the buildable
  `targets` so the UI can disable builds when the compiler is missing.
- `RMM_PLUGIN_BUILD_TIMEOUT` (default 600 seconds) bounds each build;
  Go build caches live under `RMM_logs/` so warm rebuilds are fast.
- The build never compiles browser- or API-supplied code; adding a new
  plugin means adding its source directory to the repository.

## Server plugin directory

- Plugins are read from `RMM_logs/plugins/` (override with `RMM_PLUGINS_DIR`).
- Names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; anything else is
  rejected by both the API and the serving path.
- `GET /api/v1/plugins` returns name, size, and SHA-256 for every plugin.
- Agents download plugins from `/tools/plugins/<name>` with the beacon
  secret and a known session ID, mirroring the rclone bootstrap endpoint.
- `POST /api/v1/sessions/{id}/pe` validates the plugin name, export name
  (`^[A-Za-z_][A-Za-z0-9_]{0,127}$`), and input (single line, 4096 bytes)
  before queuing `__PE_LOAD__ …` as a oneshot command.

## Function reference

New Go functions in `agent-go/`:

| Function | File | Role |
|----------|------|------|
| `loadPluginPE` | `pe_loader_windows.go` | Fetch, stage, run, and clean up a plugin child |
| `runPluginChild` | `pe_loader_windows.go` | Load a DLL with the Windows loader and invoke its export |
| `loadPluginCommand` | `main.go` | Parse the `__PE_LOAD__` operator command |
| `loadPluginPE` (stub) | `platform_other.go` | Non-Windows builds return an explicit error |

New Python functions:

| Function | File | Role |
|----------|------|------|
| `plugin_directory` | `server_rmm.py` | Return (and create) the plugin directory |
| `list_plugin_files` | `server_rmm.py` | List plugins with size and SHA-256 |
| `plugin_build_targets` | `server_rmm.py` | List checked-in Go plugin directories |
| `plugin_toolchain` | `server_rmm.py` | Report Go and mingw cross-compiler availability |
| `build_plugin` | `server_rmm.py` | Cross-compile a checked-in plugin into the plugin directory |
| `RMMHandler._serve_plugin_tool` | `server_rmm.py` | Beacon-authenticated plugin download |
| `RmmApiClient.list_plugins` | `rmm_cli.py` | `GET /api/v1/plugins` |
| `RmmApiClient.queue_pe_load` | `rmm_cli.py` | `POST /api/v1/sessions/{id}/pe` |
| `RmmApiClient.build_plugin` | `rmm_cli.py` | `POST /api/v1/plugins/build` |
| `tool_list_plugins` | `rmm_tools.py` | Shared MCP/web-AI tool implementation |
| `tool_queue_pe_load` | `rmm_tools.py` | Shared MCP/web-AI tool implementation |
| `tool_build_plugin` | `rmm_tools.py` | Shared MCP/web-AI tool implementation |

## Security notes

- Plugin downloads require the beacon secret and a registered session ID.
- Plugin bytes are passed through an anonymous pipe and are never written to
  the target disk.
- The child receives only the framed plugin bytes, export name, and JSON input;
  it does not receive beacon credentials.
- The plugin directory is operator-controlled server storage; treat its
  contents like any other operator tooling.
- The API validates plugin names, export names, and input before queueing.
- Server-side builds compile only repository plugin sources
  (`agent-plugins/`); no browser- or API-supplied code is ever built.
- Only Windows amd64 Go agents map plugins; every other agent build returns
  an explicit unsupported error.

## Shipped plugins

| Plugin | Role | Doc |
|--------|------|-----|
| `rclone-exfil` | rclone engine isolated in a plugin child process | `docs/agent-plugin-exfil.md` |
| `screenshot` | Native GDI screenshot capture in a plugin child process | `docs/screenshot-plugin.md` |

The server selects this plugin for `mode=auto` exfil requests only when the
session identifies itself as a Go agent with PE plugin support. Use
`mode=plugin` to require it or `mode=binary` to force the legacy executable
path.

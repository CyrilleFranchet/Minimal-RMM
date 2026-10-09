# Go agent PE plugin system

The Go agent supports **diskless PE plugins**: DLL images that the operator
places on the server, the agent fetches over the authenticated beacon channel
**only when needed**, and the agent maps into its own process memory. Nothing
is ever written to the target disk. This keeps the agent binary small and
moves functionality into operator-controlled plugins. Authorized lab use only.

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
        ├── fetch  GET /tools/plugins/<name>  (in memory, beacon auth)
        ├── map    NtCreateSection + dual view (RW staging, RX execution)
        └── call   DllMain attach, then the requested export
```

Plugin images stay cached in the agent's memory after the first load;
repeated `__PE_LOAD__` commands for the same plugin reuse the mapped image
and skip the download.

## How loading works

The loader lives in `agent-go/pe_loader_windows.go` and uses only the Go
standard library (`debug/pe`, `syscall`, `unsafe`):

1. **Fetch** — the plugin bytes are downloaded through the same authenticated
   `request()` helper as every other beacon call (`X-RMM-Beacon-Token` header)
   and stay in a `[]byte`.
2. **Map** — one NT section object (`NtCreateSection`, `SEC_COMMIT`) is mapped
   twice via `NtMapViewOfSection`: a `PAGE_READWRITE` staging view for writing
   and a `PAGE_EXECUTE_READ` execution view.
3. **Relocate** — `IMAGE_REL_BASED_DIR64` entries are patched when the image
   lands away from its preferred base.
4. **Imports** — the import address table is resolved with `LoadLibraryA` and
   `GetProcAddress`. Only DLLs available on the target resolve.
5. **Protect** — per-section `VirtualProtect` inside the RX view keeps data
   writable but not executable and code executable but not writable.
6. **Attach** — the staging view is unmapped, then `DllMain(base,
   DLL_PROCESS_ATTACH, 0)` runs through `syscall.SyscallN`.
7. **Call** — the requested export is found by walking the mapped image's
   export address table (the Windows loader does not know about manually
   mapped modules, so `GetProcAddress` cannot be used) and invoked.

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
| Relocations | Plugins must keep base relocations (`/DYNAMICBASE`, the default) |
| Loader registration | The image is not in the PEB module list: `GetModuleHandle(self)`, resource lookup by module handle, and implicit TLS (`__declspec(thread)`) do not work |
| Exceptions | C++ exceptions (`throw`) are not safe: unwind data is not registered for manually mapped images (`RtlAddFunctionTable` is a possible future addition) |
| CFG | Do not build plugins with Control Flow Guard expectations |
| Dependencies | Import system DLLs only; `LoadLibraryA` resolves them normally |
| Isolation | A crashing plugin terminates the agent process; in-process loading shares the beacon loop |
| Blocking | `DllMain` and the export run synchronously inside the `/cmd` poll cycle |
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
| `loadPluginPE` | `pe_loader_windows.go` | Fetch (on miss), map, cache, and call a plugin export |
| `mapPluginPE` | `pe_loader_windows.go` | Manually map a PE32+ amd64 image and return the RX base |
| `mapPluginViews` | `pe_loader_windows.go` | Create the NT section object and its RW/RX views |
| `copyPluginImage` | `pe_loader_windows.go` | Copy headers and sections into the staging view |
| `applyPluginRelocations` | `pe_loader_windows.go` | Patch DIR64 base relocations for the mapped base |
| `resolvePluginImports` | `pe_loader_windows.go` | Patch the import address table |
| `applyPluginProtections` | `pe_loader_windows.go` | Tighten per-section page protections |
| `callPluginExport` | `pe_loader_windows.go` | Resolve and invoke the plugin export with the ABI |
| `findPluginExport` | `pe_loader_windows.go` | Walk the mapped image export table |
| `mappedPluginString` | `pe_loader_windows.go` | Read a NUL-terminated string from mapped memory |
| `pluginRawAt` / `pluginRawOffset` / `pluginRawString` | `pe_loader_windows.go` | Raw-file RVA helpers for directories, imports, and relocations |
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
| `rclone-exfil` | In-process rclone engine for diskless exfil | `docs/agent-plugin-exfil.md` |

The server selects this plugin for `mode=auto` exfil requests only when the
session identifies itself as a Go agent with PE plugin support. Use
`mode=plugin` to require it or `mode=binary` to force the legacy executable
path.

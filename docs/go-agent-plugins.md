# Go agent PE plugin system

See the [documentation wiki](README.md) for navigation, the
[architecture overview](architecture.md) for system placement, and
[dynamic plugin architecture](plugin-architecture.md) for the runtime volume
and hot-loading model.

The Go agent supports PE plugins as separate child processes. The agent fetches
the DLL over the authenticated beacon channel, sends the bytes to a child over
stdin, and maps it there. The plugin is never mapped in the beacon process,
which keeps the agent small and isolates plugin crashes. Nothing is written to
the target disk. Authorized lab use only.

## Overview

```text
Operator drops plugin DLL into the persistent plugin volume
        │
        ▼
GET  /api/v1/plugins              (list: name, size, SHA-256)
POST /api/v1/sessions/{id}/pe     (queue __PE_LOAD__ or __PLUGIN_COMMAND__)
        │
        ▼
agent-go: execute() dispatch  ──  generic plugin handler
        ├── fetch  GET /tools/plugins/<name>  (beacon auth)
        ├── child  agent.exe --plugin-child <export>
        └── frame  DLL bytes + input over stdin
```

One-shot plugin images are downloaded and started per command. The parent does
not cache the DLL or load it into its own address space.

## Plugin-owned commands

The agent core exposes a generic PE load operation. Plugin-specific commands
are exports or dispatch cases inside the plugin artifact. They are not added to
`agent-go/main.go`, `server_rmm.py`, or the normal target shell parser.

For a plugin that exports separate commands, the operator selects the export:

```text
pe load network.dll --export Scan --session <id> '{"target":"10.20.0.0/16"}'
```

For a plugin that wants a single stable export, `Run` can receive a JSON
command envelope and dispatch subcommands inside the DLL. In both forms the
agent only knows how to fetch the artifact, load it in the isolated child, call
the selected entry point, and return its output. A new plugin command becomes
available as soon as its artifact is present in the runtime plugin store.

The command namespace form uses the same REST endpoint with a `command` field:

```json
{
  "plugin": "network.dll",
  "command": "scan",
  "input": "{\"target\":\"10.20.0.0/16\"}"
}
```

The server queues `__PLUGIN_COMMAND__ network.dll scan ...`. The agent always
invokes the plugin's `Run` export and passes this envelope:

```json
{
  "command": "scan",
  "input": "{\"target\":\"10.20.0.0/16\"}"
}
```

The server does not contain a `scan` handler. The plugin owns the command
dispatch, input validation, and result format.

## PowerShell script plugins

PowerShell agents use the same authenticated plugin endpoint for deployed
script plugins. The server serves only the approved `socks.ps1` and
`screenshot.ps1` artifacts from the runtime store; the client evaluates the
script in memory and calls its exported function. The plugin source and
release build live in the external plugin repository. The existing SOCKS
plugin keeps its persistent worker in a dedicated runspace.

## Persistent pivot plugin

The SOCKS5 pivot uses the same loader with a long-lived export. When `/cmd`
reports `socks_active: true`, a Windows amd64 Go agent starts
`/tools/plugins/socks.dll` in a cancellable child and passes this private
runtime configuration over stdin:

```json
{
  "base_url": "https://rmm.example",
  "beacon_secret": "…",
  "session_id": "…",
  "http_proxy": ""
}
```

The plugin polls the existing `/socks` endpoint, owns the remote TCP and UDP
connections, and exits when the server clears the relay. The normal beacon
continues polling `/cmd` while the pivot is active. Cancelling the agent also
cancels the child process, and the parent waits for that child to finish before
exiting. The artifact download uses the same cancellation context. This plugin
is started only for the public SOCKS pivot and only while `socks_active` is
true.

The RMM-owned TUN frontend has a separate `tun.dll` transport artifact from
the external plugin repository. When `/cmd` reports `tun_active: true`, the beacon starts that DLL
in a separate cancellable child process. It polls `/tun` and owns the target
TCP and UDP sockets for the server or workspace TUN relay. The TUN device and
user-space IP stack stay with the process that owns the configured storage
mode. The TUN child is not started for a public SOCKS relay, and the SOCKS
child is not started for a TUN relay. Agent shutdown waits for this child after
cancellation.

Both plugins use the same in-memory JSON task framing (`connect`, `send`,
`close`, `udp_send`, and `udp_close`) but have separate binaries and HTTP
endpoints so each pivot loads only the code it needs.

## How loading works

The runner lives in `agent-go/pe_loader_windows.go` and uses only the Go
standard library:

1. **Fetch** — the plugin bytes are downloaded through the same authenticated
   `request()` helper as every other beacon call (`X-RMM-Beacon-Token` header).
   Persistent plugin fetches use a cancellation context so shutdown can abort a
   stalled download.
2. **Run** — the parent starts itself with `--plugin-child` and frames the DLL
   bytes and input over stdin.
3. **Map** — the child manually maps the PE into private allocations, applies
   relocations and imports, sets per-section protections, and invokes the
   requested export.
4. **Cleanup** — the parent waits up to ten minutes and returns stdout as the
   plugin result. No plugin file is created.

Discretion properties:

- No file is written to the target disk at any point.
- Code pages are **never writable and executable at the same time**.
- The image uses private committed memory in the child process and is released
  when that process exits.
- After loading, the writable staging allocation is released; only the
  protected image allocation remains.

## Plugin ABI

Plugins export a function with this signature:

```c
int Run(const char *input, char *output, int outputCap);
```

- Return `0` on success; any other value is reported as a plugin error code.
- `input` is the optional operator-provided string (NUL-terminated, 4096
  bytes max from the API).
- Write a NUL-terminated result string into `output` (up to 16 MiB); it is
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
| Blocking | One-shot plugins can occupy the command cycle for up to ten minutes; the persistent pivot runs in a separate child and does not block beacon polling |
| .NET | Managed assemblies are out of scope; build AOT native binaries instead |

Test plugins in the lab before queuing them against live sessions.

## Operator surfaces

Full parity (REST → CLI → MCP), see `docs/mcp-parity.md`:

| Surface | Usage |
|---------|-------|
| REST | `GET /api/v1/plugins` (runtime inventory), `POST /api/v1/sessions/{id}/pe` with `{"plugin":"hello.dll","export":"Run","input":"…"}` or `{"plugin":"hello.dll","command":"scan","input":"…"}` |
| CLI | `rmm_cli.py pe list`, `rmm_cli.py pe load hello.dll --export Run --session <id> [input]`, or `--command scan` for plugin-owned dispatch |
| CLI (interactive) | `pe list`, `pe <plugin> [export] [input]` |
| MCP | `list_plugins`, `queue_pe_load(session_ref, plugin, export, input, command)` |
| Web AI | Same tools via `rmm_tools.py` (`TOOL_HANDLERS` / `OPENAI_TOOLS`) |
| Web UI | Runtime plugin inventory through the API; shell verb `pe <plugin> [export] [input]` |

Plugin output is returned through the normal `output` result event; poll
`GET /api/v1/sessions/{id}/events` or the `events` CLI command.

The Docker control plane keeps the delivery endpoint in the image, while the
active plugin store is a persistent volume populated from the external plugin
repository. See
[`plugin-architecture.md`](plugin-architecture.md) for volume deployment and
the plugin command boundary.

Plugin lifecycle logging is server-side. The RMM server logs queueing,
authenticated plugin delivery, command dispatch, and completion or failure with
the session, DLL, export, result type, output size, and elapsed time. Session
history retains the operator queue and result events. The Go agent does not log
plugin fetch, mapping, execution, or result details to its target-side log, and
plugin input and output contents are not written to the lifecycle log.

## Server plugin directory

- Plugins are read from the runtime plugin store (Docker uses `/app/plugins`;
  local server mode defaults to `RMM_logs/plugins/`, overridden with
  `RMM_PLUGINS_DIR`).
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
| `loadPluginPEContext` | `pe_loader_windows.go` | Run a plugin child with caller-owned cancellation |
| `runPluginChild` | `pe_loader_windows.go` | Load a DLL with the Windows loader and invoke its export |
| `loadPluginCommand` | `main.go` | Parse legacy export loads and plugin-owned command loads |
| `pluginCommandEnvelope` | `main.go` | Wrap a plugin command and opaque input for the plugin `Run` export |
| `pluginController` / `newPluginController` | `main.go` | Start and cancel one persistent SOCKS or TUN child independently |
| `loadPluginPE` (stub) | `platform_other.go` | Non-Windows builds return an explicit error |

New Python functions:

| Function | File | Role |
|----------|------|------|
| `plugin_directory` | `server_rmm.py` | Return (and create) the plugin directory |
| `plugin_script_path` | `server_rmm.py` | Resolve deployed PowerShell script plugins |
| `list_plugin_files` | `server_rmm.py` | List plugins with size and SHA-256 |
| `plugin_command_metadata` | `server_rmm.py` | Extract safe plugin identity for server-side lifecycle logs |
| `RMMHandler._serve_plugin_tool` | `server_rmm.py` | Beacon-authenticated plugin download |
| `RmmApiClient.list_plugins` | `rmm_cli.py` | `GET /api/v1/plugins` |
| `RmmApiClient.queue_pe_load` | `rmm_cli.py` | `POST /api/v1/sessions/{id}/pe` |
| `RmmApiClient.start_tun` / `stop_tun` / `list_tun` | `rmm_cli.py` | TUN lifecycle REST methods; device ownership follows storage mode |
| `tool_list_plugins` | `rmm_tools.py` | Shared MCP/web-AI tool implementation |
| `tool_queue_pe_load` | `rmm_tools.py` | Shared MCP/web-AI tool implementation |
| `tool_start_tun` / `tool_stop_tun` / `tool_list_tun` | `rmm_tools.py` | Shared TUN operator tools; device ownership follows storage mode |

## Security notes

- Plugin downloads require the beacon secret and a registered session ID.
- Plugin bytes are passed through an anonymous pipe and are never written to
  the target disk.
- One-shot plugin input is framed over stdin. The persistent pivot plugin also
  receives the beacon URL, session ID, and secret because it owns the
  authenticated `/socks` channel; these values stay in memory and are not
  logged or written to disk.
- The plugin directory is operator-controlled server storage; treat its
  contents like any other operator tooling.
- The API validates plugin names, export names, plugin command names, and input
  before queueing. It does not implement plugin-specific commands.
- Agent plugin builds happen in the external plugin repository; the server
  only inventories and serves deployed artifacts.
- Only Windows amd64 Go agents map plugins; every other agent build returns
  an explicit unsupported error.

## Shipped Go plugins

| Plugin | Role | Doc |
|--------|------|-----|
| `rclone-exfil` | rclone engine isolated in a plugin child process | [External plugin repository](plugin-repository.md) |
| `screenshot` | Native GDI screenshot capture in a plugin child process | [External plugin repository](plugin-repository.md) |
| `socks` | Persistent SOCKS5 pivot channel for Windows amd64 Go agents | this document |
| `tun` | Persistent TUN TCP/UDP transport for Windows amd64 Go agents | this document |

## Shipped PowerShell script plugins

| Plugin | Role | Doc |
|--------|------|-----|
| `screenshot.ps1` | Primary display capture for PowerShell agents | [External plugin repository](plugin-repository.md) |
| `socks.ps1` | Persistent SOCKS5 pivot channel for PowerShell agents | `docs/web-socks.md` |

The server selects this plugin for `mode=auto` exfil requests only when the
session identifies itself as a Go agent with PE plugin support. Use
`mode=plugin` to require it or `mode=binary` to force the legacy executable
path.

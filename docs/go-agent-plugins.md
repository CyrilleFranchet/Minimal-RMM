# Go agent PE plugin system

See the [documentation wiki](README.md) for navigation, the
[architecture overview](architecture.md) for system placement, and
[dynamic plugin architecture](plugin-architecture.md) for the runtime volume
and hot-loading model.

The Go agent supports PE plugins as separate child processes. The agent fetches
the DLL over the authenticated beacon channel and inspects its loader marker.
Unmarked DLLs use the existing in-memory mapper. DLLs exporting
`RMMRequiresWindowsLoader` are staged on disk and loaded by Windows in an
isolated child. The plugin never runs in the beacon process. Authorized lab
use only.

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
        ├── unmarked DLL: --plugin-child, DLL bytes + input over stdin
        └── marked DLL: private file → --plugin-library-child, input over stdin
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

The host uses the Go standard library and Windows APIs:

1. Fetch the DLL through the authenticated beacon channel and inspect its PE
   export names without executing code.
2. For an unmarked DLL, send its bytes and input to `--plugin-child`, which
   uses the existing manual mapper.
3. For a DLL exporting `RMMRequiresWindowsLoader`, create a random temporary
   directory with a protected Windows DACL for the process account and SYSTEM.
   Write the DLL, keep it open with read sharing only, and start
   `--plugin-library-child` with its absolute path. Pass plugin input over stdin.
4. The standard-loader child calls `LoadLibraryExW` with
   `LOAD_LIBRARY_SEARCH_SYSTEM32`, then `GetProcAddress` and the plugin export.
   Windows owns CRT, static TLS, worker-thread initialization, and unwind data.
5. The parent waits for the child and removes the staged file and directory.
   This also runs after a native crash, timeout, or cancellation. A forced
   parent termination or machine shutdown can leave the temporary file behind.

The DLL remains loaded until the child exits because an embedded Go runtime
can own background threads. It is not unloaded with `FreeLibrary`. The
standard-loader contract permits only Windows system DLL dependencies. It
requires a writable temporary directory and permission to execute DLLs there.
It does not bypass Windows application control policies.

To select this path, a native plugin exports the marker alongside `Run`:

```cpp
extern "C" __declspec(dllexport) int RMMRequiresWindowsLoader(void) { return 1; }
```

Only the export name is inspected. The marker is not called, and plugin input
is unchanged. REST, CLI, MCP, and WebUI continue to use the same generic PE
command. Operators must use an updated agent before deploying a marked DLL.

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
| Relocations | The manual mapper handles `IMAGE_REL_BASED_DIR64`; other relocation types fail |
| Loader registration | Unmarked images are not registered with Windows; marked DLLs use the standard loader |
| Exceptions / TLS / CFG | Use the Windows-loader marker for MSVC C++ exceptions, static TLS, or runtime worker threads; the manual mapper does not provide these guarantees |
| Dependencies | Marked DLLs must depend only on Windows system DLLs; unmarked imports retain the existing `LoadLibraryA` resolution |
| Isolation | A crashing plugin terminates only the child runner; the beacon process remains alive |
| Blocking | One-shot plugins can occupy the command cycle for up to ten minutes; the persistent pivot runs in a separate child and does not block beacon polling |
| .NET | Managed assemblies are out of scope; build AOT native binaries instead |

Test plugins in the lab before queuing them against live sessions.

### Windows loader validation

`.github/workflows/msvc-plugin-loader.yml` builds a normal MSVC `/MT` DLL with
no exception or TLS shim. `TestWindowsPluginLifecycle` exercises authenticated
download, marker selection, the real agent child, constructors, unwind lookup,
module registration, caught C++ exceptions, and `thread_local` data in a new
`std::thread`. It checks cleanup after success, an error, a missing export,
abrupt native exit, and cancellation after the DLL has loaded.
`TestLegacyPluginLifecycle` covers an unmarked fixture. Both use
`RMM_MSVC_TEST_DLL` and `RMM_MSVC_TEST_AGENT` to locate Windows artifacts.

`TestDocumentEmbeddingsPlugin` additionally uses `RMM_MSVC_TEST_INPUT` for a
pinned public MiniLM request and a quantized BGE-small graph, using mean and
CLS pooling respectively. It validates 384-dimensional normalized vectors
for two documents and a strategic label, isolates a missing document's error,
and checks cleanup after both inference and invalid input. The plugin
repository runs this against its full static ONNX build.
The earlier manual TLS and exception experiments have been removed from the
agent; the standard loader supplies those features for marked DLLs.

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
| `runPluginChild` | `pe_loader_windows.go` | Run an unmarked DLL with the existing manual mapper |
| `invokePluginExport` | `pe_loader_windows.go` | Invoke the shared bounded input/output ABI |
| `pluginRequiresWindowsLoader` | `plugin_format.go` | Inspect the PE export marker without executing DLL code |
| `pluginRVAData` | `plugin_format.go` | Resolve bounded file-backed bytes at an RVA |
| `stageWindowsPlugin` | `plugin_library_windows.go` | Stage the DLL, prevent mutation while running, and return parent-owned cleanup |
| `createPluginDirectory` | `plugin_library_windows.go` | Atomically create a random staging directory with a protected DACL |
| `runWindowsPluginChild` | `plugin_library_windows.go` | Load a staged DLL through Windows, resolve its export, and return output |
| `runWindowsPluginChild` (stub) | `platform_other.go` | Reject the child mode on other operating systems |
| `pluginTestHost` / `assertPluginCleanup` | `pe_loader_windows_test.go` | Exercise authenticated parent/child execution and verify staging cleanup |
| `pluginMarkerFixture` / `TestPluginLoaderMarker` / `FuzzPluginLoaderMarker` | `plugin_format_test.go` | Build inert PE metadata and validate marker parsing against malformed inputs |
| `Initializer` / `RMMRequiresWindowsLoader` / `Run` | `testdata/msvc_plugin.cpp` | Native fixture constructor, marker, and lifecycle test dispatch |
| `Run` | `testdata/legacy_plugin.c` | Return a fixed result from an unmarked legacy fixture |
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
- Unmarked plugin bytes remain in memory. Marked DLLs are staged in a private
  directory until the child exits; model inputs and credentials stay on stdin.
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

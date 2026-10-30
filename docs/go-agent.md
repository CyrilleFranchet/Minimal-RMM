# Go agent

See the [documentation wiki](README.md) and the [architecture overview](architecture.md)
for the agent's place in the control plane and plugin boundaries.

The repository includes a dependency-free Go agent in `agent-go/`. It uses the
same HTTP beacon protocol as `client_rmm.ps1`, so the server does not need a
second registration or command path.

## Web UI workflow

1. Sign in to `/ui/` with the operator API token.
2. Open the separate **Deploy agent (Go)** section.
3. Set the server URL and beacon secret, then choose the target and timing.
4. Click **Compile on server** and download the returned ZIP archive.

The archive contains the target binary, `README.txt`, and a launcher script:
`run-agent.ps1` for Windows or `run-agent.sh` for Linux. UI builds embed the
selected URL and secret; the launcher also applies them to the environment,
replacing stale inherited values. Generic builds with no connection settings
require `RMM_BASE_URL` and `RMM_BEACON_SECRET` to be supplied first. Windows does
not need `screen`; use the PowerShell launcher in a console, Task Scheduler,
or a service wrapper when the agent must remain running after logout.

The source and build endpoints require the operator API token. Builds use the
repository source only, disable CGO, apply `-trimpath`, and write ZIP artifacts
to `RMM_logs/agent-builds/`. The server accepts only the four target pairs
listed above. Each successful build returns its byte count and SHA-256 digest.

## Server-configured builds (AI/CLI autonomy)

`POST /api/v1/agent/go/build` accepts `"use_server_config": true`: when the
request's `config` omits the base URL or beacon secret, the server fills them
from the active beacon transport and the server's own `RMM_BEACON_SECRET`. The
AI tools `build_go_agent` (MCP) and the CLI command `agent go-build` use this
mode, so an operator or the AI assistant can produce a fully configured archive
without handling the secret manually. The build response includes the
portable `artifact_ref` for staging through `queue_upload.local_path`, plus the
usual filename, size, SHA-256, and download URL. The secret is never part of
any response. When no beacon transport is active, the endpoint answers
`no_active_beacon_url` instead of building against a stale URL.

The server must have the Go toolchain installed and available as `go`. The
build request has a 120-second timeout and does not execute shell text supplied
by the browser. The server passes `-buildvcs=false` because source trees
mounted into Exegol or Docker can contain Git metadata that is unavailable or
owned by a different container user; the agent does not need embedded Git
revision metadata.

## Runtime configuration

Nonempty target-host environment variables override embedded build values:

| Variable | Required | Default |
| --- | --- | --- |
| `RMM_BASE_URL` | unless embedded | Build value |
| `RMM_BEACON_SECRET` | unless embedded | Build value |
| `RMM_SESSION_ID` | no | Build value or `go-` plus timestamp |
| `RMM_SLEEP_SECONDS` | no | `60` |
| `RMM_JITTER_PERCENT` | no | `30` |
| `RMM_HTTP_PROXY` | no | direct connection |

The agent registers before each command cycle, polls `/cmd`, executes shell
commands, posts `/result`, and accepts `__CONFIG__`. Downloads are sent in
2 MiB resumable chunks using the server's existing `upload_id` and `offset`
protocol. On Windows it supports `cmd.exe`, `cmd:`, `PS:`, `powershell:`, and
`pwsh:` dispatch. On Linux it uses `/bin/sh` and can use `pwsh` when installed.

## Quick Tunnel connection checks

The generator refreshes transport status before compiling. If its selected
`trycloudflare.com` hostname differs from the active Quick Tunnel, it asks the
operator to select the current beacon URL. That button now refreshes the URL
instead of copying cached state. Custom/named routes are preserved.

The build API's `validate_agent_connection` checks a supplied beacon secret
against the server's active secret before invoking Go. It also rejects a known
stale Quick Tunnel hostname. It never returns either secret. Generic builds
without supplied connection settings remain supported.

`currentBeaconUrl`, `quickTunnelOrigin`, and `useCurrentBeaconUrl` in
`web/go-agent.js` fetch current transport metadata, distinguish Quick Tunnel
origins, and update the form only when requested. Transport errors preserve edits.

For a previously generated agent that cannot connect, inspect its connection
error in `%TEMP%\minimal-rmm-agent.log` (or `RMM_LOG_FILE`):

- `403 FORBIDDEN` from the RMM origin indicates a beacon-secret mismatch. Use
  the active `RMM_BEACON_SECRET`, not the operator or Cloudflare token. Server
  environment/startup flags override saved values; saved changes need a restart.
- `404 Not Found` can indicate that the tunnel points to the administration
  listener instead of the beacon listener.
- DNS, Cloudflare routing errors, and `502` require checking the current public
  URL and connector's origin listener before changing credentials.

An old `RMM_BASE_URL` or `RMM_BEACON_SECRET` on the target overrides a new binary
when it is launched directly. Use the configured archive's launcher or correct
those overrides. Regression coverage exercises registration through a local
HTTPS reverse proxy; it does not claim a live Cloudflare deployment test.

## Command dispatch

The shared router is implemented by `buildCommandInvocation` in
`agent-go/main.go`.

| Input | Windows behavior | Linux behavior |
|-------|------------------|----------------|
| Bare command | Runs with `cmd.exe /d /c`; an exact `ls` is normalized to `dir` | Runs with `/bin/sh -c` |
| `cmd: <command>` | Runs with `cmd.exe /d /c`; an exact `ls` is normalized to `dir` | Returns an unsupported-platform result |
| `PS:` / `powershell:` | Runs with `powershell.exe -NoProfile -NonInteractive -Command` | Returns an unsupported-platform result |
| `pwsh:` | Uses PowerShell 7 when `pwsh.exe` is available, otherwise falls back to `powershell.exe` | Uses `pwsh` when installed |

Prefix matching is case-insensitive and accepts optional whitespace before and
after the colon. A prefix must be followed by a command body, for example
`pwsh: Get-Date` or `PS:Get-Date`.

## Feature parity

The Go agent now implements chunked file transfer, HTTP proxy support, Windows
screenshot capture, Windows keylogging, Windows startup/Run-key persistence,
rclone exfiltration when `rclone` is already available on the target, and
isolated PE plugin loading (`__PE_LOAD__` and plugin-owned
`__PLUGIN_COMMAND__`, see
`docs/go-agent-plugins.md`). SOCKS5 is a persistent pivot plugin on Windows
amd64: the beacon observes `socks_active`, then starts the deployed `socks.dll`
plugin in a cancellable child process. Build that plugin in the external plugin
repository and install it in the runtime plugin store before starting a pivot.
The RMM-owned TUN frontend is a separate persistent plugin: the beacon
observes `tun_active`, then starts `tun.dll` in another cancellable child
process. Build the required plugin in the external plugin repository and
install `socks.dll` or `tun.dll` before starting the corresponding
pivot. The TUN device and user-space IP stack stay in the namespace selected by
the storage mode; the Go agent opens ordinary target-side TCP and UDP sockets
and does not need a TUN device or administrator privileges. See
`docs/tun-pivot.md`.
If rclone is absent, the agent bootstraps it through the authenticated
`/tools/rclone.exe` endpoint. Linux returns explicit unsupported results for
the Windows desktop features and does not advertise the Windows PE pivot
plugins.

The Windows-specific adapters are isolated in `platform_windows.go` and are
selected by Go build tags. They should be tested on representative Windows
versions before production use.

Windows builds use the GUI subsystem, so launching the executable does not
open a console window. Child command windows are also hidden, including
PowerShell commands. Screenshot capture uses native Windows GDI APIs and does
not invoke PowerShell. Startup and connection errors are written to
`%TEMP%\minimal-rmm-agent.log`; set `RMM_LOG_FILE` to choose another path.
Plugin lifecycle events are logged by the RMM server and are never written to
this target-side log.

## Shutdown

The beacon handles SIGINT/SIGTERM through a cancellation context. Shutdown
cancels in-flight HTTP requests, beacon sleeps, directly managed command
processes, and plugin children before closing idle HTTP connections and the
agent log. `closeAgentLog` serializes closure against log writers;
`stopPlatformWorkers` stops and waits for the Windows background worker.
Child-command pipe waits are bounded after cancellation or process exit.

SIGKILL and Windows forced process termination cannot run these defers. There
is no Windows Job Object containment in this implementation: a forcibly killed
parent or grandchildren created by arbitrary commands may leave processes
behind. Windows GUI-subsystem shutdown behavior still needs native testing.
The host regression suite tests SIGTERM during an in-flight registration, and
Windows/Linux cross-builds validate the platform adapters.

### Build artifact isolation

Each build uses a unique server-side binary path and ZIP filename. The executable
name inside the archive remains stable for its launcher. Concurrent builds for
the same target cannot overwrite one another's artifacts or change previously
returned artifact references.

Workspace services forward `agent-builds` and `agent-deploy` artifact downloads
to the authenticated central server, which owns these generated deployment
files. Downloaded session data remains workspace-local. Workspace Go build
requests use a 300-second proxy timeout to accommodate the compiler's
120-second limit and archive generation.

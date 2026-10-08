# Go agent

The repository includes a dependency-free Go agent in `agent-go/`. It uses the
same HTTP beacon protocol as `client_rmm.ps1`, so the server does not need a
second registration or command path.

## Web UI workflow

1. Sign in to `/ui/` with the operator API token.
2. Open the separate **Deploy agent (Go)** section.
3. Load and inspect the checked-in source.
4. Set the server URL and beacon secret, then choose the target and timing.
5. Click **Compile on server** and download the returned ZIP archive.

The archive contains the target binary, `README.txt`, and a launcher script:
`run-agent.ps1` for Windows or `run-agent.sh` for Linux. Set
`RMM_BASE_URL` and `RMM_BEACON_SECRET`, then run the launcher. Windows does
not need `screen`; use the PowerShell launcher in a console, Task Scheduler,
or a service wrapper when the agent must remain running after logout.

The source and build endpoints require the operator API token. Builds use the
repository source only, disable CGO, apply `-trimpath`, and write ZIP artifacts
to `RMM_logs/agent-builds/`. The server accepts only the four target pairs
listed above. Each successful build returns its byte count and SHA-256 digest.

The server must have the Go toolchain installed and available as `go`. The
build request has a 120-second timeout and does not execute shell text supplied
by the browser.

## Runtime configuration

The Go binary reads these environment variables on the target host:

| Variable | Required | Default |
| --- | --- | --- |
| `RMM_BASE_URL` | yes | — |
| `RMM_BEACON_SECRET` | yes | — |
| `RMM_SESSION_ID` | no | hostname plus timestamp |
| `RMM_SLEEP_SECONDS` | no | `60` |
| `RMM_JITTER_PERCENT` | no | `30` |
| `RMM_HTTP_PROXY` | no | direct connection |

The agent registers before each command cycle, polls `/cmd`, executes shell
commands, posts `/result`, and accepts `__CONFIG__`. Downloads are sent in
2 MiB resumable chunks using the server's existing `upload_id` and `offset`
protocol. On Windows it supports `cmd.exe`, `PS:`,
`powershell:`, and `pwsh:` dispatch. On Linux it uses `/bin/sh` and can use
`pwsh` when installed.

## Feature parity

The Go agent now implements the HTTP-poll SOCKS worker, chunked file transfer,
HTTP proxy support, Windows screenshot capture, Windows keylogging, Windows
startup/Run-key persistence, and rclone exfiltration when `rclone` is already
available on the target. If rclone is absent, the agent bootstraps it through
the authenticated `/tools/rclone.exe` endpoint. Linux returns explicit
unsupported results for the Windows desktop features. HTTP polling is the
portable SOCKS path; the PowerShell WebSocket transport remains an optional
optimization rather than a requirement.

The Windows-specific adapters are isolated in `platform_windows.go` and are
selected by Go build tags. They should be tested on representative Windows
versions before production use.

Windows builds use the GUI subsystem, so launching the executable does not
open a console window. Startup and connection errors are written to
`%TEMP%\minimal-rmm-agent.log`; set `RMM_LOG_FILE` to choose another path.

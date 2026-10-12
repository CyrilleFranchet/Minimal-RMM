# Web UI — Pivots

The WebUI exposes the existing SOCKS5 operator API as the first pivot type for
the selected live session console.

## Start a relay

1. Select a live session.
2. Click **Pivots** in the session header.
3. Set the listener port and bind host if needed. The defaults are
   `127.0.0.1:1080`.
4. Click **Start SOCKS5 pivot**.
5. Configure tools on the machine running the RMM server with the displayed
   `socks5://host:port` URL.

The connection exits through the selected agent host. The session sidebar
shows the listener port beside sessions with an active relay.

## Agent plugin lifecycle

The beacon carries only the `socks_active` lifecycle flag. The relay code runs
in a plugin so the normal command loop stays small:

- PowerShell agents fetch `agent-plugins/socks-powershell/socks.ps1` through
  the authenticated `/tools/plugins/socks.ps1` endpoint and run it in a
  dedicated worker runspace.
- Windows amd64 Go agents fetch `socks.dll` through the same endpoint and run
  its persistent `Run` export in a cancellable child process. Build it from
  the checked-in `agent-plugins/socks/` source with the plugin build endpoint
  before starting a pivot for a Go session.

The current plugins use the HTTP `/socks` poll path. The server WebSocket
endpoint remains available for compatible future pivot plugins.

The relay keeps the SOCKS protocol and tunnel state on the server. It accepts
fragmented TCP handshake reads, buffers remote bytes that arrive while the
local client is receiving its success reply, caps large send batches at 32
chunks per poll, and retries close tasks until the agent acknowledges them.
Both agent plugins bound target connect time to 20 seconds and retain response
batches across transient HTTP POST failures.

## Manage active relays

The dialog refreshes the global pivot inventory from `GET /api/v1/socks` and
shows the agent, proxy URL, agent channel, active tunnel count, beacon status,
and listener state. The list refreshes periodically while the WebUI is
connected. Use **Copy** to copy a proxy URL or **Stop** to tear down any listed
pivot. **Restart SOCKS5 pivot** applies a changed port or bind host for the
selected session.

The controls call the existing operator endpoints:

- `POST /api/v1/sessions/{id}/socks` with `{"port": 1080, "bind_host": "127.0.0.1"}`
  to start or restart a relay.
- `POST /api/v1/sessions/{id}/socks` with `{"stop": true}` to stop it.

SOCKS carries TCP traffic through the agent and is intended for authorized lab
use. The relay is separate from rclone exfiltration: `rmm_rclone.py` manages
cloud upload profiles and does not configure SOCKS.

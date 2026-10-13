# Web UI — SOCKS and TUN pivots

See the [documentation wiki](README.md) and the
[architecture overview](architecture.md) for system-wide placement and
privilege boundaries.

The WebUI exposes **Shell**, **Files & Screenshots**, **Pivots**, **Downloads**,
and **Screenshots** as tabs in the main workspace. Files and pivots operate on
the selected live session. Downloads and screenshots are global across all
agents. Listener and device ownership follows the storage mode: the central
server owns them in server mode, while the local workspace service owns them in
workspace mode. See
[`docs/tun-pivot.md`](tun-pivot.md) for TUN configuration and lifecycle.

SOCKS5 and TUN are independent relays. Starting or stopping one does not stop
the other, so both can be active for the same agent. The agent has a separate
plugin controller and authenticated transport channel for each relay.

## Start a relay

1. Select a live session.
2. Select the **Pivots** tab in the main workspace.
3. Set the listener port and bind host if needed. The defaults are
   `127.0.0.1:1080`.
4. Click **Start SOCKS5 pivot**.
5. Configure tools in the namespace that owns the listener with the displayed
   `socks5://host:port` URL. In workspace mode, this is the operator's Exegol
   container.

The connection exits through the selected agent host. The session sidebar
shows the listener port beside sessions with an active relay.

The listener implements **SOCKS5 with no authentication** and the TCP
`CONNECT` command for regular operator clients. The TUN frontend also uses
`UDP ASSOCIATE`. Configure clients with `socks5`; SOCKS4 and HTTP
proxy configuration are not supported. For example, the proxy entry in
`proxychains.conf` must use:

```text
socks5 127.0.0.1 1080
```

## Agent plugin lifecycle

The beacon carries only the `socks_active` lifecycle flag. The relay code runs
in a plugin so the normal command loop stays small:

- PowerShell agents fetch the deployed `socks.ps1` artifact through the
  authenticated `/tools/plugins/socks.ps1` endpoint and run it in a dedicated
  worker runspace.
- Windows amd64 Go agents fetch the deployed `socks.dll` artifact through the
  same endpoint and run its persistent `Run` export in a cancellable child
  process. Build it in the external plugin repository before starting a pivot.

The current plugins use the HTTP `/socks` poll path. The server WebSocket
endpoint remains available for compatible future pivot plugins.

The central or workspace relay keeps the SOCKS protocol and tunnel state,
depending on storage mode. In workspace mode the central process retains only
the transport bridge. The relay accepts fragmented TCP handshake reads,
buffers remote bytes that arrive while the local client is receiving its
success reply, caps large send batches at 32 chunks per poll, and retries close
tasks until the agent acknowledges them.
The public SOCKS agent plugins bound target connect time to 20 seconds and
retain response batches across transient HTTP POST failures.

## Manage active relays

The **Pivots** tab refreshes the global pivot inventory from `GET /api/v1/socks` and
shows the agent, proxy URL, agent channel, active tunnel count, beacon status,
and listener state. The list refreshes periodically while the WebUI is
connected. Use **Copy** to copy a proxy URL or **Stop** to tear down any listed
pivot. **Restart SOCKS5 pivot** applies a changed port or bind host for the
selected session.

The controls call the existing operator endpoints:

- `POST /api/v1/sessions/{id}/socks` with `{"port": 1080, "bind_host": "127.0.0.1"}`
  to start or restart a relay.
- `POST /api/v1/sessions/{id}/socks` with `{"stop": true}` to stop it.

The regular operator SOCKS path carries TCP `CONNECT` traffic through the agent.
The TUN frontend uses its private relay's SOCKS5 `UDP ASSOCIATE` support for
datagram flows and its dedicated `/tun` agent channel. The relay is separate
from rclone exfiltration:
`rmm_rclone.py` manages cloud upload profiles and does not configure SOCKS.

## Global downloads and screenshots

The **Downloads** and **Screenshots** tabs are global views in the main
workspace. They list artifacts from all agents in the current server or workspace. Each
row shows the originating agent, a short session ID, whether that session is
active or archived, the artifact name or remote path, size, and received time.
Rows provide authenticated download actions and previews for supported text and
image files.

The lists use:

- `GET /api/v1/artifacts?kind=downloads`
- `GET /api/v1/artifacts?kind=screenshots`

The existing per-session `GET /api/v1/sessions/{id}/downloads` endpoint remains
available for automation and compatibility. Artifact files continue to use
`GET /api/v1/artifacts/{kind}/{filename}`.

The workspace tabs are controlled by `setWorkspaceTab()` in `web/app.js`.
`setGlobalArtifactTab()` changes the Downloads or Screenshots collection while
the selected live session remains available in the sidebar.

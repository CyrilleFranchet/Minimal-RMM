# WebUI connection logging

## Problem

`server_rmm.py` overrides `RMMHandler.log_message()` with a no-op (it
suppresses `BaseHTTPRequestHandler`'s default per-request access log for
every route, beacon and operator alike). Before this change, none of the
operator-facing routes called `server.log(...)` either, so opening the
WebUI, loading its static assets, or connecting the operator event
WebSocket never produced a line in the server log — unlike agent
registration, plugin downloads, or SOCKS events, which already logged
through `RMMServer.log()`.

## What is logged now

### Central server (`server_rmm.py`)

Two call sites in `server_rmm.py` now report through `RMMServer.log()`
(`server_rmm.py:1280`), the same timestamped, leveled logger used
elsewhere in the file:

- **`RMMHandler._serve_web()`** (`server_rmm.py:3540`) logs
  `WebUI connection: <client-ip>` at `INFO` level, once per page load.
  The log call fires only when the resolved static file is `index.html`
  (the WebUI's single-page entry point), not for every CSS/JS/image
  asset the browser fetches afterward — this keeps one log line per
  operator visit instead of one per asset request.
- **`RMMHandler._handle_websocket_upgrade()`** (`server_rmm.py:3331`)
  logs `WebUI event channel connected: <client-ip>` when an operator's
  browser opens the `/api/v1/ws` event feed, and
  `WebUI event channel disconnected: <client-ip>` in the `finally` block
  when that socket closes (page reload/close, operator navigation away,
  or server shutdown).

Both log lines use `self.client_address[0]`, the same source the
`/register` beacon handler already uses for the agent's IP
(`server_rmm.py:5015`).

### Workspace service (`workspace_service.py`)

The per-operator workspace service serves its own WebUI, and its
`WorkspaceRequestHandler` suppresses the stdlib access log the same way
(`log_message()` is a no-op). Before this change, an operator opening
their workspace WebUI produced no line in the workspace service log at
all — only the central server logged WebUI visits.

`WorkspaceRequestHandler._static()` now logs
`WebUI connection: <client-ip>` through `WorkspaceService.log()` once
per `index.html` page load, mirroring the central server's
`_serve_web()` behavior: one line per operator visit, nothing for the
CSS/JS/image assets the browser fetches afterward. The line appears in
the workspace service's stdout, which is the server-side log for the
workspace container (`docker logs` or the Exegol terminal).

## What is intentionally not logged

- Static asset requests other than `index.html` (CSS, JS, images,
  fonts) — logging those would add one line per asset per page load
  with no operator-relevant signal.
- Workspace-scoped event polling (workspaces consume events over the
  authenticated HTTP polling API, not this WebSocket; see the comment
  in `_handle_websocket_upgrade()`).
- Any plugin input, plugin output, URLs, credentials, or operator
  command details — this logging path only reports connection
  lifecycle (IP, connect/disconnect), consistent with the project's
  [logging boundary](../AGENTS.md#logging-boundary).

## Where this fits

See [MCP operator parity](mcp-parity.md) for the general shape of the
operator surface; this is a server-side observability addition and has
no REST/CLI/MCP surface of its own, so it does not require a
`check-parity` entry.

# Tech Plan — Minimal-RMM

Living technical design for planned features. For completed work and parity gaps, see `docs/progress.md`. For API/protocol details, see `README.md`.

**Status key:** `planned` → `in progress` → `done` (update this doc when implementation lands).

---

## 1. Beacon & traffic visualization (web UI)

**Status:** planned  
**Surfaces:** Web UI (primary); optional read-only API for CLI/MCP later  
**Goal:** Give operators a graphical view of data flow and beacon rhythm between each agent and the RMM server.

### Problem

Today the web UI shows `beacon_status` (online / stale / offline) and `last_seen` text in the session sidebar, but there is no time-series view of:

- When the agent polled `/cmd` (check-in cadence vs configured sleep + jitter)
- Bytes sent **agent → server** (results, file uploads, SOCKS WS frames)
- Bytes sent **server → agent** (`/cmd` responses, queued commands, SOCKS tasks)

Operators cannot quickly spot jitter drift, missed beacons, or bursty exfil without reading server logs.

### UX (target)

Add a **Traffic & beacon** panel on the selected session (below shell or in a collapsible `<details>` next to “Files, screenshot & beacon config”):

1. **Beacon timeline** — horizontal axis = last N minutes (default 15, configurable 5–60). Vertical ticks or bars at each `/cmd` poll and each `/result` POST. Color by type: poll (empty queue), poll (command delivered), result posted. Overlay expected interval band from `sleep_seconds` + `jitter_percent`.
2. **Throughput chart** — stacked or dual-line area chart: bytes in vs bytes out per time bucket (e.g. 10 s bins). Separate series optional for SOCKS (`/socks` WS/HTTP) vs main beacon (`/cmd`, `/result`).
3. **Summary strip** — current interval estimate (median gap between polls), last poll ago, totals since session select (or since server start): ↑ sent / ↓ received.

Use a small client-side chart library (e.g. Chart.js or uPlot) loaded only from `/ui/` — no build step required.

```text
┌─ Traffic & beacon ─────────────────────────────────────┐
│  [====●====●=====●========●====]  beacon timeline      │
│  ▲ bytes  ── server→agent   ── agent→server            │
│  Totals: ↑ 2.4 MB   ↓ 890 KB   median poll: 58s        │
└────────────────────────────────────────────────────────┘
```

### Server instrumentation

Extend `Session` in `server_rmm.py` with in-memory metrics (no persistence in v1):

| Field | Updated when |
|-------|----------------|
| `beacon_poll_times` | `deque(maxlen=500)` of ISO timestamps on `GET /cmd` after auth |
| `beacon_result_times` | `POST /result` |
| `bytes_out_beacon` / `bytes_in_beacon` | Request/response body sizes on `/cmd`, `/result` |
| `bytes_out_socks` / `bytes_in_socks` | SOCKS WS frames + HTTP `/socks` bodies |

Hook points (all call `touch_session` today — co-locate counters there or in dedicated `record_beacon_poll` / `record_beacon_result` helpers):

- `handle_cmd` — count outbound command JSON size; record poll timestamp
- `handle_result` — count inbound body; record result timestamp
- `_handle_socks_agent_websocket` / SOCKS HTTP handlers — per-frame byte counts

Optional: emit lightweight WebSocket events (`type: "metrics"`) on each poll so the chart updates live without polling REST.

### Operator API

```http
GET /api/v1/sessions/{id}/metrics?window=900
```

Response (example):

```json
{
  "session_id": "…",
  "sleep_seconds": 60,
  "jitter_percent": 30,
  "beacon_status": "online",
  "last_seen": "2026-06-01T12:00:00",
  "window_seconds": 900,
  "polls": [{"t": "…", "bytes_out": 120, "had_command": false}],
  "results": [{"t": "…", "bytes_in": 4096}],
  "buckets": [
    {"t_start": "…", "bytes_in_beacon": 0, "bytes_out_beacon": 512, "bytes_in_socks": 0, "bytes_out_socks": 0}
  ],
  "totals": {"bytes_in_beacon": 890000, "bytes_out_beacon": 2400000, "bytes_in_socks": 0, "bytes_out_socks": 0}
}
```

Auth: same bearer token as other `/api/v1` routes.

### Web client (`web/app.js`)

- On session select: `GET …/metrics?window=900`, render charts in new DOM section (`web/index.html` + `web/style.css`).
- Subscribe to existing operator WS; on `metrics` events (if implemented), append points without full refetch.
- Fallback: poll metrics every 10 s while panel is open.

### Out of scope (v1)

- Historical metrics across server restarts
- Per-command byte attribution in the chart (events transcript already covers command text)
- Agent-side counters (server-side observation is enough for v1)

### Traffic viz — implementation order

1. Server counters + `GET …/metrics`
2. Static charts in web UI
3. Optional WS push for live updates
4. Document endpoint in `README.md`; add row to parity table in `docs/progress.md`

### Traffic viz — acceptance criteria

- Selecting a session shows beacon poll markers aligned with configured sleep/jitter band
- Uploading a large file via `__DOWNLOAD__` visibly increases agent→server bytes in the chart
- SOCKS traffic appears in socks series when relay is active
- No measurable slowdown on beacon path (O(1) append per request)

---

## 2. Upload to file.io (URL returned by RMM)

**Status:** planned  
**Surfaces:** REST API → `rmm_cli.py` → MCP → Web UI (parity rule)  
**Goal:** Operator queues exfil of a **remote** file on the agent; the agent uploads it to [file.io](https://www.file.io/) and the RMM server returns the public download link to the operator (shell output, events, API response).

### file.io — problem statement

`__DOWNLOAD__` pulls files to `RMM_logs/downloads/` on the server. Operators sometimes want a **shareable ephemeral URL** without storing the file on the RMM host.

### User flow

1. Operator: `fileio upload C:\path\secret.zip` (CLI), web “Upload to file.io” button, or `POST /api/v1/sessions/{id}/fileio`.
2. Server queues internal command for next beacon.
3. Agent reads remote file, `POST multipart/form-data` to `https://file.io` (field `file`).
4. Agent posts result to `/result` with JSON payload containing `link`, `expiry`, `size`, etc.
5. Server surfaces link in event transcript + API wait response; web UI shows clickable link.

### Protocol

New internal command (agent `client_rmm.ps1`):

```text
__FILEIO__ <remote_path> [expires]
```

- `remote_path` — file on agent host (same validation as `__DOWNLOAD__`)
- `expires` — optional file.io query param (`14d`, `1w`, `1m`, …); default server-configured or `14d`

Result `type` (new): `fileio_upload`

```json
{
  "type": "fileio_upload",
  "remote_path": "C:\\Users\\x\\doc.pdf",
  "success": true,
  "link": "https://file.io/AbCd12",
  "key": "AbCd12",
  "expiry": "14 days",
  "size": 12345,
  "error": null
}
```

On failure: `success: false`, `error` message (HTTP status, file missing, size limit).

### Agent implementation (`client_rmm.ps1`)

- Add handler alongside `__DOWNLOAD__` / `__UPLOAD__`.
- Stream file to multipart upload (avoid loading entire file into memory when possible; for v1, chunked read into `MultipartFormData` is acceptable up to agent memory limits).
- Use same HTTP stack as beacon (`Invoke-RmmHttp` / proxy settings) so corporate egress matches existing agent traffic.
- Respect existing verbose logging; do not log file contents.

**Limits:** file.io documents up to ~4 GB; RMM should enforce a configurable max (env `RMM_FILEIO_MAX_BYTES`, default e.g. 100 MB for lab safety) before upload.

### Server (`server_rmm.py`)

- `POST /api/v1/sessions/{id}/fileio` body: `{"remote_path": "…", "expires": "14d"}` → queue `__FILEIO__ …`
- `handle_result`: parse `fileio_upload`, `_record_event` with body containing link; no local artifact file.
- Optional: `GET /api/v1/sessions/{id}/fileio` not needed if exec/wait returns link inline.

### CLI & MCP

| Surface | Action |
|---------|--------|
| `rmm_cli.py` | `fileio <remote_path> [--expires 1w]` subcommand + interactive alias |
| `rmm_tools.py` | `queue_fileio(session_ref, remote_path, expires=None)` |
| `mcp_rmm_server.py` | `queue_fileio` tool |

### Web UI

In tools panel:

- Remote path input + optional expires dropdown
- **Upload to file.io** button
- On success: show link in shell transcript (existing event rendering) + copy button

### Security & ops notes

- file.io is a **third-party** service; files leave the agent host and are stored externally. Document clearly in README (lab-only, data handling).
- No API key required for basic file.io uploads; optional future support for authenticated file.io accounts is out of scope.
- Operator must trust file.io retention (one-time download / auto-delete per their policy).
- Block `__FILEIO__` when server flag `--no-external-upload` (optional hardening) — not required for v1.

### file.io — implementation order

1. Agent `__FILEIO__` + result JSON
2. Server queue + result handling + REST endpoint
3. CLI + MCP
4. Web UI control
5. README + metrics: count file.io bytes in feature 1 counters

### file.io — acceptance criteria

- Operator receives a working `https://file.io/…` link in events within one beacon interval after queueing
- Failed uploads (missing file, file.io down) produce clear error in transcript
- Parity: REST, CLI, MCP, and web can trigger the same command

---

## 3. Downloaded files browser (web UI)

**Status:** planned  
**Surfaces:** Web UI (primary); REST API for listing; optional CLI `downloads list` later  
**Goal:** Operators can browse, open, and save files pulled from the agent (`__DOWNLOAD__`) directly in the web UI — not only via a one-off link buried in the shell transcript.

### Downloads browser — problem statement

When an operator queues `__DOWNLOAD__`, the agent uploads the file to the server (`RMM_logs/downloads/`) and a `file_upload` event appears in the transcript with a **Download file** link (`GET /api/v1/artifacts/downloads/{name}`). That works for the moment the event scrolls by, but:

- There is **no file list** for the selected session (or globally).
- Older downloads are hard to find without re-reading the full event history.
- Screenshots get inline preview; downloaded files do not (except the transcript link).
- No metadata at a glance: original remote path, size, received time.

Operators should manage exfil artifacts from the browser the same way they use the shell — without SSHing to the RMM host or digging in `RMM_logs/`.

### Downloads browser — UX (target) (or a dedicated tab beside the shell):

```text
┌─ Downloads (from agent) ───────────────────────────────┐
│  Remote path              Size      Received    Actions │
│  C:\Users\x\doc.pdf       1.2 MB    2m ago      [↓][👁] │
│  C:\Temp\dump.zip         48 MB     1h ago      [↓]     │
└────────────────────────────────────────────────────────┘
```

Per row:

- **Remote path** — from `file_upload` result / stored metadata (not only the staged filename).
- **Size** — bytes on disk.
- **Received** — when the server finished reassembly.
- **Actions** — **Download** (save via browser), **Preview** (optional v1: images, `.txt`/`.json` under a size cap inline; else download only).

Refresh when a new `file_upload` event arrives on the operator WebSocket (no manual reload).

Optional v2: global **All downloads** view across sessions (filter by hostname).

### Downloads browser — server `RMM_logs/downloads/{hashPrefix}_{originalName}`; served at `GET /api/v1/artifacts/downloads/{basename}` with bearer token (or `?token=` query param for `<a href>`).

**Index on ingest:** when `_save_file_upload` completes a file, append to `session.download_artifacts` (in-memory + optional JSON sidecar under `RMM_logs/` for survival across restart):

```json
{
  "artifact": "a1b2c3d4_report.pdf",
  "remote_path": "C:\\Users\\x\\report.pdf",
  "size": 1258291,
  "received_at": "2026-06-01T12:05:00",
  "artifact_url": "/api/v1/artifacts/downloads/a1b2c3d4_report.pdf"
}
```

**Backfill (v1):** on server start, scan `downloads/` and match `{hashPrefix}_*` to session IDs via existing hash prefix logic (same as artifact naming today).

### Downloads browser — operator API

```json
{
  "session_id": "…",
  "downloads": [
    {
      "artifact": "a1b2c3d4_report.pdf",
      "remote_path": "C:\\Users\\x\\report.pdf",
      "size": 1258291,
      "received_at": "2026-06-01T12:05:00",
      "artifact_url": "/api/v1/artifacts/downloads/a1b2c3d4_report.pdf"
    }
  ]
}
```

Existing `GET /api/v1/artifacts/downloads/{filename}` remains the download endpoint; list entries reference it.

Optional: `DELETE /api/v1/sessions/{id}/downloads/{artifact}` to remove server-side copy (operator cleanup).

### Downloads browser — web client

- On session select: fetch `GET …/downloads`, render table/list in tools panel.
- Reuse `artifactSrc()` for authenticated download/preview URLs.
- On WS `file_upload` event (or any event with `artifact_url` under `downloads/`), prepend row or refetch list.
- Keep transcript link behavior for backward compatibility; list is the durable entry point.

### CLI & MCP (optional, post-v1)

| Surface | Action |
|---------|--------|
| `rmm_cli.py` | `downloads list [session]` — table of staged files + paths |
| `rmm_tools.py` | `list_downloads(session_ref)` |
| `mcp_rmm_server.py` | `list_downloads` tool |

Not required for first ship if web UI is the primary ask; REST list endpoint is enough for automation.

### Downloads browser — security notes; artifact URLs continue to require token.
- List endpoint must only return artifacts whose stored `hashPrefix` maps to the requested session (no cross-session leakage).
- Preview only for allowlisted MIME/types and max size (e.g. 1 MB text, images) to avoid loading huge binaries into the DOM.

### Downloads browser — implementation order

1. Track `download_artifacts` on `file_upload` complete + `GET …/downloads`
2. Web UI list + download buttons wired to existing artifact endpoint
3. WS-driven refresh on new `file_upload`
4. Optional preview + delete; CLI/MCP list if needed
5. Document in `README.md` and parity table in `docs/progress.md`

### Downloads browser — acceptance criteria

- After `__DOWNLOAD__` completes, the file appears in the session Downloads panel without a full page reload
- Clicking **Download** saves the correct file via the browser
- List shows remote path and size, not only the hashed server filename
- Sessions cannot see another session’s downloads

---

## Backlog (referenced elsewhere)

Items tracked in `docs/progress.md` **Up Next** but not spec’d here yet:

- Web UI SOCKS controls + global relay list
- Chunked upload (symmetry with chunked download)
- CLI subcommands for screenshot / SOCKS
- Automated tests
- `docs/prd.md`

When picking up a backlog item, add a numbered section here before implementation.

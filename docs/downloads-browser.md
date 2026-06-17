# Downloaded files browser (web UI)

Operators can browse files pulled from agents (`__DOWNLOAD__`) in the web console without digging through the shell transcript or `RMM_logs/downloads/` on disk.

## Surfaces

| Surface | Support |
|---------|---------|
| Web UI | **Downloads from agent** panel (per session) |
| REST API | `GET /api/v1/sessions/{id}/downloads` |
| CLI / MCP | Not in v1 (use events or list API) |

## User flow

1. Queue a download (web **Download** field, CLI, or API `POST …/download`).
2. Agent uploads chunks via `file_upload` result; server reassembles under `RMM_logs/downloads/{hashPrefix}_{basename}`.
3. Server indexes the file on `session.download_artifacts`.
4. Web UI lists remote path, size, received time; **Download** and optional **Preview** (text ≤ 1 MB, images).
5. On new `file_upload` events (WebSocket or poll), the list refreshes automatically.

## REST API

```http
GET /api/v1/sessions/{id}/downloads
Authorization: Bearer $RMM_API_TOKEN
```

Response:

```json
{
  "session_id": "…",
  "downloads": [
    {
      "artifact": "a1b2c3d4e5f6_report.pdf",
      "remote_path": "C:\\Users\\x\\report.pdf",
      "size": 1258291,
      "received_at": "2026-06-01T12:05:00",
      "artifact_url": "/api/v1/artifacts/downloads/a1b2c3d4e5f6_report.pdf"
    }
  ]
}
```

Files are served at `GET /api/v1/artifacts/downloads/{artifact}` with bearer token or `?token=` query param.

## Server implementation

### `Session` fields

- `download_artifacts` — list of index rows (newest first).
- `pending_downloads` — FIFO of remote paths queued via `__DOWNLOAD__` (matched by basename on complete).

### `RMMServer` methods

| Method | Role |
|--------|------|
| `queue_agent_download(session, remote_path)` | Queue `__DOWNLOAD__` and track pending path |
| `note_download_queued(session, remote_path)` | Append to `pending_downloads` |
| `pop_pending_download_path(session, filename)` | Resolve remote path when agent omits `remote_path` |
| `register_download_artifact(session, filepath, remote_path)` | Append/replace index row after reassembly |
| `backfill_download_artifacts(session)` | Scan `RMM_logs/downloads/` for `{hashPrefix}_*` on register |
| `list_session_downloads(session_id_or_prefix)` | Return rows scoped to session hash prefix |

### Agent metadata

`client_rmm.ps1` `Send-RmmFileDownload` includes `remote_path` in each `file_upload` JSON chunk so the server can show the full agent path even if the pending queue is lost after restart.

## Security

- List entries only include artifacts whose filename starts with the session’s `safe_session_storage_prefix(session.id)`.
- Artifact download URLs require operator API token (same as screenshots).

## Web client

- `web/index.html` — **Downloads from agent** `<details>` panel.
- `web/app.js` — `fetchSessionDownloads()`, `renderDownloadsList()`, WS refresh on `file_upload`.
- `web/style.css` — table and preview styles.

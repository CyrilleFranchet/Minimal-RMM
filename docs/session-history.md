# Session history (web UI + REST)

Operators can browse **archived transcripts** from ended sessions in the web console without SSH access to the RMM host.

## Surfaces

| Surface | Support |
|---------|---------|
| Web UI | **Session history** list in sidebar; read-only transcript view |
| REST API | `GET /api/v1/history`, `GET /api/v1/history/{id}`, `GET /api/v1/history/{id}/events`, `DELETE /api/v1/history/{id}` |
| CLI / MCP | MCP: `list_history`, `get_history_session`, `get_history_events`, `delete_history` (CLI subcommands not in v1) |

## Storage

Under `RMM_logs/history/{session_id}/`:

| File | Content |
|------|---------|
| `meta.json` | Session fields (`hostname`, `username`, …), `event_count`, `updated_at`, `ended_at`, `end_reason` |
| `events.jsonl` | One JSON event per line (same shape as live `/events`; no server filesystem paths) |

Events append on every `_record_event` while the session is active. On **kill**, the server sets `ended_at` / `end_reason` before removing the live session.

## REST API

```http
GET /api/v1/history
```

Returns ended sessions only (`active: false`), newest first.

```http
GET /api/v1/history/{id}
GET /api/v1/history/{id}/events?since=0&limit=500
```

`{id}` may be a full session UUID or unique prefix (4+ chars).

```http
DELETE /api/v1/history/{id}
```

Removes `RMM_logs/history/{id}/` from disk (archived sessions only). Returns `409 session_still_active` if the session is still live.

## Web UI

- **Sessions** — live agents; updates via WebSocket `sessions` messages + 5 s poll fallback; beacon status recomputed client-side every 15 s.
- **Session history** — archived sessions; click to view read-only transcript (shell input and tools hidden). Hover a row to reveal **Delete** (permanent disk removal, with confirmation).
- **Kill session** — closes the console panel immediately and refreshes both lists.

## Server methods

| Method | Role |
|--------|------|
| `_history_append_event` | Append event to `events.jsonl` |
| `_history_write_meta` | Write/update `meta.json` |
| `_finalize_session_history` | Mark session ended (on kill) |
| `list_session_history` | Scan history dir for archived rows |
| `get_history_meta` / `get_history_events` | Read archive for API / web |
| `delete_history_session` | Remove archived transcript directory from disk |

## Limitations (v1)

- Transcripts exist only for sessions that produced events **after** this feature landed (or from first event onward).
- Sessions that disappear without an operator **kill** (server restart, agent exit) keep in-memory events until restart; disk history retains whatever was appended before restart.
- History list shows **ended** sessions only; active sessions stay in the live sidebar.

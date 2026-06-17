# Session history (web UI + REST)

Operators can browse **archived transcripts** from ended sessions in the web console without SSH access to the RMM host.

## Surfaces

| Surface | Support |
|---------|---------|
| Web UI | **Session history** list in sidebar; read-only transcript view |
| REST API | `GET /api/v1/history`, `GET /api/v1/history/{id}`, `GET /api/v1/history/{id}/events` |
| CLI / MCP | Not in v1 |

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

## Web UI

- **Sessions** — live agents; updates via WebSocket `sessions` messages + 12 s poll fallback; beacon status recomputed client-side every 30 s.
- **Session history** — archived sessions; click to view read-only transcript (shell input and tools hidden).
- **Kill session** — closes the console panel immediately and refreshes both lists.

## Server methods

| Method | Role |
|--------|------|
| `_history_append_event` | Append event to `events.jsonl` |
| `_history_write_meta` | Write/update `meta.json` |
| `_finalize_session_history` | Mark session ended (on kill) |
| `list_session_history` | Scan history dir for archived rows |
| `get_history_meta` / `get_history_events` | Read archive for API / web |

## Limitations (v1)

- Transcripts exist only for sessions that produced events **after** this feature landed (or from first event onward).
- Sessions that disappear without an operator **kill** (server restart, agent exit) keep in-memory events until restart; disk history retains whatever was appended before restart.
- History list shows **ended** sessions only; active sessions stay in the live sidebar.

# Session history (web UI + REST)

Operators can browse **archived transcripts** from ended sessions in the web
console without SSH access to the RMM host. The storage owner depends on the
deployment mode: server mode keeps the archive under `RMM_logs`; workspace mode
keeps it in the operator's local workspace database and artifact directory.

## Surfaces

| Surface | Support |
|---------|---------|
| Web UI | **Session history** list in sidebar; read-only transcript view |
| REST API | `GET /api/v1/history`, `GET /api/v1/history/{id}`, `GET /api/v1/history/{id}/events`, `DELETE /api/v1/history/{id}` |
| CLI / MCP | MCP: `list_history`, `get_history_session`, `get_history_events`, `delete_history`, `clear_history` (CLI subcommands not in v1) |

## Storage

### Server mode

Under `RMM_logs/history/{session_id}/`:

| File | Content |
|------|---------|
| `meta.json` | Session fields (`hostname`, `username`, …), `event_count`, `updated_at`, `ended_at`, `end_reason` |
| `events.jsonl` | One JSON event per line (same shape as live `/events`; no server filesystem paths) |
| `ai_chat.json` | Operator AI chat for this session (see `docs/web-ai-chat-memory.md`) |

Events append on every `_record_event` while the session is active. On **kill**, the server sets `ended_at` / `end_reason` before removing the live session. On **server restart**, sessions that were live before shutdown but are not in the new in-memory registry are finalized with `end_reason: server_restart` (from `sessions.json` and/or existing history directories). New agent registrations write initial `meta.json` and persist to `sessions.json` so beacon-only sessions can be archived too.

### Workspace mode

When `RMM_STORAGE_MODE=workspace`, the central server does not write
`events.jsonl`, history metadata, AI chat, downloads, screenshots, or keylogs.
It publishes result events to a bounded per-workspace queue. The matching
`workspace_service.py` consumes those events and stores them in
`RMM_WORKSPACE_DATA/workspace.db`:

| SQLite table / directory | Content |
| --- | --- |
| `sessions` | Active and ended session metadata received from the central broker |
| `events` | Local transcript events |
| `ai_chats` | Per-session AI messages |
| `artifacts/downloads` | Reassembled file downloads |
| `artifacts/screenshots` | PNG screenshots |
| `artifacts/keylogs` | Keylog text files |

The workspace service exposes the same history routes. Its event cursor is
stored in the `metadata` table. If the central transient queue overflows while
the service is offline, it reports `reset_required` and the local transcript
contains an explicit gap rather than silently claiming completeness.

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

Removes the archived session and its artifacts from the active storage owner.
In server mode this removes `RMM_logs/history/{id}/`, the `sessions.json` row,
and matching files under `RMM_logs/downloads/`, `screenshots/`, and
`keylogs/`. In workspace mode it removes the local SQLite row and
session-prefixed files. Returns `409 session_still_active` if the session is
still live.

```http
DELETE /api/v1/history
```

Removes **all** ended archived sessions (same filter as `GET /history`). Also purges each session's download, screenshot, and keylog files. Returns `{ "ok": true, "deleted": N, "errors": [...] }`.

## Web UI

- **Sessions** — live agents; hover a row to reveal **Beacon** (sleep/jitter dialog) and **Kill**. Updates via WebSocket `sessions` messages + 5 s poll fallback; beacon status recomputed client-side every 15 s.
- **Session history** — archived sessions; click to view read-only transcript (shell input and tools hidden). Hover a row to reveal **Delete** (permanent disk removal, with confirmation). **Clear all** (header button) deletes every archived session.
- **Archive cleanup** — deleting the selected archive clears the transcript and returns the main panel to the empty state. Closing the view or clearing all archives uses the same cleanup path; an in-flight archive load cannot restore a deleted transcript.
- **Close console** — **×** top-right on the session panel closes the view (live or archived) without killing the agent; sidebar selection clears.
- **Kill session** — hover **Kill** on a live session row (same pattern as archived **Delete**); closes the console if that session was selected and refreshes both lists.

## Server methods

| Method | Role |
|--------|------|
| `_history_append_event` | Append event to `events.jsonl` in server mode; no-op in workspace mode |
| `_history_write_meta` | Write/update `meta.json` |
| `_finalize_session_history` | Mark session ended in server mode; workspace mode receives the ended row through sync |
| `_archive_orphaned_sessions_on_startup` | Finalize stale/offline sessions after server restart in server mode |
| `list_session_history` | Scan server history dir or return no central archive in workspace mode |
| `get_history_meta` / `get_history_events` | Read archive for API / web |
| `purge_session_artifacts` | Delete session-scoped files in downloads / screenshots / keylogs |
| `delete_history_session` | Remove archived transcript, `sessions.json` row, and session artifacts |
| `clear_session_history` | Delete all ended archived sessions |
| `remove_persisted_session` | Drop session id from `sessions.json` (kill, delete, post-restart archive) |
| `_rehydrate_session_events_from_history` | Reload `events.jsonl` into memory when an agent reconnects after restart |

## Troubleshooting lost history

**History is not in git.** In server mode, transcripts live under
`RMM_logs/history/{session_id}/` on the machine where the server runs. In
workspace mode, they live under the operator's `RMM_WORKSPACE_DATA` directory.
`git pull` does not move or delete either location unless you run something
like `git clean`.

**Working directory matters.** `RMM_logs` is relative to the process cwd when you start `server_rmm.py`. If you restart from a different directory, the server creates a fresh empty `RMM_logs` and prior transcripts stay in the old path.

**After server restart + agent reconnect (same session id):** On startup the server archives the old session (`end_reason: server_restart`). When the agent registers again, the live console used to start with an empty in-memory transcript even though `events.jsonl` was still on disk. Reconnect now **rehydrates** events from `events.jsonl` into the live session. The session moves back to the live sidebar (not the history list) but the full transcript should appear when you open it.

**Check whether data still exists:**

```bash
ls RMM_logs/history/
curl -s -H "Authorization: Bearer $RMM_API_TOKEN" http://127.0.0.1:8080/api/v1/history
```

If `events.jsonl` exists under a session folder, the transcript can be recovered (web UI after rehydrate fix, or read the file directly).

## Limitations (v1)

- Transcripts exist only for sessions that produced events **after** this feature landed (or from first event onward); new registrations also write initial `meta.json` so beacon-only sessions can be archived after restart.
- Sessions that disappear without an operator **kill** are archived on **server restart** (`end_reason: server_restart`) if they were in `sessions.json` or had a history directory.
- History list shows **ended** sessions only; active sessions stay in the live sidebar.

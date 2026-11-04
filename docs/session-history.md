# Session history (web UI + REST)

Operators can browse **archived transcripts** from ended sessions in the web
console without SSH access to the RMM host. The operator's local workspace
database and artifact directory own history in every deployment.

## Surfaces

| Surface | Support |
|---------|---------|
| Web UI | **Session history** list in sidebar; read-only transcript view |
| REST API | `GET /api/v1/history`, `GET /api/v1/history/{id}`, `GET /api/v1/history/{id}/events`, `DELETE /api/v1/history/{id}` |
| CLI / MCP | CLI: `events --history --session ID --forward`; MCP: `list_history`, `get_history_session`, `get_history_events`, `delete_history`, `clear_history` |

## Storage

### Legacy server data for migration

Legacy installations stored the following under `RMM_logs/history/{session_id}/`.
Use [copy-only migration](workspace-migration.md) to import this data before upgrading:

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

Use `forward=1` with `since` and `limit` to retrieve ascending pages without
skipping older events. Advance `since` to the last returned event ID and repeat
until the page is empty. The WebUI loads retained live and archived transcripts
this way. See [shell identity and recovery](shell-transcript-recovery.md) for
AI session changes and storage limitations.

Without `forward=1`, both live and archived workspace endpoints return the most
recent matching page in ascending order, consistent with the central API and
CLI/MCP defaults. Prefixes are literal and case sensitive; `%` and `_` are not
SQL wildcards. `WorkspaceStore.get_events` keeps its internal forward default
for exact embedding-result lookup; the HTTP adapter explicitly selects the
requested direction. The workspace store regression tests cover both modes.

```http
DELETE /api/v1/history/{id}
```

Removes the archived session and its artifacts from the active storage owner.
This removes the local SQLite row and
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

**History is not in git.** Current transcripts live under the operator's
`RMM_WORKSPACE_DATA` directory. Old installations may still have their preserved
source archive under `RMM_logs/history/{session_id}/`.
`git pull` does not move or delete either location unless you run something
like `git clean`.

**Workspace data directory matters.** Restart the workspace with the same
`--data-dir` or prepared deployment root. Starting against another directory
creates an empty local database; the central server cannot reconstruct prior
workspace history from its transient queue.

**After central restart and agent reconnect:** Existing transcripts remain in
the workspace database. A fresh authoritative session synchronization determines
which sessions are active; reconnecting the same session ID does not erase its
local transcript. Event epochs distinguish the restarted central stream from
previously synchronized events. Events lost before reaching the workspace cannot
be recovered from central memory after a restart.

**Check whether data still exists:**

```bash
ls /path/to/deployment/workspaces/default/workspace.db
curl -s -H "Authorization: Bearer $RMM_WORKSPACE_LOCAL_TOKEN" http://127.0.0.1:8787/api/v1/history
```

For older server-owned `events.jsonl` files, use the copy-only migration procedure
or read the preserved archive. The current server does not rehydrate these files.

## Current limitations

- Only synchronized or explicitly migrated events are present in the workspace.
- Sessions absent from a successful central session synchronization become local
  history; a failed synchronization must not be treated as an empty session list.
- History list shows **ended** sessions only; active sessions stay in the live sidebar.

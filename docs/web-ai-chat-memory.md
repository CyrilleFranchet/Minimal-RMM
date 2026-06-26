# Web AI — chat memory

The AI assistant panel keeps a **per-session conversation history** on the **RMM server** so operators do not lose context when reloading the page, switching browsers, or reconnecting from another machine.

## Storage

| File | Location | Content |
|------|----------|---------|
| `ai_chat.json` | `RMM_logs/history/{session_id}/` | `{ "updated_at", "messages": [ … ] }` |

Each message has `role` (`user` / `assistant`), `content`, and optional `tool_calls_made` on assistant turns. Error lines shown in the UI are **not** persisted.

The file lives next to `meta.json` and `events.jsonl` for the same session. Up to **500** messages are kept (oldest dropped when saving).

When no RMM session is selected in the sidebar, chat stays **in-memory only** in the browser (welcome message only after reload).

OpenAI API keys, model choice, Exegol MCP settings, and skill checkboxes remain in `sessionStorage` (tab-scoped).

## REST API

```http
GET /api/v1/sessions/{id}/ai/chat
```

Returns `{ "session_id", "messages", "count" }`. Works for **live** and **archived** sessions (`{id}` = full UUID or unique prefix).

```http
DELETE /api/v1/sessions/{id}/ai/chat
```

Removes `ai_chat.json` for that session. Returns `{ "ok": true, "session_id" }`.

```http
POST /api/v1/ai/chat
```

Unchanged request shape. On success, when `selected_session_id` is set, the server appends the assistant reply to the posted `messages` array and writes `ai_chat.json`.

## Web UI

- Selecting a live or archived session loads chat via `GET …/ai/chat`.
- **Reset chat** calls `DELETE …/ai/chat` and clears the panel.
- After each successful assistant reply, the server persists; the browser does not use `localStorage` for chat (legacy `rmm_ai_chat_v1` is removed on panel init).

## Automatic purge

| Operator action | Server behavior |
|-----------------|-----------------|
| Kill live session | `clear_ai_chat` in `kill_session` |
| Delete archived session | `ai_chat.json` removed with `shutil.rmtree` on history dir |
| Clear all archived sessions | Same per session |

The web UI clears the panel when the current session is killed or deleted.

## Server methods (`RMMServer` in `server_rmm.py`)

| Method | Purpose |
|--------|---------|
| `get_ai_chat(session_id_or_prefix)` | Load messages from disk |
| `save_ai_chat(session_id, messages)` | Write `ai_chat.json` |
| `clear_ai_chat(session_id)` | Delete `ai_chat.json` |

## JavaScript (`web/ai.js`)

| Export | Purpose |
|--------|---------|
| `syncAiChatWithSession(sessionId)` | `GET …/ai/chat` when sidebar selection changes |
| `clearAiChatMemory(sessionId)` | Clear panel if that session is loaded (server already purged) |
| `purgeAiChatsForSessions(sessionIds)` | Clear panel after bulk history delete |

## Related files

| File | Role |
|------|------|
| `server_rmm.py` | Persistence + REST routes |
| `web/ai.js` | Load, render, reset |
| `web/app.js` | Session lifecycle hooks |
| `docs/session-history.md` | Transcript storage layout |

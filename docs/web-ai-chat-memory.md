# Web AI — chat memory

The AI assistant panel keeps a **per-session conversation history** in the
active storage owner so operators do not lose context when reloading the page,
switching browsers, or reconnecting from another machine. In server mode this
is the RMM server. In workspace mode it is the operator's local workspace
service.

On desktop, opening **AI** divides the space beside the session sidebar
equally between the agent workspace and the AI assistant. Both panels stay
visible side by side, separated by a border. At viewport widths of 768 pixels
or less, the panels stack vertically.

The providers do not own this history for the current integration. Each
request sends the relevant history from the active storage owner to the
selected provider: `RMM_logs/history/{session_id}/ai_chat.json` in server mode
or the workspace `ai_chats` table in workspace mode. An API key identifies
authorization and billing; it is not a portable conversation-history lookup
key. OpenAI Chat Completions, Anthropic Messages, and Mistral Chat
Completions are used as stateless request/response APIs here. Provider
products may offer separate conversation or workspace storage, but those
objects are not the same as this RMM transcript and are not queried by this
UI.

## Storage

### Server mode

| File | Location | Content |
|------|----------|---------|
| `ai_chat.json` | `RMM_logs/history/{session_id}/` | `{ "updated_at", "messages": [ … ] }` |

Each message has `role` (`user` / `assistant`), `content`, and optional `tool_calls_made` on assistant turns. Error lines shown in the UI are **not** persisted.

The file lives next to `meta.json` and `events.jsonl` for the same session. Up to **500** messages are kept (oldest dropped when saving).

When no RMM session is selected in the sidebar, chat stays **in-memory only** in the browser (welcome message only after reload).

AI provider API keys, validation state, model choice, Exegol MCP settings, and
skill checkboxes are synchronized to the authenticated server/workspace
SQLite UI state. The API token remains tab-scoped because it is needed to
authenticate the state request. Keep the selected SQLite database owner-only.

### Workspace mode

When `RMM_STORAGE_MODE=workspace`, the central server skips `ai_chat.json` and
the workspace service stores normalized messages in the `ai_chats` table in
`RMM_WORKSPACE_DATA/workspace.db`. The local service exposes the same GET,
POST, and DELETE API paths to the workspace WebUI. Workspace-scoped tool calls
forward `X-RMM-Workspace` and `X-RMM-Workspace-Token` to the internal direct or
MCP RMM client, so AI tools see the same session scope as the browser.

## REST API

```http
GET /api/v1/sessions/{id}/ai/chat
```

Returns `{ "session_id", "messages", "count" }`. Works for **live** and **archived** sessions (`{id}` = full UUID or unique prefix).

```http
DELETE /api/v1/sessions/{id}/ai/chat
```

Removes the session chat from the active storage owner. In server mode this is
`ai_chat.json`; in workspace mode it is the local `ai_chats` row. Returns
`{ "ok": true, "session_id" }`.

```http
POST /api/v1/ai/chat
```

Unchanged request shape. On success, when `selected_session_id` is set, the
active storage owner appends the assistant reply to the posted `messages`
array and persists it.

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
| `get_ai_chat(session_id_or_prefix)` | Load messages from server disk or return the central workspace-mode placeholder |
| `save_ai_chat(session_id, messages)` | Write server `ai_chat.json`; workspace service writes SQLite |
| `clear_ai_chat(session_id)` | Delete server `ai_chat.json`; workspace service deletes the SQLite row |

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

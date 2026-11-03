# Web AI — chat memory

The AI assistant panel keeps a **per-session conversation history** in the
active storage owner so operators do not lose context when reloading the page,
switching browsers, or reconnecting from another machine. The owner is the
operator's local workspace service in both deployment presets.

On desktop, opening **AI** divides the space beside the session sidebar
equally between the agent workspace and the AI assistant. Both panels stay
visible side by side, separated by a border. At viewport widths of 768 pixels
or less, the panels stack vertically.

The providers do not own this history for the current integration. Each
request sends the relevant history from the active storage owner to the
selected provider from the workspace `ai_chats` table. An API key identifies
authorization and billing; it is not a portable conversation-history lookup
key. OpenAI Chat Completions, Anthropic Messages, and Mistral Chat
Completions are used as stateless request/response APIs here. Provider
products may offer separate conversation or workspace storage, but those
objects are not the same as this RMM transcript and are not queried by this
UI.

## Storage

In the AI prompt, **Enter** sends the message. **Command+Enter** or
**Control+Enter** inserts a new line at the cursor, replacing selected text.
Enter used to confirm an input-method composition does not send the message.

### Legacy data for migration

| File | Location | Content |
|------|----------|---------|
| `ai_chat.json` | `RMM_logs/history/{session_id}/` | `{ "updated_at", "messages": [ … ] }` |

The old server-owned format is no longer used by production startup. The
copy-only migration imports it into workspace storage.

Each message has `role` (`user` / `assistant`), `content`, and optional `tool_calls_made` on assistant turns. Error lines shown in the UI are **not** persisted.

The file lives next to `meta.json` and `events.jsonl` for the same session. Up to **500** messages are kept (oldest dropped when saving).

When no RMM session is selected in the sidebar, chat stays **in-memory only** in the browser (welcome message only after reload).

AI provider API keys, validation state, model choice, Exegol MCP settings, and
skill checkboxes are synchronized to the authenticated server/workspace
SQLite UI state. The API token remains tab-scoped because it is needed to
authenticate the state request. Keep the selected SQLite database owner-only.

### Workspace mode

The central server skips `ai_chat.json` and
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

Removes the session chat from the local workspace `ai_chats` row. Returns
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
| Delete archived session | Workspace chat row removed with archived session |
| Clear all archived sessions | Same per session |

The web UI clears the panel when the current session is killed or deleted.

## Legacy compatibility methods (`RMMServer` in `server_rmm.py`)

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

## Background jobs and reconnect recovery

The WebUI now submits `/api/v1/ai/jobs` rather than waiting on `/ai/chat`.
Accepted work continues in the central server when the browser disconnects.
The server persists scoped job status and final `result.chat_messages`; the UI
recovers that transcript on reconnect, including in workspace mode. Ordinary
workspace chat storage remains local, while reconnectable execution results
are stored in the central job database. Reset dismisses completed jobs so old
results do not repopulate cleared chat; active jobs prevent reset.
See [Background AI jobs](ai-background-jobs.md) for API, storage, and restart
semantics. Server restart interrupts work without automatically replaying it.

## Task completion diagnostics

Final provider responses include server-counted rounds consumed and the total
round budget under Task timing. A round is one provider request, including
the final response. Exhausting the round budget produces an explicit incomplete
report with timing and counts. Provider output-limit stops are marked failed
(`provider_output_truncated`); their partial text is retained for inspection.
These diagnostics do not establish that the requested task succeeded.

`_final_response_error` also rejects plain-text tool-call markup such as
`<tool_call>`, `<arg_key>`, or `<arg_value>` with `malformed_tool_call_text`.
The preserved response includes an incomplete-status notice, timing, and rounds.
No command is parsed or executed from this text. The server requests a corrected
response up to twice per task, within the existing total round budget. Previous
tool results remain in context, and the correction preserves operator-consent
boundaries. Exhausted retries retain the failed response and incomplete notice.
The result field `malformed_response_retries` records retries consumed.
Transport errors and output-limit truncation are not automatically retried.
This conservative check also flags literal examples of these tags in answers.

The WebUI displays partial reports for failed jobs. `formatAiFailureDetails`
adds the job ID, last recorded round/activity, and bounded server diagnostics
(exception types, HTTP status, traceback line numbers) to the failure status.
Raw exception messages, URLs, and credentials are excluded. The last recorded
round is a progress checkpoint, not proof that the round completed.

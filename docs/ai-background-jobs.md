# Background AI jobs

The WebUI submits `POST /api/v1/ai/jobs` and receives a job ID immediately.
The central server owns the model/tool loop. Closing a browser, refreshing the
page, or losing the browser-to-workspace connection does not cancel an accepted
job. The UI polls status and discovers unfinished or completed jobs on reconnect.
The workspace forwards short job requests instead of holding a chat request open
for the duration of execution. A workspace outage can still make workspace or
Exegol tools unavailable; background execution does not repair broken tools.

## API and state

Submission accepts the existing `/ai/chat` fields plus a required `request_id`
(8–128 ASCII letters, digits, underscores, or hyphens). Successful new submissions
return HTTP 202 with `job`; repeated request IDs in the same owner scope return
that original job without re-execution. Reusing an ID for a different session
conflicts. Use a fresh ID for a new task. Admission is bounded; overload returns
503. An active job in the same session returns 409.

- `GET /api/v1/ai/jobs?session_id=ID` lists scoped jobs.
- `GET /api/v1/ai/jobs/JOB_ID` returns status, timestamps, progress, and result.
- `DELETE /api/v1/ai/jobs` dismisses completed sessionless chat jobs.
- Resetting session chat dismisses its completed jobs. Reset while a job is
  active returns 409; reset is not cancellation.
- CLI: `ai jobs [--session-id ID]` and `ai job JOB_ID`.
- MCP/web AI: `list_ai_jobs` and `get_ai_job` inspect work without spawning
  recursive AI tasks.

States are queued, running, succeeded, failed, and interrupted. Progress records
model rounds and tool names/status, not tool arguments or raw outputs. Final
results include the chat transcript and existing tool-call trace. A tool error
can still be handled by the model; job success means the model returned a final
response, not that every requested remote operation succeeded.

## Persistence and restart behavior

`rmm_ai_jobs.AIJobManager` owns the SQLite job database at
`RMM_logs/ai-jobs.sqlite3`, a bounded worker pool, and owner-scoped queries.
Its submit/get/list/dismiss methods enforce idempotency, session exclusivity,
visibility, and chat reset. Internal progress and finish methods persist state;
close prevents new work and marks unfinished jobs interrupted. The database is
created with restrictive file permissions. Request API keys and authentication
headers stay in worker memory and are not stored in job request records.
Chat content and tool traces may contain sensitive operational data, as with
existing chat history; protect the server volume accordingly.

The central job store is authoritative for reconnectable results, including
workspace-owned jobs. Workspace ownership is checked on every job query. This
adds scoped execution-result storage centrally even when ordinary chat history
is workspace-local. The returned `result.chat_messages` lets the UI recover the
conversation without relying on a lost HTTP response.

`RMMServer.ai_jobs` lazily opens the store under its shutdown lock. The provider
loop reports progress before model/tool calls and after tool results. Workers
check interruption at these boundaries; an already-running remote command or
provider request cannot be forcibly undone. Restart marks unfinished persisted
jobs interrupted. It does not replay tool calls or automatically resume model
conversations because their last side effects may be uncertain. Inspect session
events before explicitly starting replacement work.

The legacy synchronous `POST /api/v1/ai/chat` endpoint remains available for
existing clients. It does not gain reconnectable job semantics.

## Validation

Tests cover job lifecycle, persistence, ownership, idempotency, bounded admission,
interruption, HTTP handler independence, workspace routing, CLI/MCP inspection,
and browser reconnection. These use fake providers and tools and do not execute
remote operations or make paid provider calls.

WebUI helpers `jobState`/`saveJobState` retain only request/job identifiers and
reset timestamps. `currentChat` guards session changes; `setJobBusy` and
`showJobStatus` render pending/terminal states. `scheduleJobPoll`, `jobCreatedAt`,
`refreshAiJob`, and `displayAiJob` discover and render scoped server results.
`submitAiJob` sends one idempotent submission and retains an uncertain request
in memory for an explicit retry with the same ID. It does not save request
credentials or automatically re-submit after losing a response.

If submission loses its response, the UI searches by request ID instead of
starting another task. While the original payload remains in memory, the
operator can explicitly retry that same request ID. After a browser reload,
if the server never accepted the request, the outcome remains unresolved and
the original payload cannot be reconstructed automatically. Inspect job and
session history before starting replacement work. Accepted jobs remain
recoverable without that browser payload.

## Killing a session during AI work

Deleting a session interrupts its queued and running background jobs immediately
in the job store. `AIJobManager.interrupt_session` retains their audit records;
`_check_active` rejects their next provider/tool/finalization checkpoint. Queued
workers cannot start after interruption. Other sessions and sessionless jobs
remain independent. Submission is serialized with deletion, and completed work
cannot recreate the killed session's chat file.

The current provider request or external tool call may still finish. Its effects
cannot be undone by cancellation; the next checkpoint prevents further tool
calls. The worker retains its capacity slot until that call returns. This is
cooperative cancellation, not forced termination of remote processes.

Session deletion wakes a pending `exec_and_wait` immediately; the exec endpoint
returns HTTP 410 (`session_killed`). Server shutdown returns 503. A second
simultaneous exec returns 409 (`session_busy`) instead of replacing the first
waiter. `Session.wait_command` records the expected command: operator events and
results echoing a different command do not satisfy its waiter. Legacy results
without a command echo remain supported, but cannot be reliably attributed when
other queued or persistent work overlaps; identical command strings also lack
unique execution IDs.

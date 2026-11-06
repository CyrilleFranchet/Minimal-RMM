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

Canceling a job marks its persisted state interrupted immediately, but a new
request for the same owner/session remains blocked with 409 until the previous
worker exits. The worker may still be waiting on an already-dispatched command;
terminal status alone does not prove that execution has stopped. An in-memory
scope reservation in `AIJobManager.submit`/`_run` prevents overlapping replacement
work and is released in worker cleanup, including worker-start failures.
Idempotent requests for the original job continue to return its recorded state.

- `GET /api/v1/ai/jobs?session_id=ID` lists scoped jobs.
- `GET /api/v1/ai/jobs/JOB_ID` returns status, timestamps, progress, and result.
- `DELETE /api/v1/ai/jobs` dismisses completed sessionless chat jobs.
- Resetting session chat dismisses its completed jobs. Reset while a job is
  active returns 409; reset is not cancellation.
- CLI: `ai jobs [--session-id ID]` and `ai job JOB_ID`.
- MCP/web AI: `list_ai_jobs` and `get_ai_job` inspect work without spawning
  recursive AI tasks.

States are queued, running, succeeded, failed, and interrupted. Progress records
model rounds, tool names/status, active skill IDs, a public step summary and a
bounded execution checkpoint. It does not record tool arguments or raw outputs;
step summaries and checkpoints must not contain secrets. Final
results can include the chat transcript, tool-call trace, final step state,
and a manifest of scoped evidence artifacts. A tool error
can still be handled by the model; job success means the model returned a final
response, not that every requested remote operation succeeded.

## Persistence and restart behavior

### Model request timeout recovery

`rmm_ai._model_request` retries a timed-out model request at most twice,
waiting one second and then two seconds. Each attempt retains the existing
180-second socket timeout. OpenAI, Mistral, and Anthropic use this wrapper;
provider validation requests do not. HTTP errors and malformed responses are
not retried by this mechanism.

Retries keep the same conversation, including completed tool results, and do
not replay tool execution or consume another model round. Progress reports
`retry_wait` and `retrying` statuses and checks job interruption before another
request. A timed-out request may still have been processed by the provider,
so a retry can incur another model charge. After three failed attempts the
original exception chain is retained in job diagnostics. Recovery is in memory;
it does not resume already failed jobs or survive a server restart.

`tests/test_ai_model_retry.py` covers retained tool results across all providers,
bounded exhaustion, timeout classification, and interruption before retry.

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

### Failure diagnostics

Unhandled worker exceptions persist `result.diagnostics.exception_chain` in the
existing job record, accessible through `ai job JOB_ID` and the owner-scoped API.
Each entry contains the exception type, up to 12 traceback line numbers, and an
HTTP status for urllib HTTP errors. `_failure_diagnostics` follows at most eight
causes or unsuppressed contexts and handles cycles. It excludes exception
messages, URLs, headers, source text, and local variables. The existing `progress`
field identifies the last model round or tool checkpoint; it is not proof that
the corresponding operation failed or had no effects. The diagnostic metadata does not contain partial tool outputs. The AI runtime
separately retains received outputs in scoped evidence archives and attaches
artifact manifests and returned-call references to structured provider/tool
failure results, preserving the latest step checkpoint without exposing the
provider exception text. See [context evidence](web-ai-skills.md).

Deploy and restart the server to enable this for future failures. Existing failed
jobs cannot recover discarded exception details. No database migration is needed.

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

## Stop a task

Use **Stop task** in the WebUI, or POST `/api/v1/ai/jobs/{id}/cancel`.
`AIJobManager.cancel` scopes cancellation to the authenticated owner and is
idempotent; unknown or other-owner IDs return 404. Workspace mode proxies the
request under its workspace identity. Completed jobs retain their result.
Active jobs become interrupted immediately. Execution stops cooperatively at
the next progress checkpoint; an in-flight provider request or dispatched tool
may continue. Cancellation never rolls back actions or replays the task.

The Stop control occupies the Send button position below the prompt while a
job is active, outside the scrolling transcript. It shows Stopping while the
cancellation request is pending and restores Send when the job terminates.

## Execution details in AI chat

Each new job reports its tool path, MCP status, and Exegol status. MCP is
marked connected only after initialization and tool discovery succeed. Every
AI job requires Exegol independently of its playbook. Missing MCP support or
`RMM_AI_USE_MCP=0` produces `exegol_requires_mcp`; an unavailable owner runtime
fails readiness before execution. There is no direct fallback.

`run_ai_chat.report_execution` attaches connection status to progress events.
Legacy skill/step metadata may remain in stored records for compatibility, but
the current UI does not display skill selection or active-skill labels. The
explicit playbook is retained in the accepted job's request snapshot, not in
public progress labels. An established MCP connection is not relabeled failed
merely because a later model request fails.

## Concurrent tools within a conversation

The AI scheduler runs up to four approved independent tool calls together within
one model round. Set the environment-only `RMM_AI_TOOL_CONCURRENCY` on the AI
server process to change the bound (integers are clamped to 1–16;
unparsable values use 4). Set it to `1`
for serial scheduling, without additional session-resolution calls. This does
not change the number of background job workers.

The initial read allowlist is `health`, `list_sessions`, `get_session`,
`get_events` without forwarding, `list_plugins`, `list_ai_jobs`, and `get_ai_job`.
`exec_command` may overlap across different resolved hosts. References are
resolved through the caller's authenticated API client and replaced with full
session IDs. Commands sharing a session ID or case-insensitive hostname remain
ordered within that conversation, including different sessions on the same
named host. This is not a global host lock across separate AI jobs or manual
CLI/MCP calls. Missing hostname,
failed resolution, and ambiguous references fall back to serial execution.
Hostnames are the available identity boundary; distinct names for the same
physical machine cannot be recognized as one host automatically.

Every other tool is a barrier within the model round: preceding parallel work finishes first, the tool
runs alone, and subsequent work starts afterward. This includes unknown/external
MCP tools, queued commands, configuration changes, transfers,
BloodHound actions, and lifecycle operations. A tool name alone is not evidence
that an arbitrary action is safe to overlap. Dependent calls must be requested
in later model rounds; the model receives explicit guidance about this boundary.

Results and tool traces retain the model's original call order and provider call
IDs even when completion order differs. Tool-returned error payloads remain
model-visible results. Raised exceptions stop admission of pending work; already
started calls drain before the exception escapes. Cancellation is cooperative:
remote operations and blocking worker threads cannot reliably be stopped, and
no operation is automatically replayed. Progress remains the latest completed
or running call, not a complete list of all concurrently active calls.

Direct tool dispatch runs outside the asyncio event loop. The approved MCP
handlers also run off-thread because the MCP SDK executes ordinary synchronous
handlers inline. API schemas, authentication scope, and command behavior are
unchanged. Endpoint polling, interval, jitter, result submission, server command
delivery, and the query catalog are unchanged. Concurrency can change aggregate
traffic across hosts; live Vectra detection behavior still requires a demo test.
Dependent commands on one host retain their existing callback delays.

### WebUI, CLI, and MCP boundaries

The WebUI submits AI jobs to the server-owned orchestration loop. CLI
`ai jobs` / `ai job` and MCP `list_ai_jobs` / `get_ai_job` inspect those same
jobs through the shared API; they do not submit recursive AI conversations.
The WebUI Stop action uses the existing cancellation route; this change does
not add a CLI or MCP cancellation command.

The concurrency limit is an environment-only server setting, not a WebUI,
CLI, or MCP per-request option. Ordinary manual CLI/MCP command calls do not
pass through the AI scheduler. Approved MCP handlers support overlapping calls,
but existing server execution restrictions still apply. All surfaces retain
their existing command parameters and result formats.

### Implementation and validation

`rmm_ai_concurrency.tool_concurrency` reads and bounds the setting.
`run_tool_batch` classifies calls and preserves transcript order; its `invoke`
helper reports execution progress, and `drain` admits bounded work while excluding
occupied host/session resources. Per-run `resolve_session` closures in both AI
transports use the existing visible-session resolver off-thread. The MCP
`_offload_tool` decorator and its `execute` wrapper preserve registered names,
annotations, descriptions, and schemas while offloading blocking calls.

`tests/test_ai_concurrency.py` covers bounded overlap, alias and host ordering,
barriers, rollback, failure draining, cancellation, all three provider result
formats, direct blocking calls, and registered MCP dispatch/schema preservation.
The MCP test requires the MCP SDK. These are deterministic mocked workflow tests;
no live endpoint, provider, or Vectra detection claim is made.

## Playbook and bounded context

One explicit playbook and the operator request drive the provider conversation.
There is no preparation barrier or dynamic skill selection. The full playbook
is pinned in the system prompt, and operational tool schemas remain available
on every request. Large results and older complete conversation groups can be
archived under scoped evidence IDs and retrieved through `read_context`.
Job polling returns evidence manifests rather than full archived output.

Context admission, bounded recovery for empty/truncated replies, checkpoint
limitations, and template authoring are documented in
[Playbook execution](web-ai-skills.md) and [AI providers](ai-providers.md).

## Operation activity and protected evidence

Each job exposes a chronological `activity` array through its existing,
owner-scoped job API. `AIJobManager._progress` persists a timestamped safe
checkpoint before a tool invocation. It retains the latest 500 events and
reports the removed count as `activity_omitted`. Existing databases migrate on
startup; historical calls cannot be reconstructed. Activity survives terminal
failure and restart independently of the last `progress` checkpoint.

`run_tool_batch` assigns a distinct `call_id` to each invocation, with matching
start and response/error events, elapsed milliseconds, argument field names,
and response byte count. MCP activity identifies the resolved `rmm`, `exegol`,
or internal backend. A completed call means a response was received, not that
the command succeeded. An exception marks `outcome_unknown`: a remote command
may still have executed. A job that stops or restarts with only a running event
also has an unresolved outcome; nothing is automatically replayed.

Canceling a job does not discard a later response from an already-started call.
`AIJobManager._progress` retains a matching call's terminal observation and
evidence references even after interruption, then stops the worker at that
checkpoint. The job remains interrupted, its original stop time is retained,
and no subsequent call is admitted. Unmatched or repeated observations cannot
revive stopped work.

`ContextDispatcher.prepare_activity` archives command arguments before the
call begins, and `activity_evidence` supplies asynchronous task-local references
to arguments and returned output. No raw arguments, command, output, credential,
or exception message is included in the activity metadata. Context archives
remain protected operational evidence and may contain secrets. Their existing
owner-local file permissions and retention requirements still apply.

The operation details dialog can explicitly retrieve a bounded evidence page
using the authenticated UI endpoint
`GET /api/v1/ai/jobs/JOB_ID/evidence/ARTIFACT_ID?offset=0&limit=4096`.
`AIJobManager.read_evidence` requires both the authenticated job owner and an
artifact reference recorded on that job. A private persisted evidence scope is
never returned in job metadata. Pages report `content`, `characters`, and
`next_offset`; offsets count Python Unicode characters. Evidence is not included
in diagnostic exports automatically. Inspect it locally before sharing.

Failure diagnostics traverse bounded `ExceptionGroup` children as well as
causes and contexts. Only exception types, HTTP status, and traceback line
numbers are exposed; nested exception messages remain excluded.

## Operation details dialog

Each displayed AI job has an **Operation details** button. The modal dialog
shows its identity, state, timestamps, elapsed time at the last update, and the
last progress checkpoint. The operation selector includes recent jobs discovered
for the current chat. Existing polling refreshes the selected running job's
recorded activity without resubmitting work; completed and failed jobs remain
inspectable after reconnect through the existing job discovery endpoint.
While the dialog is open, it also refreshes the selected job every two seconds,
including stopped jobs whose dispatched calls may still return observations.
Closing the dialog stops these read-only requests. Refresh failures leave the
last data visible with a reconnecting notice; no command is repeated.

The activity timeline retains expandable event metadata and failure diagnostics.
A call start is not proof of remote execution, and a received response is not
proof that the operator's goal succeeded. A terminal job with an unfinished call
is labeled **Outcome unknown**. Older jobs without activity show an explicit
missing-history state. The dialog reports omitted older event counts.

Operators can explicitly load protected command arguments or tool output from
owner-scoped evidence references. These reads are paginated and capped to a
65,536-character browser preview. Evidence can contain secrets and is rendered as plain
text. It is never loaded automatically or included in **Copy diagnostics**,
which exports only allowlisted job, activity, and exception metadata. Failed
reads remain retryable; unavailable historical evidence cannot be reconstructed.

`web/ai-operation-details.js` owns the dialog lifecycle, safe metadata projection,
operation selection, lazy evidence reads, copy behavior, and scope reset. Native
modal behavior provides Escape dismissal and keyboard focus containment. The
Close button is always available. Expanded events and summary focus survive
polling updates. Switching chats or resetting them clears the cached details.
`tests/test_ai_operation_details.cjs` covers metadata filtering, explicit evidence
loading, final-page handling, failures, retained expansion, unknown outcomes,
copy exclusions, and account-generation cache invalidation.

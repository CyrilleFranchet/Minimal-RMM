# Durable Exegol command execution

[Documentation index](README.md)

## AI adapter and recovery

The AI MCP session wraps Exegol's `execute_command_in_container` tool in
`DurableExegolBackend`. The upstream Exegol MCP server needs no changes. Direct
`McpHttpSession` users, including prerequisite checks and BloodHound lifecycle
operations, retain their original behavior. Other external tools, including
`execute_remote_command`, keep their native synchronous execution behavior.

The original container-command schema is preserved. RMM persists a random
execution ID before sending a launch request, then polls status automatically
without spending model rounds. Each MCP helper request has a 30-second timeout;
polling normally waits two seconds between requests. A transport timeout stops
waiting and returns `outcome_unknown` with the saved ID, not a claim that the
command failed or stopped. No launch request is automatically retried.

The adapter exposes three synthetic Exegol tools to the AI session:

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `list_exegol_executions` | None | Return the latest 100 owner-local handles and last observed states. |
| `get_exegol_execution` | `execution_id`, optional byte `offset` and `limit` | Refresh state and retrieve up to 65536 output bytes. |
| `cancel_exegol_execution` | `execution_id` | Persist cancellation intent; inspect the returned state and poll until termination is confirmed. |

Ask the AI to inspect the existing execution ID after a disconnect. If its ID
is unavailable, list executions first. Never repeat a command just to retrieve
its result. Repeated identical commands in the same container reuse an
unresolved local handle; completed commands may be run again intentionally.
A missing remote record stays unresolved and does not authorize replay.

Automatic results include `execution_id`, `state`, nullable `exit_code`, and
combined `output`. Safe supervisor error codes explain failures such as an
output limit or unconfirmed termination. The adapter retains at most 1 MiB of
output in its automatic result, continues polling until execution finishes,
and returns `next_offset` and `output_truncated` when more output is available.
Recovery pages include readable UTF-8 output plus exact `output_b64`; use the
latter for binary data or UTF-8 sequences split across byte-page boundaries.

`RMM_EXEGOL_EXECUTION_TIMEOUT` is a persistent service setting: default 3600
seconds, minimum 1, maximum 86400. It bounds the actual command independently
of transport requests. Change it through the existing service settings UI,
CLI, or MCP and restart the owning service. A deadline terminates the process
group with the supervisor's grace period. Stopping an AI job only stops its
polling at the next checkpoint; explicitly cancel its execution to request
remote termination. A process stuck in uninterruptible kernel I/O can delay
termination confirmation.

`execution_scope` hashes the authenticated RMM endpoint, operator/workspace
credentials, and Exegol endpoint credentials. Credentials are not written to
the local execution registry. Token rotation or an endpoint change creates a
different scope; old handles are not automatically transferred. Within each
scope, `ExecutionStore` stores metadata in
`RMM_LOG_DIR/exegol-executions/SCOPE.sqlite3` with private permissions. Its
`claim` transaction prevents concurrent duplicate unresolved launches;
`get`, `update`, and `list` resolve and record owner-local observations, using
short SQLite transactions from `connect`. Each connection closes immediately
after committing or rolling back; long polling does not accumulate open
SQLite handles until garbage collection.
Commands and outputs stay in protected AI evidence and remote execution
files, not the safe activity metadata. Safe `exegol_execution` checkpoints
record the execution ID before launch and on subsequent observations.
Execution checkpoints inherit `round` from the model tool invocation that
triggered them. `CURRENT_TOOL_ROUND` is task-local; `run_tool_batch` restores
the previous context after completion, error, or cancellation, so concurrent
conversations cannot exchange round numbers. Launch and polling share their
parent round; recovery queries in a later round carry that later round.
Standalone MCP use outside a model round leaves the field absent. The WebUI
shows recorded rounds in timeline summaries and copied diagnostics. Historical
events without a recorded round cannot reconstruct it.
They also retain the supervisor action, observed output byte count, signed
exit code when known, and allowlisted supervisor error code. The WebUI keeps
these safe fields in expanded activity and Copy diagnostics. Execution states
use execution-specific labels, so `completed` identifies the supervised
command's completion rather than a generic tool response. Older execution IDs
already retained by the API become visible when the updated UI loads; action,
exit, output-size, and error fields require observations from the updated
service. Commands and raw output remain in explicitly loaded evidence.
The WebUI's `executionMetadata` helper validates the hexadecimal handle,
supervisor-action and error-code enums, signed exit-code bounds, and output
counter before projecting them into snapshots and copied diagnostics.
Consecutive execution heartbeats with the same state and action update the existing activity entry
instead of appending another. This keeps the active tool's start/evidence
references available during long commands while still checking interruption
on every poll and retaining meaningful state transitions.

`DurableExegolBackend` enters and closes the original backend, preserves its
tools, and adds recovery schemas from `recovery_tools`. `call_tool` validates
arguments, claims an execution, and follows status/output. `request` sends a
bounded helper call, verifies the execution-specific response frame, and
records a `checkpoint`; shell startup banners cannot hide the framed response.
`pending` retains an unresolved handle after transport failure. `outcome`
preserves allowlisted error codes, and `page_result` decodes a bounded page.
The adapter is installed only by `run_with_mcp_session`; it does not add REST
endpoints, CLI commands, or tools to the standalone RMM MCP server.

## Container supervisor

`rmm_exegol_supervisor.build_request(scope, execution_id, action, ...)` builds
a short command for Exegol MCP's existing `execute_command_in_container` tool.
It accepts `start`, `status`, and `cancel`; it validates the owner scope and
execution ID, integer deadline, command size, and byte-page bounds before
encoding the stdlib Python helper and request. Shell metacharacters in the
operator command are data until the supervised zsh evaluates that command.
The container must provide Python 3, zsh, and Linux `waitid` support.
Commands are limited to 65,536 UTF-8 bytes. The final encoded shell request
must also fit within 120,000 bytes because Exegol forwards it as one `CMD`
environment string, below Linux's 128 KiB per-string limit. Unicode is encoded
directly in JSON before base64; heavily escaped control characters can reach
the encoded limit earlier and are rejected before dispatch.

The remote helper creates private directories under
`/var/tmp/minimal-rmm-executions-UID/SCOPE/EXECUTION_ID`. Directory permissions
are `0700`; files are `0600`. Existing directories and files must belong to the
effective user and have private permissions. Symlinks and multiply linked
files are rejected. A scope separates authenticated RMM owners; it does not
isolate them from the container's root user or other code running as that user.

The helper functions implement the following internal protocol:

- `private_directory` and `open_private` validate filesystem ownership, type,
  permissions, and link properties before use.
- `atomic_json` flushes and atomically replaces a JSON record, then syncs its
  directory. `read_json` reads a record through the same safe file opening.
- `lock_file` takes an advisory exclusive lock. `alive` checks the supervisor's
  held lock without trusting a stored PID or signaling an unrelated process.
  The worker waits for concurrent status probes to release this lock; probes
  use nonblocking acquisition.
- `handle` dispatches requests. A launch lock serializes simultaneous starts;
  the first start persists its request and launch intent before spawning the
  detached supervisor. Repeating an execution ID with the same command and
  deadline retrieves that execution. Different contents conflict. A crash in
  the launch gap leaves an unresolved execution; it does not replay the command.
- `supervise` runs the command in a separate process session with stdin closed,
  inherited environment, and Exegol-compatible zsh initialization and aliases.
  It persists status and exit code independently of any MCP connection.
- `collect_output` drains a nonblocking stdout/stderr pipe into a combined file.
  It completes short file writes and fails if a write makes no progress, so
  captured bytes cannot be silently discarded at a storage boundary.
  Each drain is bounded so continuous output cannot starve deadline and cancel
  checks. Captured output is capped at 64 MiB without restricting the sizes of
  other files created by the command. Exceeding the cap terminates execution
  with `state=failed` and `error=output_limit_exceeded`.
- `stop_group` sends SIGTERM, allows a two-second grace period, then sends
  SIGKILL and reaps the direct child. Linux `waitid(WNOWAIT)` keeps the direct
  child unreaped during signal decisions, preventing PID reuse. On normal
  leader exit, remaining members of its process group are killed before the
  leader is reaped. These command executions are unsuitable for launching
  persistent services; use a service lifecycle interface instead. Descendants
  that deliberately escape the process group are outside this guarantee.
- `snapshot` returns state, timestamps, nullable `exit_code`, `output_bytes`,
  base64-encoded `output_b64`, and `next_offset`. Offsets count bytes, including
  invalid UTF-8, and the next offset always follows the returned chunk.

Successful helper responses use a single line framed as
`RMM_EXECUTION:EXECUTION_ID:JSON`. The adapter can identify its response even
when the outer Exegol zsh initialization prints unrelated text. Command output
is captured separately and never interpreted as this response frame.

The lifecycle states are `starting`, `running`, `completed` (exit code zero),
`failed`, `timed_out`, `cancelled`, and `outcome_unknown`. Missing references
return `not_found`. A status query never creates a missing execution. Cancel
persists intent for the supervisor; it does not claim termination succeeded.
Status detects a dead supervisor through its released lock. A launch intent
receives a ten-second startup allowance before it is reported unknown. A
supervisor that cannot confirm termination reports `outcome_unknown` with
`error=termination_unconfirmed`.

Execution records and captured output survive MCP reconnects and RMM restarts.
They are stored in the selected container, so container deletion, `/var/tmp`
cleanup, or storage loss can remove them. There is no automatic replay or
automatic deletion of retained execution evidence. Operators should apply
their workspace retention policy to completed execution directories; do not
delete records for active or unresolved executions.

## Supervisor validation

`tests/test_exegol_supervisor.py` exercises real detached subprocesses, repeated
and concurrent submission, aliases, combined and binary output, byte paging,
exit status, deadline, cancellation, output bounds, private permissions,
symlink rejection, missing references, and unresolved launch intent.
It also verifies unknown outcomes for a dead running worker, absence of replay,
and encoded request bounds with Unicode and escaped control characters.

The macOS fixture substitutes polling and direct-child signals because its
Python lacks `os.waitid` and the test sandbox prohibits process-group signals.
The unmodified Linux-only regression verifies that background descendants
cannot keep writing after command completion. That regression is skipped on
macOS; host tests do not establish native Exegol/Linux process-group behavior.

The unchanged supervisor suite has also been executed in a disposable Debian
Linux container, including the process-group regression. This validates Linux
supervisor behavior independently of the macOS substitutions.

## Real MCP HTTP integration validation

`tests/test_exegol_http_integration.py` runs a real localhost MCP HTTP server,
the production `McpHttpSession` and `DurableExegolBackend`, and the actual
encoded supervisor in a disposable Linux container through Docker exec. The
fixture matches Exegol's zsh initialization and JSON result envelope. It does
not patch `build_request` or the supervisor. It validates recovery after a
successful launch response is lost, a new MCP session recovering the same
execution exactly once, cancellation after disconnect, and a known deadline
outcome. A shell banner without a newline exercises response framing.
Another case enters the production composite session with the real RMM stdio
server and the fixture Exegol HTTP server, checking tool discovery, dispatch,
and session cleanup together without contacting an RMM operator API.

The fixture methods `setUpClass` and `tearDownClass` own the localhost server
and socket; `setUp` isolates the local execution store and deadline;
`backend` creates each production client connection. The fixture tool accepts
only the explicitly selected disposable container. These tests require Docker,
MCP support, and uvicorn and are opt-in; ordinary AI unit tests need no Docker.

Prepare a **disposable** container named `rmm-exegol-test-*`, install Python 3
and zsh inside it, and run:

```bash
make check-exegol-execution-integration RMM_TEST_EXEGOL_CONTAINER=rmm-exegol-test-http
```

Use the configured project Python interpreter through `PYTHON=...` when
needed. Remove the disposable container afterward. The tests create execution
records and marker files inside it. Successful fixture tests establish the
real MCP HTTP and Linux execution path; they do not establish compatibility
with every installed Exegol version or replace testing the deployed lab
endpoint.

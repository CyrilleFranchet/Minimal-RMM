# AI v3 execution architecture

V3 replaces model-controlled tool preparation with a tool-less Supervisor,
code-compiled specialist capsules, and bounded Executor episodes. It is the
default on the experimental `v3` branch. It is not yet a general replacement
for every enabled execution skill; see the [full review](ai-v3-review.md).
Set `RMM_AI_ENGINE=legacy_v2` and explicitly select the **Legacy v2
(unrestricted tools)** workflow to use the previous engine. Clear v3 scope
fields first: legacy cannot enforce them and rejects nonempty scope. Other
workflow selections under legacy are rejected before tools connect, so Plan
only and Explain cannot silently become execution. This is a code rollback switch, not a database
migration or automatic replay mechanism.

## Analysis of the supplied report

The supplied `pentestgpt-pentagi-best-parts.md` identifies two useful complementary
patterns: deterministic episodic memory and fixed specialist tool boundaries.
V3 adopts these principles, exact receipt quotations, typed outcomes, dispatch
journaling, and repeat limits. No upstream source was copied; the report's
claims about those projects were not independently verified.

V2's weakness was architectural: the same model selected its instructions,
selected tools, executed them, and decided whether its own account proved
completion. More prompting through `prepare_step` did not separate these powers.
V3 keeps the existing metadata/dependency guards but removes `prepare_step`
from the v3 model surface. Legacy loops remain isolated for rollback.

V3 does not add thirteen general agents, a knowledge graph, an installer, or
summarized narrative memory. These increase complexity without enforcing the
contracts needed here. Provider transcripts are disposable; evidence and state
are not summaries written by a model.

## Admission, skills, and tools

The operator selects a workflow and supplies typed `execution_scope` separately
from chat. The default `auto` workflow is **plan only**; `explain` also has no
execution authority. Operational workflows run specialists autonomously after
admission. Scope is never extracted from addresses mentioned in prose.

| Workflow | Specialist methods and execution boundary |
| --- | --- |
| `observe` | Session reads and a fixed Windows identity/token/OS/network baseline |
| `network` | Baseline, selected-session SOCKS/TUN, route probe, structured Nmap |
| `directory` | BloodHound graph queries and catalog discovery; no LDAP shell substitute |
| `collect` | BloodHound collection/import/verification with an explicit domain; AI collection uses owner-validated strict SOCKS |
| `files` | Inspect download records; no arbitrary transfer or file mutation |
| `embeddings` | Inspect existing profiles/results; no profile mutation or plugin launch |
| `agent_commands` | Execute only exact operator-approved commands on the selected session |

The Supervisor proposes one immediate typed task. Code supplies mandatory skill
bundles and fixed tools. It can select up to four additional enabled `skill_ids`
for that **current step**, using compact catalog descriptions. A leading
`/skill-id` also requires that method on each operational task. Dependencies
must be enabled. Missing or disabled methods block admission, including explicit
invocations; no alternative shell is granted. Enabled optional bodies enter
only their specialist episode. The two locked core bodies govern every role.
Custom methods can add instructions but cannot grant tools outside a capsule;
a new execution capability needs an explicit adapter and completion contract.

Checkbox changes affect the next accepted request. Accepted jobs retain their
original snapshot. Only enabled bodies are snapshotted and archived. The skill
snapshot records each body hash and scoped artifact ID for audit. Technical
prompts, comments, and documentation are English; public progress shows the
current role, task, and active methods, not private model reasoning.

## Scope and remote Exegol

`execution_scope` accepts `targets` and `exclude` (IPv4 hosts/CIDRs), `directory`,
`commands` (exact strings), `proxy_host`, and `tun_device`. Network execution
requires allowed targets. A scan task covers at most 256 addresses and cannot
overlap an exclusion. Large allowed ranges must be partitioned. Run completion
requires coverage of all allowed addresses after subtracting exclusions.

The selected session is resolved through authenticated RMM state. Workspace
identity and Exegol container come from the owning service, not browser fields.
A remote Exegol connection alone provides no route to target resources.

For SOCKS scans, the verified owner runtime establishes that Exegol and the
workspace pivot share a network namespace. The proxy uses that namespace's
loopback and a port from live selected-session state. An optional `proxy_host`
must be loopback; arbitrary cross-namespace mappings are rejected. V3 creates a private temporary strict
proxychains configuration and invokes a fixed Nmap TCP-connect command. It
requires a target open/closed TCP response before scanning; filtered/timeouts
are inconclusive. Nmap XML and a fresh exit marker establish command completion.
Route evidence must match target, session, container, transport, and current
relay configuration. The relay is checked again before the scan.

For TUN scans, the device comes from the authoritative selected relay; an
optional `tun_device` must match it. Live relay routes must cover the task.
`ip -j route get` inside Exegol must select that device for every destination
in the scan CIDR, including possible more-specific routes, before the scan.
Route checks are observations at dispatch time; they do not
freeze the operating system's routing table.

The owning workspace verifies it shares the selected Exegol container's network
namespace. BloodHound collection passes a typed session/SOCKS binding to that
owner. The owner resolves its live loopback relay again inside the worker and
wraps the collector argv with installed proxychains using a private strict-chain
configuration. DNS collection remains TCP. A missing wrapper or changed relay
fails collection; there is no plain subprocess fallback for this AI path.
Collection status waits in code with five-second polling and cancellation
checkpoints for up to 1,850 seconds; it never resubmits collection/import. A
wait deadline is noncompletion requiring receipt inspection. AI collection
through TUN is not implemented. Existing manual collection calls
without a network binding retain their previous behavior.

`bloodhound-queries/catalog.json` remains a query aid, not a skill or instruction
source. Query discovery does not prove a path was executed. Database evidence
can include earlier ingestions; see the existing BloodHound provenance warnings.

## State, evidence, and context

Each accepted request creates a private SQLite database at
`RMM_LOG_DIR/ai-runs/<owner-session-scope>/<run_id>.sqlite3`. It stores the
request ID, objective, operator constraints, execution scope, selected session,
container, skill snapshot, tasks, attempts, invocations, observations, and
revisioned transitions. Raw arguments and results live in the existing scoped
context artifact archive. These files can contain sensitive operator inputs
and tool output; treat the log directory as private evidence storage.

Each provider request is fresh: core/method instructions, a bounded database
projection, and tools for one role. The Supervisor receives eight open tasks,
four closed tasks, six observations, four diagnostics, and aggregate counts.
The Executor receives one task, up to six explicit basis observations, two prior
observations, and two recent receipt previews. Evidence can be paged in exact
4,000-character chunks. `read_evidence(part=source)` follows an adapter's source
reference only from a receipt authorized for this task or its basis, so complete
Nmap and baseline output remains inspectable. Compact baseline receipts retain
section states without injecting full interface/token dumps into each episode.
V3 disables the legacy archive index because it exposes `read_evidence`, not
`read_context`. Context cost does not grow with the full transcript;
it still depends on instruction/snapshot size. Oversized instructions fail the
configured `RMM_AI_CONTEXT_BUDGET` before another action, rather than being
silently truncated. `RMM_AI_OUTPUT_RESERVE` reserves provider output space.

One native structured call is allowed per episode on OpenAI, Anthropic, and
Mistral. Compiler errors become bounded feedback. Supervisor/task/attempt
budgets bound retries. Trusted compiler rejections before operational dispatch can be retried within
the episode budget. Attempted mutations, including backend failures, return
their durable receipt reference on duplicate calls;
reads may refresh twice per attempt. There is no general-purpose shell for
network or directory tasks and no model-selected container command string.

Dispatch intent is committed before the backend call. Its exact result is
archived and committed before another progress/cancellation checkpoint.
`finish_task` must cite a receipt and an exact contiguous excerpt. Queued,
unknown, error, and oversized receipts cannot prove completion. Baselines need
an actual fixed-baseline receipt; scans need an actual scan receipt; graph tasks
need a graph-query receipt. Generic health checks cannot substitute for those.
A preview may be truncated while the full canonical receipt remains available.

## Limits and recovery

Network and collection admission require a completed selected-session baseline.
The fixed baseline records identity/SID, full token information, host membership,
OS/build, interfaces/DNS/routes, process architecture, and filesystem context.
Failed sections explicitly record unknown status and a reason. This is local
fact collection; it does not provide autonomous secret acquisition.

V3 is a foundation with deliberately bounded adapters, not feature parity with
all unrestricted v2 shell workflows. The baseline currently supports Windows.
File and embedding specialists inspect records only. A blocked prerequisite is
reported explicitly. General natural-language objectives still require model
judgment; evidence contracts constrain what can be reported as completed but do
not prove every semantic claim in free-form reports.

Each run is independent. SQLite is durable audit/state, not cross-request memory
or an automatic restart/resume engine. Caught interruptions preserve uncertainty;
a hard process death can leave a durable `started` invocation. Neither case is
authority to replay it. Background job recovery retains its existing interrupted
status behavior. Inspect job/run receipts before submitting replacement work.
There is no automatic restart replay or public API to reopen an old v3 kernel.
No live Exegol/agent scan was performed by the automated test suite.

## Implementation reference

The following tables document the new classes and functions. Existing adapters
in `rmm_ai.py`, server/workspace forwarding, job progress, settings, and the UI
carry the workflow/scope and trusted owner metadata without changing beacon
execution or the manual operator tools.

### rmm_ai_kernel.py

| Symbol | Responsibility |
| --- | --- |
| `now` | Return a UTC transition timestamp. |
| `KernelError` | A bounded public state-contract rejection. |
| `ExecutionKernel` | Own one authenticated run; provider transcripts never become canonical state. |
| `ExecutionKernel.__init__` | Initialize the documented state contract. |
| `ExecutionKernel.revision` | Read the authoritative revision. |
| `ExecutionKernel._transition` | Record a state transition in the caller's transaction. |
| `ExecutionKernel.add_task` | Admit one compiled task against a current revision and canonical basis IDs. |
| `ExecutionKernel.task` | Read one task owned by this run. |
| `ExecutionKernel.lease` | Lease a task exactly once; running or uncertain work is never auto-replayed. |
| `ExecutionKernel.start_call` | Persist dispatch intent before side effects; duplicates return their previous record. |
| `ExecutionKernel.receipt` | Commit exact receipt provenance before another provider or cancellation checkpoint. |
| `ExecutionKernel.reject` | Record a trusted compiler rejection before operational dispatch; it can be retried. |
| `ExecutionKernel.finish_task` | Validate an exact evidence quote and receipt class before recording completion. |
| `ExecutionKernel.interrupt` | Keep unacknowledged dispatch uncertain and prohibit automatic retry. |
| `ExecutionKernel.supervisor_view` | Return fixed-count, bounded execution memory rather than the transcript. |
| `ExecutionKernel.executor_view` | Return exactly one task with explicit basis and at most two prior observations. |
| `ExecutionKernel.answer` | Mark a nonoperational explanation distinctly from verified execution. |
| `ExecutionKernel.block` | Record explicit noncompletion without rewriting uncertain task evidence. |
| `ExecutionKernel.complete` | Close only a run with canonical completed tasks and no unresolved work. |
| `ExecutionKernel.close` | Release the database after preserving durable state. |

### rmm_ai_capsules.py

| Symbol | Responsibility |
| --- | --- |
| `decode` | Parse a tool receipt without interpreting its prose as instructions. |
| `validate_execution_scope` | Compile operator-provided allow/exclude lists; prose never grants execution scope. |
| `CapsuleRegistry` | Compile tasks into static tools and mandatory enabled method instructions. |
| `CapsuleRegistry.__init__` | Initialize the documented state contract. |
| `CapsuleRegistry.compile` | Reject incompatible methods, disabled requirements and unscoped network targets. |
| `CapsuleRegistry.backend` | Apply existing declarative metadata guards to code-owned backend operations. |
| `CapsuleRegistry.owner_read` | Resolve one bounded owner-broker read without replaying its originating request. |
| `CapsuleRegistry._relay` | Read an authoritative, live relay owned by the selected session. |
| `CapsuleRegistry._nmap` | Build a bounded command from validated fields and the trusted container. |
| `CapsuleRegistry.dispatch` | Apply capsule/action/target boundaries before the authenticated backend call. |
| `receipt_class` | Distinguish received data, queued work, explicit terminal receipts and errors. |

### rmm_ai_v3.py

| Symbol | Responsibility |
| --- | --- |
| `episode` | Make one provider-native structured call with a fresh, bounded input envelope. |
| `bounded_receipts` | Expose two recent exact previews with explicit truncation and provenance. |
| `read_evidence` | Read receipts only from this attempt or the task's explicitly linked observations. |
| `run_v3` | Run bounded specialist episodes without accumulating provider conversations. |

`WorkspaceService._collection_network` resolves a typed selected-session SOCKS binding from live owner state. `BloodHoundWorkflow.launch`, `_work`, and `collect` carry and revalidate that binding, then wrap collection without a shell. `bloodhound_api` and `execute_workspace_request` forward the owner resolver; existing callers can omit it.

`rmm_ai.legacy_admission` requires explicit legacy execution and rejects scope
fields the legacy loop cannot enforce. Both transport entry points and the
public dispatcher apply it before connecting tools.

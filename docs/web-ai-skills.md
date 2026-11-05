# Web AI playbook execution

The WebUI runs a conversation loop with one explicitly supplied Markdown
playbook and the operator's prompt. Paste the skill body into **AI → Playbook**,
then submit the task in the normal message input. Leave the playbook empty for
ordinary RMM chat. The editor remains in the current page; its contents are not
saved as a separate reusable asset and must be pasted again after a reload.
Accepted background jobs retain their submitted playbook through completion.

Use **Download playbook template** in the Playbook panel to save
`rmm-playbook-template.md`. The static source is
[`web/playbook-template.md`](../web/playbook-template.md), served by both the
central and workspace WebUI. Fill its placeholders, remove unused steps, and
paste the document into the editor. Send the concrete task and input values
in the chat prompt. Downloading does not populate the editor or start a task.
The template defines scope, global rules, numbered phases with completion
evidence, checkpoints, and a final report. Phase headings are organizational:
the entire document still has to fit alongside tool schemas and conversation
state. Automatic phase loading and context-size preflight are not implemented.

## Runtime behavior

Production AI uses the combined RMM and Exegol MCP connections. The system prompt
contains basic operator guidance, selected-session context, and the one supplied
playbook. Available operational schemas are exposed on each provider request.
The model executes tools, receives their results, and continues the conversation
until it reports completion or a concrete blocker. Prerequisite checks alone do
not satisfy a request to perform an action. Reported completion is still a model
judgment, not an independent acceptance validator.

There is no automatic skill selection, dependency resolution, default/core skill
injection, `load_skill`, or `prepare_step` gate in the production chat path.
A leading `/skill-id` has no special loading behavior. Existing Markdown files
in `ai-skills/` remain on disk unchanged and are not read for chat execution.
Legacy skill upload, selection, and enable controls are hidden in the WebUI;
the existing files and backend asset API remain available. Legacy skill helper modules are
retained for compatibility but are not used by the production conversation path.

## API and validation

Both `/api/v1/ai/chat` and `/api/v1/ai/jobs` accept `skill_documents` containing
zero or one object with a nonempty string `body`. The runtime assigns the
canonical ID `playbook`; supplied metadata does not activate dependencies or
policy. Multiple documents return `expected_single_playbook`; malformed bodies
return `invalid_playbook`. Omitted documents mean no playbook. Legacy `skill_ids`
and `skill_policy` do not cause filesystem loading or alter tool availability.

`playbook_documents` validates and normalizes the explicit document without
reading directories. `playbook_system` constructs the pinned system instructions.
The workspace validates the same payload before forwarding it to the central
worker. Authenticated tool APIs retain their authorization and workspace scope.

For example, the relevant fields of a job submission are:

```json
{
  "request_id": "operator-task-001",
  "provider": "openai",
  "messages": [{"role": "user", "content": "Perform the scoped task and verify its result."}],
  "skill_documents": [{"body": "# Playbook\n\nInspect prerequisites, perform the action, and verify completion."}]
}
```

Supply the provider credential through the existing authenticated submission
flow; the example omits credentials. A selected session can be supplied through
`selected_session_id`. Background-job IDs and request IDs track submissions;
they are not proof that a queued agent action completed. Correlate that action
with its session, operation ID, and matching result before dependent work.

MCP supplies tool schemas and executes calls. The conversation loop sends
results back to the model and enforces cancellation and request limits. Neither
MCP nor the loop independently proves that a playbook's objective was achieved.

## Exegol and retained safeguards

Exegol is available to every AI run, independently of playbook contents. There
is no per-run toggle. Legacy disable flags cannot remove it. Workspace-owned
configuration supplies the endpoint and token; browser connection credentials
are not used. Every run requires MCP and a ready owning Exegol runtime. Missing
prerequisites fail explicitly rather than silently using RMM-only tools.

Conversation history, background execution, cancellation, round ceilings,
context budgeting and retrievable evidence remain available. Empty or truncated
provider replies receive bounded recovery and cannot silently complete a task.
No step is automatically replayed by that recovery.

## Context limits and long playbooks

The central worker reads `RMM_AI_CONTEXT_BUDGET` (default 65,536) and
`RMM_AI_OUTPUT_RESERVE` (default 4,096). Admission counts serialized request
bytes as a conservative token upper bound, with additional headroom; this is
not a tokenizer measurement or the provider's advertised model window. The
request includes the complete playbook, tool schemas from both MCP servers,
conversation state, and protocol overhead.

Large tool results are archived and replaced with previews and scoped evidence
references. Older complete conversation groups can also be archived. The model
can retrieve evidence through `read_context`; archiving never replays a tool.
The playbook remains pinned and is not automatically summarized or split.
All retained operator requests also consume context. A long conversation or
large Exegol tool registry can therefore exhaust the budget even with a short
playbook.

Archived AI evidence is stored by the central worker under
`RMM_LOG_DIR/ai-context/<scope>.sqlite3`, separately from workspace-owned raw
artifacts. Scope is derived from the authenticated endpoint, credentials,
workspace headers, and selected session. Evidence IDs alone do not grant
cross-scope access. The directory and databases use owner-only permissions,
but tool results can contain sensitive content; protect this storage like
other operator evidence. Archives can remain retrievable by later tasks with
the same scope. Resetting the browser chat does not delete these databases,
and there is no automatic archive-retention policy. Tool output is evidence,
not a source of new execution instructions.

If admission fails, the task returns `context_budget_exceeded` with content-free
size diagnostics. Shorten repeated instructions, keep raw output in artifacts,
or submit a smaller task with the necessary scope and evidence references.
Adjust the budget only to a size supported by the selected model, leaving room
for output. Increasing the round limit does not increase context capacity.

Numbered phases currently organize prose only. There is no `advance_phase`
tool, automatic phase loader, or independently validated phase state machine.
Checkpoints requested by the template are conversation content; they do not
provide automatic restart or exactly-once execution after interruption.

## Troubleshooting and validation

| Symptom | Meaning and next check |
| --- | --- |
| `expected_single_playbook` / `invalid_playbook` | Submit zero or one document with a nonempty string body. |
| `exegol_requires_mcp` | Install MCP support and enable MCP in the central AI worker settings. |
| `exegol_workspace_not_ready`, `workspace_exegol_not_ready`, or `workspace_ai_context_unavailable` | Check the owning runtime and authenticated workspace relay. |
| `context_budget_exceeded` | Inspect schema, system, and conversation byte measurements; reduce the request or adjust its budget. |
| `empty_provider_response` | The provider repeatedly returned no text or tool calls; inspect retained evidence before resubmitting. |
| `provider_output_truncated` | Bounded recovery failed after an output-limit stop; inspect output counters and the configured reserve. |
| `max_tool_rounds_exceeded` | The conversation reached its request ceiling; completion remains unconfirmed. |

Use `make check-ai`, `make check-parity`, and `make check-web-contracts` with
the configured project interpreter. The template plus real RMM schemas is
tested against the default admission budget for all three providers. This test
does not include a live Exegol registry, whose size depends on the deployment.
Use the [Exegol diagnostic](exegol-mcp-verification.md) for actual connectivity
and tool discovery; mocked regressions do not establish live execution.

Regression coverage includes `tests/test_ai_playbook.py`, central route and
workspace forwarding tests, and the executable WebUI job submission checks.
These tests verify single-playbook injection, absence of skill-directory reads,
MCP dispatch without preparation, and mandatory Exegol availability. The direct
helper is covered for compatibility but is not a production AI fallback.

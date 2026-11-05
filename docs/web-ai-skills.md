# Web AI skills

Skills are server-side workflow instructions for the WebUI agent, independent
of the IDE's skill system. Selection is evaluated for the **immediate step**,
not only for the operator's high-level task.

## Availability and activation

The UI checkboxes determine which optional skills are available. An unchecked
skill's body is absent from the run snapshot and cannot be loaded through
`load_skill` or as a dependency. The two locked core skills,
`operator-boundaries` and `task-completion-report`, enter the initial prompt.
Asset-level disablement still overrides selection. Changes apply to the next
submitted task; an accepted background job retains its original snapshot.
Existing conversation and archived evidence are historical data, not newly
enabled instructions.

All other enabled skills initially contribute their full description and
metadata to a catalog. There are no special eager-loading rules for Exegol,
BloodHound, or other workflows. Enabling a skill does not execute it on every
step: its directives govern steps to which it applies.

Before every batch of operational tools, the model must call `prepare_step`
**alone**, supplying:

- `summary`: a short public explanation of the immediate action and skill choice.
  This is operational rationale, not private model reasoning. Include no secrets.
- `skill_ids`: relevant enabled skills, including prerequisites as applicable.
- `tools`: intended tools for this step.
- `checkpoint`: completed work, pending or uncertain operations, and evidence IDs.
  Include no secrets and never claim a queued command has completed.

The selection request exposes internal preparation tools plus a compact catalog
of operational tool names and bounded descriptions. Complete operational schemas
stay server-side until needed. The runtime resolves enabled dependencies, returns
complete instructions, and requires a later provider round before execution.
That request exposes only the prepared operation schemas plus internal tools;
after the operational batch, the next request returns to preparation-only
schemas. Required-tool validation still checks the complete registry. Preparation is consumed by
one operational batch. Another batch requires another selection. A mixed
preparation/execution batch is rejected. Read-only tools are operational too;
`load_skill` and `read_context` can be used during preparation.

A leading `/skill-id` in the newest user message explicitly invokes that skill.
Its complete instructions and enabled dependencies are attached before the
first provider request, including responses that use no operational tools.
It is also included in each preparation for that submitted task. An unavailable ID
blocks preparation; it never enables a disabled skill.

The WebUI execution panel keeps the public step summary and active skill IDs
visible through subsequent model/tool progress events.
Progress and final checkpoints are retained with the scoped job. Subsequent
chat turns carry the final checkpoint as historical evidence; enabled skills
must be selected again, using the current snapshot and current file contents.

## Files and selection API

Built-in files are immediate `*.md` files in `ai-skills/`. A custom directory
can be set with `RMM_AI_SKILLS_DIR`. Workspace URL-installed files live under
`RMM_WORKSPACE_DATA/assets/skills/` and override built-ins by ID. Uploaded files
must be trusted operator content; they define instructions and tool policy.

`GET /api/v1/ai/skills` returns metadata, never bodies. `skill_ids` on chat/job
submission has these meanings:

- Omitted or `null`: all enabled skills are available.
- `[]`: only `always: true` skills are available.
- A list: those IDs plus enabled `always: true` skills.

Custom `always` skills remain available on demand. Only the two core IDs are
initially injected. The UI persists checkbox selection in workspace UI state.

## Metadata

Existing Markdown files remain compatible. New fields use JSON values on one
frontmatter line; this loader does not implement arbitrary YAML structures.

```markdown
---
id: file-review
title: Review file evidence
description: Use before interpreting file metadata or file contents.
default: false
requires: ["agent-command-dispatch"]
required_tools: ["get_session"]
priority: 10
tool_rules: [{"tool":"inspect_files","requires":["file-review"]}]
---

Follow these instructions when reviewing file evidence...
```

`requires` names enabled dependencies. Missing dependencies, cycles, malformed
metadata, and unavailable required tools fail closed. `priority` defaults to
zero; larger values take precedence for conflicting workflow preferences.
Operator instructions and core boundaries prevail. Same-priority conflicts
must be reported rather than silently choosing one.

Each `tool_rules` entry has one selector, `tool` or `backend: "external"`.
Optional `actions` narrows an action-based tool rule. Live-schema action enums
are validated before dispatch; guarded actions without enums use conservative
whitespace-insensitive, case-insensitive comparison. `requires` means all
listed skills must be active; `requires_any` means at least one must be active.
Multiple matching rules are cumulative. Rules without `actions` also hide a
tool when its prerequisites are disabled. Backend identity comes from MCP
ownership, not tool-name guesses.

Body-free rules are read even from unchecked or asset-disabled files, so
unchecking a skill cannot erase its tool guard. Workspace overlays supply both
documents and policy to the central worker. Both come from the same parsed
file versions; submission does not read the bodies and guards independently. Deleting a file or changing its
rules changes policy and therefore requires the same trust as editing code.

Backend selection uses the same metadata. An enabled external-backend rule
requests the configured Exegol MCP connection; without one, chat remains
available with RMM tools. There are no skill-ID checks in the connection policy.

These guards enforce declared prerequisites, not arbitrary prose semantics.
The agent still decides relevance. It is instructed to select applicable skills
at every step, honor their directives, and report workflow failures instead of
silently substituting another method. Behavioral acceptance needs real model
trials in addition to protocol tests.

## Context budget and evidence

`RMM_AI_CONTEXT_BUDGET` defaults to 65,536 conservative tokens;
`RMM_AI_OUTPUT_RESERVE` defaults to 4,096 and caps provider output. Configure
these within the selected model's actual context and output limits. The input
estimate counts UTF-8 bytes of serialized messages and tool schemas, plus an
overhead allowance, deliberately overestimating ordinary text token counts.
It is a conservative estimate, not an exact provider tokenizer.

The output reserve applies to each provider response, independently of the
160-round task ceiling. Provider output-limit stops trigger up to two bounded
generation retries; the incomplete tool batch is never dispatched and current
preparation remains active. See [output-limit recovery](ai-providers.md#output-limit-recovery)
for reasoning-token accounting, diagnostics, and configuration guidance.

Complete active skill instructions and the latest checkpoint are attached to
every provider request independently of conversation history. They are never
silently truncated. If mandatory content cannot fit, execution stops with
`context_budget_exceeded`; it does not replay completed operations. The final
`context_budget` object records content-free measurements: configured budget,
output reserve, effective input limit, serialized request size, system and
conversation sizes, exposed schema size and tool count. These are conservative
byte-based admission measurements, not a provider-reported context overflow.
The WebUI failure details show the estimate, limit, system and schema sizes.

Large tool results become previews with opaque artifact IDs. Under pressure,
older complete conversation groups are archived while preserving original
operator requests and consent and complete tool-use/result pairs. This is deterministic
history eviction with a structured checkpoint, not a generated narrative
summary. `read_context` retrieves exact evidence in bounded character pages, preserving
Unicode and embedded NULs. New persisted results store separate pages so retrieval
does not load the whole output. Earlier archive schemas remain readable.
Job responses contain an artifact manifest rather than full archived blobs.

Artifacts use unique IDs and owner-only SQLite files under
`RMM_LOG_DIR/ai-context/`. The store scope is derived from authenticated RMM
credentials, endpoint, and canonical selected session. Another workspace or
session cannot retrieve it by supplying an artifact ID. The same scope can
retrieve earlier artifacts on subsequent tasks. Credential rotation changes
scope. Stores are retained as execution evidence; resetting chat does not
purge them. Include this directory in the operator's evidence retention policy.
Provider/tool failures retain a structured, credential-safe diagnostic response
with the latest step state, returned-call references and artifact manifest.
Those references remain usable after reconnects and in subsequent tasks with
the same authenticated scope. A returned call is not proof of remote success.
Background execution still stops on server restart and is never auto-replayed;
this does not guarantee recovery of in-flight output during abrupt process loss.

The BloodHound `bloodhound-queries/catalog.json` remains query guidance used by
its adapter and skill; it is not a skill selector or a context registry.

## Runtime reference

| Component | Responsibility |
|-----------|----------------|
| `prepare_skill_context`, `_tool` | Build the catalog, internal schemas and dispatcher. |
| `StepSkills.__init__`, `require_from_messages`, `activate_explicit` | Snapshot enabled files, versions, guards and explicit invocation. |
| `StepSkills.begin_batch`, `__call__` | Enforce preparation barriers and dispatch declared operations. |
| `StepSkills._resolve`, `available`, `instructions`, `request_tools` | Resolve dependencies, filter tools and rebuild active instructions. |
| `backend_enabled` | Evaluate declarative backend connection requirements. |
| `resolve_ai_skill_snapshot`, `resolve_ai_skill_policy` | Read a consistent instruction/policy snapshot, or standalone body-free policy for legacy supplied documents. |
| `ContextWindow.__init__`, `archive`, `read`, `read_page`, `manifest` | Own the budget and scoped persistent evidence store. |
| `ContextWindow.bounded_result`, `prepare`, `measure` | Bound results and archive complete older history groups. |
| `ContextDispatcher.__init__`, `begin_batch`, `instructions`, `request_tools`, `__call__` | Preserve step gates while paging evidence and bounding output. |
| `_run_provider_loop`, `_run_provider_conversation` | Share budgeting around each provider's native tool protocol. |
| `_message_with_checkpoint` | Carry prior checkpoints as evidence, never as active policy. |

## Adding or updating a skill

Add a Markdown file with a useful activation description, ordered instructions,
and any dependencies or declared tool guards. Enable it in the AI panel. Files
are read on submission, so editing skill files needs no service restart.
Python runtime updates require restarting the affected service. Test both
successful activation and disabled/missing prerequisites. Do not change global
runtime code merely to add another workflow.

## Validation

`make check-ai` runs the AI/provider/context/route suites and UI recovery test.
It is included in `make check` alongside syntax, CLI, parity, WebUI contracts,
and lint. Provider protocol tests use scripted responses; they do not establish
semantic skill-selection quality against a live provider or remote workspace.

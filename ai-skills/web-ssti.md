---
id: web-ssti
title: Server-side template injection investigation
description: Distinguish server template evaluation from reflection or client rendering and validate a minimal template expression in the observed engine context.
default: false
---

# Server-side template injection investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Locate the template boundary

Identify rendering features such as previews, notifications, exports, or
customized documents. Retain a baseline and unique inert marker. Determine
whether user content is template source or a value passed into a template.
The presence of template delimiters in output is not evidence of evaluation.

## Narrow the engine with controls

| Observation | Next experiment |
|-------------|-----------------|
| Delimiter is returned literally | Compare a normal value; evaluate whether this is a template entry at all. |
| Simple arithmetic is evaluated | Change the operands and include a literal control. |
| Template-specific error | Corroborate with another benign syntax distinction or known application code. |
| Evaluation appears only in browser DOM | Compare raw HTTP response; investigate client-side rendering. |
| Blank output | Check context, rendering errors, and asynchronous output before escalating. |

Use small side-effect-free expressions rather than a universal payload list.
For example, a varied arithmetic result is stronger than finding the string
"49" somewhere on a page. Do not identify an engine from a single ambiguous
expression; syntax can be shared across template families.

## Bound the proof

Confirm evaluation in the intended server-rendered artifact, including delayed
mail or export output when relevant. Keep markers unique to the current test.
An expression being evaluated does not establish access to the language
runtime, arbitrary file access, or command execution.

Do not automatically enumerate configuration objects, environment variables,
secrets, or language object graphs after confirming evaluation. If the task
requires a stronger capability proof, identify the exact engine/version and
the minimum additional behavior needed; use `web-rce` for that separate
validation question. Never assume a sandbox is absent from the engine name.

## Interpret and continue

Record the input channel, surrounding template context, engine evidence,
sandbox observations, and rendered result. Distinguish intentional, constrained
template functionality from exposure of capabilities beyond its design.
If only markup changes, report HTML injection rather than claiming SSTI.
If output is asynchronous, inspect the resulting job/artifact before retrying.

## Detection and handoff

Expected evidence includes trigger requests, template/parser errors, and
rendered outputs. Process creation is not expected from arithmetic-only proof.
Link actual application events and timestamps to the marker; report detector
coverage separately. Handoff the smallest control/probe pair and unresolved
engine or permission assumptions, not a preselected exploitation chain.

Adapted from [Claude-Red offensive-ssti](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-ssti/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

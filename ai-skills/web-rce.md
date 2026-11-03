---
id: web-rce
title: Web execution-capability investigation
description: Validate a suspected command or code execution sink using minimal lab proof and distinguish it from reflection, parsing errors, or file inclusion.
default: false
---

# Web execution-capability investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Establish the suspected execution sink

Start with the observed feature and data flow: command wrapper, expression
evaluator, template, serialized object, or uploaded/loaded file.
Record a valid baseline and the intended operation. Do not spray unrelated
execution payloads across inputs without evidence of the relevant sink.

Use the narrower skill first when the evidence points to SSTI,
deserialization, or upload handling. This skill answers whether a confirmed
input boundary can cause server-side execution.

## Choose a discriminating proof

| Situation | Minimal useful observation |
|-----------|----------------------------|
| Output is returned | A unique generated benign value, distinct from literal reflection. |
| Execution is blind | A correlated bounded timing/control experiment or designated lab callback. |
| An asynchronous worker runs it | Matched task identity and completed output. |
| A file is included | Determine interpretation versus plain content retrieval. |
| A command wrapper is used | Establish argument handling and platform before selecting syntax. |

Use only the capability needed for the requested lab proof. A harmless
identity command may establish process context, but should not become a
recursive filesystem or secret inventory. No persistent shell, new account,
agent deployment, or lateral movement follows automatically from confirmation.

## Control false positives

Pair every candidate with an inert/literal control and repeat with a fresh
marker. A returned command string can be reflection; a generic 500 can be
validation failure. One delay can be congestion.
Record which component executed: frontend, application process, job worker,
converter, or an existing operator-side tool.

Do not infer operating system or privileges from a product label. Corroborate
the actual runtime and effective identity only to the extent needed.
If a command wait expires, inspect the existing task rather than replaying
an operation whose side effects may already have occurred.

## Interpret and continue

Report execution capability, effective identity, input constraints, and
required authentication separately. Do not claim full host compromise from
limited expression evaluation or a constrained interpreter.
Additional privilege escalation belongs to its separate skill and task.
Configuration changes and pre-existing file mutations remain governed by
`operator-boundaries`.

## Detection and handoff

Expected evidence may include inbound HTTP, application errors, child-process
creation, or a controlled outbound connection, depending on the actual proof.
Preserve correlated timestamps, request/task IDs, redacted output, and observed
detector results. State missing visibility and the minimum verified capability.

Adapted from [Claude-Red offensive-rce](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-rce/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

---
id: web-sqli
title: SQL injection investigation
description: Investigate suspected SQL injection with controlled response comparisons and database-specific evidence; load for database-backed inputs, not arbitrary web errors.
default: false
---

# SQL injection investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Start from a query hypothesis

Identify one input likely to reach a query: filter, sort key, identifier,
search expression, or a stored value used by a later operation. Retain the
valid request, authenticated role, result semantics, and cache behavior.
Changing an integer to a quote and receiving HTTP 500 is a lead, not proof.

## Choose a discriminating experiment

| Observation | Next check | What it distinguishes |
|-------------|------------|-----------------------|
| Database-specific error | Compare a valid value and a syntax-neutral control | Query parsing versus generic input validation. |
| Stable content difference | Alternate logically opposed conditions with unchanged session/state | Boolean behavior versus cache or authorization variation. |
| No visible result | Use a small repeated timing comparison only if timing is the hypothesis | Backend evaluation versus ordinary network jitter. |
| Input stored before use | Trace the second request and exact stored test marker | Second-order execution versus immediate reflection. |
| JSON/filter syntax | Identify the actual backend/operator language | SQL injection versus a different query-language flaw. |

Change one input and one syntax feature at a time. Use fresh correlation
markers and interleave controls; do not compare a cold baseline with a warmed
probe. Preserve redirect chains and content normalization before comparing.
Choose DB-specific syntax only after corroborating the database family.

## Bound the proof

Prefer a deterministic expression over reading application data. If the task
requires demonstrating disclosure, use a designated lab record with a known
value and stop once the boundary is established. Do not convert discovery
into database dumping, filesystem access, or OS execution automatically.
Inspect an installed tool's current help and test selection before automation;
do not run an unrestricted SQLmap campaign against every parameter.

For time-based tests, record sample count, delays, timeout, and control
distribution. One slow request is inconclusive. Back off when tests cause
errors or queues; a timeout does not establish successful execution.

## Interpret and continue

Record the input context, inferred backend, reproducible control/probe pair,
and what the result proves. A filter accepting unusual syntax may still use a
safe query API. Escaping behavior alone does not prove arbitrary query control.
If a WAF blocks the request, distinguish the edge decision from backend
behavior before loading `web-waf-bypass`.

## Detection and handoff

Expected evidence includes web requests, edge decisions, and possibly database
errors or query telemetry. HTTPS may hide request contents from a passive
sensor. Link actual timestamps/request IDs to observed logs; never infer a
detection from the payload name. Preserve the smallest redacted reproduction,
confirmed scope, untested branches, and the next specific hypothesis.

Adapted from [Claude-Red offensive-sqli](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-sqli/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

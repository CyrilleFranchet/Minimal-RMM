---
id: web-business-logic
title: Application workflow investigation
description: Model business state transitions and test violated invariants with designated lab fixtures; load for workflow bypass, replay, quota, or role-dependent behavior.
default: false
---

# Application workflow investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Write the intended state machine

For one user flow, list states, transitions, permitted actors, preconditions,
and side effects. Examples include draft/submitted/approved or
reserved/confirmed/canceled. Establish the business rule from product behavior,
documentation, or the operator; do not invent a forbidden transition.

Record which server response or read API is authoritative for final state.
UI hiding alone is not server enforcement.

## Pick a single violated assumption

| Axis | Controlled test question |
|------|--------------------------|
| State | Can a known prerequisite be skipped or an expired state reused? |
| Identity | Does a different designated role invoke a restricted transition? |
| Input | Does the server recompute a derived amount, owner, or entitlement? |
| Replay | Does repeating the same operation duplicate a one-time effect? |
| Order | Does reordering two permitted steps violate the invariant? |
| Concurrency | Does simultaneous execution differ from sequential behavior? |

Use small disposable lab fixtures. Record the normal sequence first, then
change one axis. For monetary flows use sandbox values and verify that no
external payment, message, or fulfillment side effect will occur.
Do not treat all unusual application behavior as a security defect.

## Bound the proof

A minimal reproduction should include the starting state, exact transition
sequence, actor, and resulting unauthorized effect. Compare against the normal
control flow with equivalent fixture data. HTTP success without the claimed
state change is not proof.

Test replay only to the bounded extent needed to establish the invariant.
For a concurrency-specific result, load `web-race-condition`.
For a cross-object boundary, load `web-idor`; do not enumerate other
customers merely to demonstrate the same access failure repeatedly.

## Interpret and continue

Check server recalculation, asynchronous reconciliation, idempotency scopes,
role inheritance, and documented exceptions. A temporary UI balance or stale
cache can differ from the settled state. Preserve pending transitions and
inspect them before retrying.
State the impact demonstrated by the fixture, separately from hypothetical
scaled loss; do not multiply an unproven effect into a severity claim.

## Detection and handoff

Traffic may look fully valid at the protocol level. Expected evidence is a
sequence of authenticated requests plus business/audit state transitions.
Correlate actor, object, request IDs, and timestamps. Report which invariant
failed, observed alerts, missing visibility, and the smallest repeatable path.

Adapted from [Claude-Red offensive-business-logic](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-business-logic/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

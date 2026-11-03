---
id: web-race-condition
title: Concurrent workflow investigation
description: Test security-relevant concurrency and TOCTOU behavior with bounded requests and disposable lab state; load for one-time actions or state-transition races.
default: false
---

# Concurrent workflow investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Define the invariant before sending traffic

Identify the state transition and its invariant: one redemption, one active
reservation, one owner, or one increment per accepted operation.
Record initial state, required identity, idempotency keys, and side effects.
Use disposable lab objects and nonfinancial test values; do not discover a
race by repeatedly modifying live pre-existing data.

## Establish sequential controls

Run the normal operation and inspect final state, then test its sequential
duplicate where the task permits. Confirm whether the endpoint promises
idempotency or intentionally supports repetition. An HTTP response count is
not the final business-state count.

If sequential repetition already violates the invariant, investigate it as
business logic first. Load `web-business-logic` rather than assuming a
timing flaw.

## Bound the concurrent experiment

| Step | Evidence to retain |
|------|--------------------|
| Fresh fixture | Initial state and unique object/correlation ID. |
| Small concurrent pair | Exact request bytes, connection layout, send/receive timestamps. |
| Completed processing | Final database/application state or authoritative audit result. |
| Sequential comparison | Same operation and comparable starting state. |
| Limited confirmation | A second isolated fixture when needed for reproducibility. |

Start with two requests, an explicit total-attempt budget, and a deadline.
Synchronization features in an installed proxy may help, but verify its
actual HTTP version and connection behavior before using them.
Stop on unexpected load, unrelated errors, or ambiguous partial completion.
Do not increase concurrency merely because a first attempt was negative.

## Interpret and continue

Eventual consistency, asynchronous retries, client double submission, caching,
and separate idempotency scopes can mimic a race. Wait for authoritative
completion and correlate distinct operations. A pair of HTTP 200 responses
with one effect may be correct idempotent behavior.
A single negative trial cannot establish race freedom.

Preserve partial outcomes instead of repeating timed-out mutations.
Rollback or fixture cleanup follows `operator-boundaries`; deleting
pre-existing records is not part of a reproduction shortcut.

## Detection and handoff

Expected evidence is closely timed authenticated traffic plus state changes;
network traffic alone may not prove the invariant violation. Correlate request
IDs with application/transaction telemetry. Report the demonstrated excess
effect, timing assumptions, attempt budget, and observed detection separately.

Adapted from [Claude-Red offensive-race-condition](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-race-condition/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

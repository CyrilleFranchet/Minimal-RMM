---
id: web-waf-bypass
title: WAF and application normalization comparison
description: Investigate edge-versus-application parsing discrepancies in a lab using controlled equivalent requests; load when an edge filter obscures a specific validated test.
default: false
---

# WAF and application normalization comparison

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Define the lab question

Begin with a known application request and a specific security test that an
edge layer appears to block. Identify the intended policy and observable
application outcome. This skill compares validation behavior; it does not
choose stealth as the objective or rotate sources to avoid monitoring.

Confirm the blocking component through available response/log evidence.
A 403 response or a vendor-looking header is not enough to identify a WAF rule.
Authentication, rate limits, routing, and application validation can look similar.

## Construct a small equivalence matrix

| Variation | Question |
|-----------|----------|
| Canonical allowed request | Does the normal path still work? |
| Canonical blocked request | Is the suspected edge decision reproducible? |
| One encoding/normalization difference | Do the edge and application interpret equivalent content differently? |
| One parameter-location change | Are parsing and policy coverage consistent across accepted locations? |
| One duplicate-value comparison | Do layers disagree on which value is used? |
| Unsupported representation | Is it safely rejected before application interpretation? |

Change one feature at a time. Preserve exact bytes and decoding steps; do not
claim two requests are semantically equivalent until the application evidence
supports it. Use `web-parameter-pollution` for duplicate-value questions.
Avoid indiscriminate vendor payload lists or high-volume obfuscation campaigns.

## Bound the proof

Use an inert marker or the already scoped minimal proof from the relevant
vulnerability skill. A request reaching the origin does not establish that
its payload executed or that a security boundary failed.
Correlate edge and origin observations; where origin logs are unavailable,
state the inference and what would confirm it.

Keep concurrency and source identity stable during comparisons. Source/IP
rotation, disabling rules, and changing protective settings are not automatic
next steps. Configuration changes remain governed by `operator-boundaries`.

## Interpret and continue

Different status/body responses may reflect caching, bot challenges, content
negotiation, or session state. Compare like-for-like valid sessions and timing.
Separate filter acceptance, origin receipt, application interpretation, and
confirmed impact. A blocked probe is useful detection evidence, not a failed
demo that must be made invisible.

## Detection and handoff

Record canonical and varied requests, rule/request IDs when available,
normalization observations, origin evidence, and timestamps. Report actual
alerts alongside expected telemetry. Handoff the smallest policy discrepancy
and its application consequence; do not label reduced alerts as success.

Adapted from [Claude-Red offensive-waf-bypass](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-waf-bypass/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

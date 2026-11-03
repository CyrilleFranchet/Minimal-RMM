---
id: web-parameter-pollution
title: HTTP parameter interpretation investigation
description: Compare duplicate and conflicting parameter handling across web layers; load when gateway, framework, or application precedence may disagree.
default: false
---

# HTTP parameter interpretation investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Identify the parser boundary

Capture a valid request and identify the parameter's intended type and role.
Map query string, form body, JSON object, headers, and cookie handling
separately. Do not infer precedence from framework branding; configuration and
binding APIs can change first-value, last-value, list, or rejection behavior.

## Build a small comparison matrix

| Input | Purpose |
|-------|---------|
| One value A | Baseline interpretation. |
| One value B | Alternate valid interpretation. |
| A then B | Observe duplicate handling. |
| B then A | Identify order dependence. |
| Query A with body B | Test cross-location precedence if both are accepted. |
| Scalar versus explicit list | Identify binding/type differences when the feature supports lists. |

Use harmless distinguishable values in the first experiment. Preserve exact
ordering and bytes: many HTTP clients normalize duplicates before sending.
For JSON duplicates, retain the raw body rather than a dictionary that has
already collapsed keys. Record the response and any observed backend value.

## Select the consequential branch

If both layers consistently choose the same value, duplicates alone are not
a vulnerability. Investigate only where validation, authorization, signatures,
or routing appear to use a different interpretation than the final action.
Use known lab objects and permissions when testing that consequence.

Do not replace a harmless parameter with an administrator/tenant selector
without establishing the relevant access-boundary question. If the result
concerns foreign object access, use `web-idor` with paired identities.
If the mismatch is at an edge filter, use `web-waf-bypass` for controlled
normalization comparison rather than unrestricted payload variation.

## Interpret and continue

Check response reflection against the actual action: the page may echo one
value while the database uses another. Caches may key on a normalized form;
repeat with cache observations and fresh correlation markers.
Different HTTP status codes prove different handling, not necessarily a
security boundary failure. Keep rejected, ignored, merged, and executed
values distinct in the evidence.

## Detection and handoff

Expected telemetry includes raw query/body ordering, gateway normalization,
and application-bound values. Logs may discard duplicate values; record this
visibility limitation. Handoff the minimum request matrix, observed precedence
at each known layer, demonstrated consequence, and any unobserved layers.

Adapted from [Claude-Red offensive-parameter-pollution](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-parameter-pollution/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

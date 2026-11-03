---
id: web-xss
title: Cross-site scripting investigation
description: Test reflected, stored, and DOM XSS by tracing input to browser execution context; load when untrusted content reaches a page or browser sink.
default: false
---

# Cross-site scripting investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Trace source to rendering context

Capture a valid page and identify where a unique inert marker appears:
HTML text, quoted attribute, URL, script literal, JSON, or DOM-only content.
Record whether the value comes from a request, stored object, fragment, or
cross-window message. Reflection alone is not script execution.

## Select the relevant branch

| Evidence | Next step | Proof needed |
|----------|-----------|--------------|
| Marker in server HTML | Inspect the exact surrounding syntax and escaping | A browser execution context reached from that input. |
| Marker only after JavaScript runs | Trace source-to-sink data flow | The sink receives attacker-controlled content. |
| Stored marker on another page | Revisit with a separate designated lab viewer | Persistence plus execution in the viewer's origin. |
| Content renders but script does not | Inspect CSP, sanitization, and sandbox rules | Actual browser enforcement, not assumed bypass. |
| Blind processing suspected | Use a designated callback marker without sensitive content | Correlated execution from the intended rendering context. |

Use an inert DOM change or console marker for bounded execution proof.
Do not collect cookies, tokens, keystrokes, or other users' content merely to
prove JavaScript ran. Test with an isolated lab browser profile and known
accounts. A privileged viewer must be part of the actual demo scope.

## Control the experiment

Keep an escaped/inert variant beside the execution candidate. Compare raw
response bytes with the parsed live DOM; HTML parsing can change structure.
Repeat with a fresh marker and normal page navigation. Record browser version,
CSP headers, iframe sandbox attributes, and whether interaction was required.
A browser extension or developer-console execution is not a target XSS proof.

If a persistent test record is required, use an identifiable agent-created
record. Do not overwrite pre-existing content for convenience. Apply
`operator-boundaries` to cleanup and any configuration changes.

## Interpret and continue

Report the source, sink, victim context, origin, persistence, and prerequisites
separately. HTML injection, open redirect, and XSS are different findings.
Do not claim account takeover from a harmless execution marker. If the
evidence shows template evaluation on the server, load `web-ssti`;
if it only changes navigation, consider `web-open-redirect`.

## Detection and handoff

Server logs can show submitted content but may miss DOM-only fragment input.
Browser console/CSP events and a controlled callback can provide additional
evidence. Keep expected telemetry separate from actual detector findings.
Retain redacted request/response, DOM evidence, marker, timestamps, and the
minimum reproduction; include the rendering conditions that were not tested.

Adapted from [Claude-Red offensive-xss](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-xss/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

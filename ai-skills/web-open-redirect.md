---
id: web-open-redirect
title: Redirect validation investigation
description: Test user-controlled navigation destinations and redirect chains with controlled lab origins; load for return URLs, redirectors, or authentication navigation.
default: false
---

# Redirect validation investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Establish intended navigation

Identify a redirect parameter, path, header-derived destination, or client
navigation sink. Record a normal relative destination, full response chain,
and the expected allowlist. Disable automatic redirect following initially
so each server decision remains visible.

## Compare destination handling

| Candidate | Question |
|-----------|----------|
| Valid relative path | Does ordinary in-application navigation work? |
| Controlled absolute lab URL | Are external origins intentionally allowed? |
| Equivalent URL representations | Do validation and navigation parse the same host/scheme? |
| Controlled redirect chain | Is the destination checked at each relevant hop? |
| Client-side destination | Does the browser actually navigate after script execution? |

Change one representation at a time and record the browser's final parsed
origin. Do not assume the display string is the navigation target.
Keep the callback destination under lab control and use an inert landing page.
Do not send links to other users or collect authentication material as proof.

## Bound the proof

For server redirects, retain status and Location bytes. For JavaScript or
meta refresh, retain the source/sink and actual browser navigation.
A Location value alone may be malformed and rejected by the client.
Compare an allowed and disallowed destination under the same login state.

If the redirect is part of OAuth/SSO, determine whether it can actually affect
a registered redirect URI or token-bearing step. A generic redirect somewhere
on the same domain is not proof of token compromise. Use designated test
identities and report any untested assumptions.

## Interpret and continue

Check explicit user-confirmation interstitials, URL shortener functionality,
same-origin restrictions, and browser normalization. An intended external link
can be a product feature rather than an authorization defect.
A server-side fetch following the redirect is an SSRF question; load
`web-ssrf` only when that behavior is observed.

Distinguish untrusted navigation, header injection, and script execution.
Do not infer all three from one permissive destination parameter.

## Detection and handoff

Expected evidence includes trigger requests, redirect responses, and navigation
to the controlled landing page. Referrer behavior depends on browser policy;
its absence is not proof no navigation occurred. Record actual telemetry and
detector outcomes separately, then hand off a redacted chain with the final
origin, prerequisites, and confirmed impact boundary.

Adapted from [Claude-Red offensive-open-redirect](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-open-redirect/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

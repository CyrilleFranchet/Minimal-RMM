---
id: web-ssrf
title: Server-side request forgery investigation
description: Investigate server-side URL fetching and destination validation using controlled lab endpoints; load for fetchers, previews, imports, or webhook delivery.
default: false
---

# Server-side request forgery investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Establish the fetch boundary

Identify a feature that accepts a URL or resource reference and document its
normal fetch behavior. Record the account, input field, expected scheme,
redirect policy, and whether fetching is synchronous or queued.
Start with a known lab endpoint serving a unique harmless marker.

## Separate possible origins

| Observation | Interpretation to test |
|-------------|------------------------|
| Browser opens the supplied URL | Client-side navigation; not yet server-side fetching. |
| Callback receives correlated HTTP request | A server-side component may have fetched it; identify the component. |
| Only DNS resolution appears | Name resolution occurred; HTTP reachability is not established. |
| Supplied response appears in application output | Fetch and response handling are demonstrated. |
| Timeout or changed error only | Connectivity lead requiring another observation. |

Use a unique identifier for each request and retain callback timestamps,
source address, method, and a minimal header set. Link scanners and security
proxies may also visit URLs; distinguish their traffic from the application.
Do not put credentials or sensitive response content into callback URLs.

## Choose the next validation

Compare an allowed URL with one controlled destination outside the intended
allowlist, using only destinations included in the lab task. If redirects are
relevant, use a controlled redirect chain and identify which hop is checked.
If parsing is relevant, compare normalized host/scheme interpretations rather
than trying a large bypass list. Preserve the exact submitted and resolved URL.

Do not turn URL-fetch confirmation into an internal subnet scan, cloud
credential request, or arbitrary local-file read automatically. Establish the
destination-boundary failure with a designated benign endpoint. Extend to an
internal service only when that is the requested lab scenario.

## Interpret and continue

A trusted public fetcher accepting a public URL can be intended behavior.
State the violated destination or protocol restriction, not merely that a
callback occurred. An allowlisted redirector can create a second-hop issue;
load `web-open-redirect` only if that branch needs investigation.
Treat asynchronous failures as pending until the actual fetch job is observed.

## Detection and handoff

Expected traffic may include DNS and HTTP/TLS from the application host,
rather than the operator's Exegol address. Correlate both the inbound trigger
and outbound fetch with their timestamps. Report confirmed fetch capability,
observed restrictions, unavailable telemetry, and one bounded reproduction.
A detector alert requires independent sensor evidence, not a callback alone.

Adapted from [Claude-Red offensive-ssrf](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-ssrf/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

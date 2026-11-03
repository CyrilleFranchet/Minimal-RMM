---
id: web-request-smuggling
title: HTTP message-boundary investigation
description: Investigate proxy/backend HTTP framing discrepancies on isolated lab connections; load for suspected request desynchronization or protocol downgrades.
default: false
---

# HTTP message-boundary investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Map the actual HTTP chain

Identify the client protocol, frontend, connection reuse, and any downgrade
to a different backend protocol. Use configuration or observed evidence when
available; headers alone do not prove topology.
Retain a normal request/response exchange on a dedicated lab connection.

This class can affect neighboring requests on a shared connection pool.
Use an isolated lab route and test-owned follow-up requests. Do not use other
users' requests as the detection signal or attempt credential interception.

## Choose one framing hypothesis

| Hypothesis | Required distinction |
|------------|----------------------|
| Content length versus transfer coding | Frontend and backend disagree on the body boundary. |
| Duplicate/ambiguous framing field | Layers normalize or reject the field differently. |
| HTTP/2 to HTTP/1 downgrade | Translation produces a different request boundary or authority. |
| Authority/Host discrepancy | Routing interpretation differs; this may be a routing bug rather than smuggling. |
| Connection-close anomaly | Determine whether rejection/closure safely prevents reuse. |

Use an installed raw-capable tool only after confirming it preserves the
intended bytes and HTTP version. Ordinary clients may repair malformed
framing. Record exact wire representation, not only a prettified request.
Do not cycle through every malformed-header variant without a hypothesis.

## Bound the proof

Compare a clean exchange, one ambiguous candidate, and a harmless test-owned
follow-up marker. A delayed response is a lead, not sufficient proof of
desynchronization. Seek repeatable boundary effects in the isolated stream
and, where available, matching frontend/backend logs.

Limit open sockets, request count, and per-connection timeout. Close the test
connection after each experiment and stop if unrelated traffic is affected.
Do not turn confirmation into cache poisoning or response-queue interception.
Those are separate impact questions with different operational consequences.

## Interpret and continue

Different proxies may reject the same input at different stages. Distinguish
safe rejection, ordinary timeout, upstream failure, and a proven boundary
mismatch. HTTP/2 support does not itself imply downgrade vulnerability.
Record whether connection reuse and a specific translation path are required.

## Detection and handoff

Expected telemetry may include edge 400 responses, backend parse errors,
connection resets, and mismatched request counts. TLS capture alone may not
show framing. Retain redacted raw bytes, connection sequence, marker, timestamps,
and actual detector evidence; report uncertain attribution explicitly.

Adapted from [Claude-Red offensive-request-smuggling](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-request-smuggling/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

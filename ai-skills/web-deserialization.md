---
id: web-deserialization
title: Deserialization boundary investigation
description: Investigate untrusted serialized input and object reconstruction with format and dependency evidence; load for opaque cookies, binary bodies, or unsafe unmarshalling sinks.
default: false
---

# Deserialization boundary investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Identify format and reachability

Record the input channel, encoding layers, content type, and valid baseline.
Magic bytes or recognizable object metadata suggest a format but do not prove
that the server deserializes attacker-controlled objects.
Decode copies offline for inspection; do not deserialize unknown samples in
the operator environment merely to identify them.

Distinguish plain JSON data binding from polymorphic object reconstruction.
Record language/framework evidence and any integrity check or signature.

## Select a useful control

| Evidence | Next question |
|----------|---------------|
| Structured serialization signature | Does a benign altered field reach the corresponding parser? |
| Signature/integrity rejection | Is untrusted input rejected before object reconstruction? |
| Type/class error | Which types are accepted, and does the error identify a reachable sink? |
| Format accepted | Is object construction actually security-sensitive? |
| Candidate library chain | Are the required versions/classes and invocation conditions present? |

Preserve exact byte lengths, encoding, and compression. A Base64 mutation
that only breaks decoding says nothing about object-instantiation behavior.
Do not select a gadget chain merely because a framework name appears in a
header; dependency and sink compatibility must be established.

## Bound the proof

Prefer parser/type-boundary evidence or a harmless designated marker
appropriate to the confirmed sink. If execution proof is required, restrict
it to the smallest lab observation and use existing validated tooling.
Do not automatically invoke a broad gadget generator, external callbacks,
reverse shell, or credential access after recognizing a format.

An available gadget library does not prove the application is exploitable:
type allowlists, signatures, class loading, runtime restrictions, and code
paths can prevent the chain. Record each prerequisite as confirmed or unknown.

## Interpret and continue

Separate accepted serialized data, unsafe type selection, reachable gadget
behavior, and code execution. A timing change or error alone is inconclusive.
For XML-based parser behavior, load `web-xxe`; for a separately justified
code-execution proof, load `web-rce`. Do not assume the stronger outcome
from the weaker finding.

## Detection and handoff

Expected evidence includes encoded request bodies, parser/class errors, and
application events. Process or network telemetry is expected only if the proof
actually invokes those behaviors. Preserve hashes and sanitized parser evidence,
dependency assumptions, exact control/probe bytes, and detector observations.

Adapted from [Claude-Red offensive-deserialization](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-deserialization/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

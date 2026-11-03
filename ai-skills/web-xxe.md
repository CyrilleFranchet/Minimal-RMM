---
id: web-xxe
title: XML parser and external-entity investigation
description: Investigate XML entity handling, external resolution, and parser boundaries in XML requests or document processing without automatic file disclosure.
default: false
---

# XML parser and external-entity investigation

Use existing tools in the verified Exegol workspace through its MCP tools.
Apply `operator-boundaries`; loading this skill does not expand the task.
Do not install tools. Keep raw credentials out of commands shown in reports.

## Identify actual XML processing

Find a request or document type that the application genuinely parses as XML.
Preserve a valid baseline, content type, character encoding, and schema shape.
Changing a JSON content type to XML and getting an error does not establish
a reachable XML parser. For archives/documents, identify the member parsed.

## Select a controlled parser test

| Question | Evidence to seek |
|----------|------------------|
| Does the parser accept a DTD? | A benign internal entity expands in a known output position. |
| Does it resolve external entities? | A unique controlled resource is fetched and correlated. |
| Does it process XInclude? | A separately designed XInclude test changes the intended output. |
| Is processing blind or queued? | A matched job result or callback rather than HTTP timing alone. |
| Does a different document converter parse the content? | Converter-specific logs or output tied to the submitted artifact. |

Keep internal-entity support separate from external-resource resolution.
Test one parser feature at a time with tiny documents and ordinary resource
sizes. Never use recursive expansion or resource-exhaustion documents as a
discovery shortcut; they answer an availability question, not file-access proof.

## Bound the proof

Use a harmless marker served by a designated lab endpoint. DNS-only evidence
does not establish successful HTTP fetching or file disclosure. If local-file
reading is explicitly part of the lab task, prefer a designated non-sensitive
fixture and establish which service account/parser performed the read.
Do not follow a positive result with credential-file retrieval automatically.

For document uploads, retain the source hash and exact modified member.
Keep a valid document control to distinguish entity handling from a generic
converter failure. Allow asynchronous processing to finish before repeating.

## Interpret and continue

DTD rejection, schema rejection, network denial, and unsupported entity syntax
are different outcomes. A callback may originate from an antivirus scanner
rather than the target parser; check timing and origin before attributing it.
Load `web-ssrf` only when the next question concerns destination controls.
Do not infer remote code execution from XXE without a separate proven chain.

## Detection and handoff

Expected evidence can include XML parser errors, upload/conversion jobs, and
outbound DNS or HTTP from a processing worker. Correlate the request, artifact,
worker, and callback. Record observed telemetry separately from expected alerts,
and report the exact parser capability established plus unresolved coverage.

Adapted from [Claude-Red offensive-xxe](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/web/offensive-xxe/SKILL.md) at revision
`a8de813edc9f66f60d44cfe42d47750e89d9930b`; see [adaptation notes](../docs/claude-red-adaptations.md)
and the [upstream license](../third_party/claude-red/LICENSE).

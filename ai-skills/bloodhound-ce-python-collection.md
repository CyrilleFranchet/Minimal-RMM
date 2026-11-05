---
id: bloodhound-ce-python-collection
title: BloodHound CE Python collector (bloodhound-ce-python) collection
description: Collect AD data with the official BloodHound CE Python collector (bloodhound-ce-python) in Exegol using DCOnly, import it into local BloodHound CE, and verify the dataset. Use for acquisition or refresh; use bloodhound-analysis for questions about existing graph data. Not the legacy bloodhound-python tool.
default: false
tool_rules: [{"tool":"bloodhound","requires_any":["bloodhound-analysis","bloodhound-ce-python-collection"]},{"tool":"bloodhound","actions":["create","collect","import","verify"],"requires":["bloodhound-ce-python-collection"]},{"tool":"bloodhound","actions":["collect"],"requires":["exegol-rmm-pivot"]}]
priority: 30
---

# BloodHound CE Python collector collection

This is the **BloodHound CE Python collector** method: the installed executable
must be `bloodhound-ce-python` (the official CE collector, not the legacy
`bloodhound-python` tool), producing supported CE version-6 artifacts.
Use the `bloodhound` adapter at the owning Exegol workspace; never send the
collector command to a Windows agent. `operator-boundaries` applies.

The collection method is fixed to **DCOnly**. Do not substitute the legacy
`bloodhound-python`, SharpHound, `All`, or another collection mode. **ldeep is
a planned second collector, not an implemented fallback.** Its output must not
be submitted to this adapter as if it were a Python BloodHound artifact.

## Decide whether acquisition is needed

Use an existing verified run when its scope, age, and coverage answer the
question. A request to analyze an existing graph does not request a new scan.
For current-token questions, load `windows-identity-and-groups` instead of
collecting the directory. This skill takes precedence over the general
ADWS-first preference when Python BloodHound collection is requested.

Before acquisition, record the requested domain, collection purpose, expected
coverage, and existing run IDs. The `exegol-rmm-pivot` skill must be enabled for acquisition; establish
or reuse SOCKS5 or TUN before contacting target resources. A connected MCP server or live agent does not prove that Exegol can reach
the DC or resolve its domain. Check those prerequisites in the owner's network
namespace, not on the central server or operator laptop.

## Prepare the local service and credentials

1. Call `bloodhound(action="health")`. For startup, call `ensure` once and poll
   `health`. The runtime owns Exegol identity checks and the installed CE
   launcher. Do not launch another instance through a separate MCP call.
2. If startup is `uncertain`, inspect its recorded outcome and current health;
   never replay a launch with an unknown outcome. Configuration and identity
   failures do not justify selecting another container.
3. Once ready, call `preflight`. Inspect individual collector, API, and cache
   flags, not only the wrapper's `ok`. A configured cache is not yet a validated
   ticket; an available API does not prove directory connectivity.
4. Use an existing matching Kerberos **file** cache. The owner service expects
   `KRB5CCNAME` to contain the raw cache path, not a `FILE:` URI. Do not acquire
   passwords, install tools, reset CE, or change credentials to satisfy this
   workflow. Missing API credentials and HTTP 401/403 are configuration blockers.

## Create one acquisition run

Establish the domain DNS name, DC FQDN, DNS server IP, and collection username
from verified context. The current adapter also requires exact source and
target SIDs **when creating a collection run** because ingestion verification
checks those identities. This is an API limitation, not a requirement to
analyze a path during collection. Do not invent SIDs or equate the collection
account with the eventual path starting identity.

Call `bloodhound(action="create", options={"domain": ..., "dc": ...,
"nameserver": ..., "username": ..., "source_sid": ..., "target_sid": ...})`.
Retain the returned `run.id`. A new ID represents another acquisition, not a
retry. After a lost response, inspect prior results before creating a new run.

For centrally hosted AI, an action may first return `workspace_request_id`.
Poll `bloodhound(action="result", run_id=<request ID>)` for the owner's reply;
do not resubmit the action. That transport ID is distinct from `run.id`. After
a central restart or unknown request, inspect the local workspace's cached
reply and artifacts before replacing work.

## Collect, ingest, and verify

| Phase | Action with the same `run_id` | Completion evidence |
|-------|-------------------------------|---------------------|
| Collect | `collect`, then bounded `status` checks | Collection complete, artifact hashes/counts, collector fingerprint, requested coverage, and local diagnostics path. |
| Ingest | `import`, then bounded `status` checks | `state=ingested`; upload acceptance alone is insufficient. |
| Verify | `verify` | Expected domain and endpoint identities occur in the current artifacts and in the graph. |

Collection and ingestion are asynchronous. Preserve the run ID across waits;
do not spend the entire conversation repeatedly polling. For an uncertain
phase, retain the ingestion job ID and inspect status and artifacts. A known
finalization ambiguity can be reconciled by `status` without another upload.
Other uncertain outcomes require inspection, not automatic collection replay.

Partial ingestion, missing objects, malformed files, or unsupported responses
are not success. Do not clear the graph to solve stale data: CE merges data
across imports, so older relationships may remain after verification passes.

## Handoff to analysis

Return the `report` and a compact acquisition record: run ID, domain, source
and target SIDs, collector identity/version evidence, method **DCOnly**,
timestamps, observed CE version, hashes/counts, ingestion/verification status,
and any uncertainty. Keep raw directory artifacts local and secrets out of
chat. Load `bloodhound-analysis` only when interpretation is requested.

DCOnly supplies directory-oriented Group, ACL, Trusts, ObjectProps, and
Container coverage; it does not establish live sessions or host-local admin
membership. Collection creates network activity; ingestion and graph analysis
are separate phases. Expected directory/authentication traffic is a hypothesis
until independently observed. This adapter does not query a sensor, so its
detection result remains `not_verified`.

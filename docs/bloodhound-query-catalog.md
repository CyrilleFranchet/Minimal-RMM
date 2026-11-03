# BloodHound query scope and provenance

The maintained [catalog](../bloodhound-queries/catalog.json) contains sixteen
read-only graph templates. The adapter renders only validated domain/SID
parameters and returns nodes or paths, not arbitrary tabular Cypher output.
Every template has bounded results; variable-length paths also have a hop limit.
These bounds constrain output and traversal depth, not a guaranteed runtime.

## Complete reference library and selective execution

The separate [upstream snapshot](../bloodhound-queries/upstream-library.json)
preserves all **236** official queries at revision
`b1fee8f5715f8fb3358e187ca3f1bddbfd125f93`, including their original Cypher,
metadata, raw YAML, source path/URL, and file checksum. Importing the complete
library does not expand the sixteen-template execution allowlist.

- `bloodhound(action="library_search", options={"text":"delegation","limit":10})`
  returns compact metadata with pagination. Optional `platform` and `category`
  filters use exact case-insensitive values. Search pages contain no Cypher bodies.
- `bloodhound(action="library_get", options={"id":"<upstream GUID>"})` retrieves
  one original query and its compatibility notes.
- `bloodhound(action="queries")` lists executable local templates and includes
  a `reference_library` pointer with snapshot revision/count and discovery actions.

CLI equivalents use `bloodhound library_search --options-json
'{"text":"delegation","limit":10}'` and `bloodhound library_get --options-json
'{"id":"<upstream GUID>"}'`. The same syntax works in the standalone interactive
shell and WebUI shell, where it is intercepted as an operator action and never
forwarded to an agent. The WebUI's **BloodHound operations** form offers the
same action/options contract without requiring an agent session.
REST uses the existing authenticated `POST /api/v1/bloodhound` route.
Workspace mode relays discovery to the owning workspace; these read-only
operations do not create durable relay journals, and completed relay replies
expire after 30 seconds.

All original entries have `upstream_executable: false`. Exact pinned source
links associate some entries with `local_template_ids`; these are adaptations,
not approval to execute the unchanged original. Other entries remain
`reference_only`. Compatibility hints identify likely coverage concerns such
as Azure, certificates, host sessions, or local administration. They are
inferred review aids, not an authoritative dependency analysis or proof that
the installed CE supports the original. Unknown requirements remain unverified.

The agent searches by its current question and retrieves selected entries.
Neither the full library nor its Cypher is inserted into the initial prompt.
Original GUIDs and raw Cypher cannot be submitted as executable query templates.
To enable a new query, add a reviewed local template with bounded output,
validated parameters, graph-return compatibility, and collection requirements.

## Reproducing or updating the snapshot

`scripts/import_bloodhound_library.py` reads a local clean checkout of the
official repository. It performs no network requests or installations. Pass
`--source /path/to/BloodHoundQueryLibrary --output bloodhound-queries/upstream-library.json`.
The development interpreter needs PyYAML; the running service uses only JSON
and the standard library. The importer records the Git revision, validates the
tracked query inventory and unique IDs, and refuses malformed inputs rather
than silently dropping entries. Review the resulting diff and licensing before
accepting an update; no background updater follows upstream changes.
`--revision` defaults to the pinned revision above; supply the full reviewed
commit SHA when intentionally updating to a different revision.

`rmm_bloodhound_library.search_library` validates bounded search/filter/paging
options; `get_library_entry` validates an exact catalog ID and returns one
reference; `library_summary` exposes only snapshot identity and discovery
actions. Compatibility metadata is derived from the pinned sources and local
allowlist, never from executing a query. Importer and discovery tests cover
lossless import, provenance, pagination, filtering, mapping, and refusal paths.
Internally `_read_json` and `_documents` read/validate fixed local files;
`_metadata` and `_compatibility` build compact descriptors and review hints.
The importer uses `git` for local tracked-file reads, `parse_entry` with
`UniqueLoader`/`unique_mapping` to reject duplicate YAML keys, `build_snapshot`
for complete inventory validation, `write_snapshot` for deterministic atomic
output, and `main` for CLI validation. No source code or hooks are executed.

## Questions covered

| Family | Query IDs | Evidence boundary |
|--------|-----------|-------------------|
| Identity and path | `domain`, `identity`, `memberships`, `candidate_path` | Exact identity, bounded memberships, or one candidate path. |
| Privileged membership | `domain_admin_groups`, `domain_admin_members` | RID-based group selection avoids localized display names. |
| Account properties | `kerberoastable_users`, `asrep_roastable_users` | Enabled accounts with relevant directory flags; no authentication attack is executed. |
| Delegation | `unconstrained_delegation_computers`, `constrained_delegation`, `resource_based_constrained_delegation` | Stored configuration, not credentials, live sessions, or successful impersonation. |
| Object control | `outbound_object_control`, `inbound_object_control`, `gpo_control` | Selected direct permissions; effective rights and GPO application require additional evidence. |
| Replication | `replication_rights_candidates` | DCSync or relevant broad domain-control edges; not proof of executed replication. |
| Domain boundary | `foreign_group_memberships` | Direct cross-domain membership, not a complete trust analysis. |

Use `bloodhound(action="queries")` to obtain exact parameters, required
properties/edges, collection methods, interpretations, limits, and source URLs.
Domain-scoped templates use the recorded domain property; incomplete metadata
can omit objects. SID-scoped templates inspect the explicit identity.

## Source choices

The [SpecterOps query library](https://queries.specterops.io/) is the primary
template reference. Adaptations are pinned to
[BloodHoundQueryLibrary b1fee8f](https://github.com/SpecterOps/BloodHoundQueryLibrary/tree/b1fee8f5715f8fb3358e187ca3f1bddbfd125f93),
with per-query source links. The
[MIT notice](../third_party/bloodhound-query-library/LICENSE) is preserved.
Changes narrow scope, cap results/hops, and retain graph-compatible returns.
No upstream installer or query was run against the live lab.

The [PIIRATES article](https://www.piirates.fr/bloodhound/) helps identify useful
investigation questions, but mixes legacy GUI/Neo4j guidance with broad or
scalar-returning examples. Its query text is not used as the CE API contract.
Unbounded shortest paths and name-based assumptions were not imported.

Collector coverage was checked against
[Python BloodHound CE f7c0144](https://github.com/dirkjanm/BloodHound.py/tree/f7c0144902f8cacaf16af7beaf1064ee79d0c56b).
That collector emits the relevant account and delegation properties through
LDAP-oriented collection. It does not emit `isdc` in computer properties;
the unconstrained-delegation query therefore does not assume missing `isdc`
means a non-DC. Results include DC candidates requiring separate classification.

## Deferred questions

`deferred_queries` contains descriptions and blockers, never executable Cypher.
It is a capability backlog, not a queue: nothing is scheduled or retried when
collection completes. The API exposes these alongside active queries;
rendering their IDs fails.

- **Local token/user-right privileges:**
  [PR #1999](https://github.com/SpecterOps/BloodHound/pull/1999) was closed without
  merging on October 10, 2026. The proposed privilege edge set is absent from
  the reviewed [v9.9.0 AD schema](https://github.com/SpecterOps/BloodHound/blob/v9.9.0/packages/go/graphschema/ad/ad.go).
  The current DCOnly method also lacks the required host-local collection.
- **Sessions and local administrators:** require collection beyond DCOnly.
- **ADCS paths:** require supported certificate-service collection, schema,
  and post-processing that this first collector slice does not establish.

An existing graph may contain these records from other imports. That does not
make them attributable to the current run or authorize a new collection method.

## Adapter safeguards and validation

`BloodHoundWorkflow.query` refuses execution when `required_collection` is not
a subset of the run's `coverage`. That field records requested methods, not
per-property completeness. A matching method list is necessary but insufficient
to establish comprehensive collection; missing fields can still hide findings.

Each evidence artifact and summary retains the catalog hash, sources,
interpretation, required properties/edges/methods, and coverage/provenance
warnings. Empty output is labeled `no_matches_in_query_scope`. Graph contents
remain instance-wide and may include older data. The DCSync interpretation
does not equate a lone GetChangesAll right with the combined replication rights;
see [SpecterOps DCSync semantics](https://bloodhound.specterops.io/resources/edges/dc-sync).

Tests cover rendering, parameter rejection, bounded read-only templates,
metadata, non-executable deferred entries, pre-network coverage rejection,
and evidence persistence. These are contract tests, not live CE query-engine
validation. Before the demo, execute relevant templates against the deployed
CE version and compare results to known lab objects and permissions.

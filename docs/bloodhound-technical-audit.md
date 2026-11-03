# BloodHound technical and operator parity audit

Audit date: November 3, 2026. This review covers the BloodHound workflow,
reference library, Exegol lifecycle, and their operator surfaces. It does not
certify that every unrelated project feature has a dedicated manual WebUI control.

## Unified workspace follow-up

The repeat failure-path audit also corrected four defects:

- Workspace child processes inherited central-only credentials. The deployment
  launcher now filters known settings by service scope and clears internal relay flags.
- Relay intent files were flushed without persisting their directory entries.
  `rmm_bloodhound_broker.sync_directory` now flushes those entries before dispatch
  and after publishing completed results.
- Replaying a final upload chunk after an interrupted receipt commit could replace
  the complete artifact with its tail. Staging now survives until the receipt,
  transcript, and download index commit; missing preceding bytes cause an error.
  `WorkspaceStore._upload_staging_path` resolves the session-scoped staging file.
- CLI connection diagnostics contaminated JSON stdout. They now go to stderr.
  A real-process smoke test checks single-preset startup, workspace authentication,
  administrator-token rejection, CLI JSON output, and supervisor shutdown.

The subsequent workspace architecture audit found additional issues beyond
the original BloodHound-only scope:

- CLI and external MCP defaults still selected the central port instead of
  workspace port 8787; they now default to the workspace, respecting explicit URLs.
- Central Web AI could target central storage routes for workspace-owned tools.
  An authenticated, allowlisted JSON relay now executes those operations at
  the workspace owner. See [routing and parity scope](mcp-parity.md).
- A saved nondefault central listener was not reflected in generated workspace
  connection settings. Preparation now derives its default URL from that listener.
- Restarted central event streams and migrated transcript IDs could collide.
  Epoch-aware synchronization and workspace-local ID mapping preserve history;
  see [stream continuity](workspace-deployment.md).
- Current startup, assets, UI-state, embeddings, and architecture documentation
  still described retired server-owned operation; these references were corrected.

The test counts below describe the earlier BloodHound verification checkpoint.
Current unified-workspace verification results are recorded in
[the progress log](progress.md); they do not imply live Exegol validation.

## Findings corrected

- Interactive CLI and WebUI shell BloodHound commands could fall through to
  the target agent. Both now intercept valid and invalid BloodHound syntax.
- The manual WebUI lacked access to the full workflow. Its BloodHound operations
  form now exposes all fifteen actions without an agent selection or AI.
  Disconnects cancel pending form requests and suppress stale responses.
- Exegol readiness survived owner configuration changes. Verification is now
  bound to the MCP endpoint/token/container and CE endpoint, with rechecks
  during startup and before readiness publication.
- CE ingestion could be accepted despite partially failed files. Both complete
  and partial file failure counters now gate successful ingestion.
- Falsy non-object API options and non-string actions now fail validation.
  Malformed library metadata produces a controlled configuration error;
  upstream GUID lookup accepts either letter case.
- Documentation now reflects owner-authoritative Exegol settings, on-demand
  skills, reference-only upstream queries, and the deferred capability backlog.
- The repeat audit found read-only relay replies expiring from submission time.
  Their thirty-second retention now starts at completion, preserving slow replies.

## Operator matrix

| Capability | CLI | Manual WebUI | MCP / Web AI |
|------------|-----|--------------|--------------|
| All fifteen BloodHound actions | One-shot and interactive commands | Operations form and intercepted shell command | Shared `bloodhound` tool |
| Library search and selected query retrieval | JSON options | JSON options | Structured options |
| Create, collect, import, verify, query, report | Same owner API | Same owner API | Same owner API |
| Async run and relay inspection | Explicit status/result actions | Explicit status/result actions | Explicit status/result actions |
| Open CE interface | Returns proxy URL | Convenience button opens new tab | Returns proxy URL |

Action-set checks compare CLI choices, browser actions, the AI tool schema,
and the workspace relay. Browser tests execute the production dispatch code;
HTTP tests exercise real localhost requests through the API client and route.
No mutation is retried automatically by the new manual controls.

## Evidence and remaining validation

The unified-workspace audit subsequently passed all 432 Python tests with no
skips, including a localhost owner-relay round trip and event restart/migration
continuity tests. `make check`, AI job UI recovery, and 20 plugin UI tests passed.
Current documentation link validation found no broken links among 274 relative
file links across 78 Markdown files. These checks do not replace live deployment
validation.

The repeat verification ran 384 Python tests: all passed, with no skips.
MCP 1.30.0 and its dependencies were installed from the local package cache
into `/private/tmp/minimal-rmm-mcp-verification`, leaving the project interpreter
unchanged. Setting `PYTHONPATH` to that directory enabled the actual registered
MCP tool invocation test, which passed with a mocked downstream API client.
This verifies SDK registration and dispatch, not a live Exegol MCP connection.

`make check` passed, including operator parity, WebUI contracts, BloodHound
browser and shell tests, and Markdown/YAML lint. AI job UI recovery tests and
`git diff --check` also passed.

Pinned CE v9.9.0 source was checked for graph response shape, identifiers,
upload contracts, ingestion statuses, and catalog relationship names. The
pinned Python collector source was checked for DCOnly methods and CE format
version 6. The repeat review checked 267 relative file links across 77 Markdown
files, with no broken links, and confirmed the 16 executable templates,
three deferred entries, and all 236 upstream source checksums.

No live Exegol, Ludus collection, or CE query execution was performed. Before
the demo, validate MCP connection and namespace identity, CE startup, browser
login/proxy behavior, DCOnly collection, ingestion counters, and the relevant
templates against known lab objects. Schema/source compatibility alone does
not establish query-engine behavior or collection completeness.

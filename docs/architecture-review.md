# Review Summary

Review date: November 3, 2026. Baseline: `d47fdaa` on `v2`.
This is a risk-focused review of the recent architecture changes, not a claim
that every project file or deployment scenario is correct. See the
[documentation index](README.md) and [operator surface detail](operator-surface-review.md).

## Verdict

**Ready with follow-ups for the authorized lab**, subject to the remaining live
validation below. The concrete defects listed here have fixes and regression
coverage in the working tree; the deployed Ubuntu/Exegol environment was not changed.
The repository should not be treated as a hostile multi-tenant security boundary.

## Blocking Issues

No reproduced high-severity finding remains unresolved in this review's scope.
The following issues were fixed before this verdict:

| Severity | Area | Defect and correction | Evidence |
| --- | --- | --- | --- |
| High | Workspace authorization | Workspace credentials could open the unfiltered central WebSocket feed. Reject before handshake; administrators retain their feed. | `test_server_workspace.py` |
| High | Storage authorization | Direct scoped requests could delete central history or mutate central assets. Reject these routes after the authenticated owner relay, keeping workspace publication separate. | `test_server_workspace.py` |
| High | Deployment identity | An inherited workspace ID could start one identity against another owner's prepared directory. Reject mismatched overrides before launching processes. | `test_deployment.py` |
| High | AI cancellation | A canceled job could admit another job for the same owner/session while its tool was still executing. Retain the in-memory reservation until the worker exits. | `test_ai_jobs.py` |
| High | CLI dispatch | The subcommand and command payload shared a parser destination; file-based exec entered interactive mode and a payload named health invoked the wrong handler. Separate routing from payload arguments. | `test_cli_dispatch.py` |
| High | Browser ownership | Old-login responses could repopulate session data or log out a newer connection. Discard results using an authentication generation, including during body consumption. | `test_workspace_access_ui.cjs` |
| Medium | Plugin recovery | Synchronization ran only once and could fail before central was ready. Reconcile the owner cache on a separate retrying worker; send only changed/missing bytes and revoke stale entries. | `test_workspace_plugin_sync.py` |
| Medium | Plugin mutation races | Concurrent publication and deletion could reorder updates; caller metadata could be stale. Serialize supported plugin mutations and publish current authoritative metadata. | `test_workspace_plugin_sync.py` |
| Medium | Plugin aliases | Mixed-case asset-kind aliases could bypass central plugin rejection or skip workspace publication. Normalize guards and use the canonical stored kind. | `test_server_rmm_plugins.py`, `test_workspace_plugin_sync.py` |
| Medium | Shutdown | The relay worker could still access SQLite after a two-second join timeout closed the store. Wait for it before closing storage. The deployment supervisor retains its overall shutdown deadline. | `test_workspace_shutdown.py` |
| Medium | SQLite migration | A hot rollback journal was omitted from the temporary clone, making a recoverable crashed database fail migration. Copy rollback journals as well as WAL files before SQLite backup. | `test_workspace_migration.py` |
| Medium | Workspace registry | Duplicate normalized workspace IDs silently replaced credentials. Reject duplicates in object and list configuration inputs. | `test_rmm_settings.py` |
| Medium | Event continuity | Global cursor gaps caused false loss warnings between unrelated workspaces, while explicit queue clearing was not tracked. Use a per-owner discarded-event watermark. | `test_workspace.py` |
| Medium | Session lookup | Another workspace's matching ID prefix caused false ambiguity. Filter by ownership before resolving prefixes. | `test_server_workspace.py` |
| Medium | MCP result handling | Structured-only SDK results were lost because the wrong attribute name was used. Read the SDK's structuredContent field. | `test_mcp_result_encoding.py` |
| Medium | Browser launch lifecycle | Delayed BloodHound replies could navigate an old-owner launch ticket after disconnect. Recheck cancellation and the originating generation after awaits. | `test_bloodhound_ui.cjs` |
| Medium | Diagnostics and compatibility | Exec progress corrupted JSON stdout; runtime exception classification required a Python 3.11 builtin despite documented 3.10 support. Move progress to stderr and accept nested backport exception shapes. | `test_cli_dispatch.py`, `test_exegol_runtime.py` |

## Non-Blocking Issues

- **Medium, deployment validation:** actual Exegol MCP reachability, container
  discovery, CE startup/import/query, and browser proxy behavior have not been
  exercised on the Ubuntu host. Run those before relying on the demonstration.
- **Medium, trust model:** agent downloads use the deployment-wide beacon secret
  and registered session ID. Filename partitioning does not stop a holder of the
  shared secret from impersonating another known session. Per-agent identity is
  a separate protocol change, not silently added by this review.
- **Medium, evidence durability:** central event queues remain in memory. A crash
  before workspace synchronization can lose events; epoch and overflow warnings
  expose the gap but cannot reconstruct it.
- **Medium, process ownership:** use one workspace process per identity/data
  directory and one AI job manager per database. In-process synchronization does
  not provide distributed locking across duplicated services.
- **Low, compatibility evidence:** tests ran under the configured Python 3.12 SDK.
  Backport exception behavior is covered, but this is not a full Python 3.10 run.

## Test Assessment

The pre-review checkpoint had 446 passing Python tests. The integrated suite now
has 468 passing tests with the isolated MCP dependency set, including real local
HTTP listeners and deployment subprocesses. New tests focus on owner boundaries,
observable dispatch, concurrency, cancellation, shutdown, and migration recovery.
They do not merely check that private helper calls mirror their implementation.

Plugin reconciliation also rejects corrupt local inventory metadata before making
cache changes, preventing a read failure from being mistaken for an empty inventory.
The regression verifies that existing central publications remain intact.

`make check`, the additional AI-job and plugin-release JavaScript suites, Compose
configuration validation, entrypoint shell syntax, and whitespace checks passed.
All 286 relative file links checked across 84 Markdown files resolve; external URLs
and section anchors were not validated.

The surface checks cover 55 MCP tools, six WebUI shell meta commands, BloodHound
launch behavior, and workspace access. They establish contracts and routing,
not a dedicated graphical control for every CLI operation. The raw BloodHound
operations form remains intentionally removed.

JavaScript tests execute production functions under Node with controlled responses;
they are not browser rendering or real browser end-to-end tests. No Windows agent,
Ludus collection, PCAP capture, or live CE operation was executed in this review.
Go binaries and provider installations were not changed or rebuilt.

## Follow-Up Tickets

No external tickets were created.

| Title | Priority | Risk and acceptance evidence |
| --- | --- | --- |
| Validate the actual lab deployment | High | Confirm authenticated workspace login, Exegol MCP ownership check, CE proxy access, DCOnly collection/import, and one known graph query on the deployed environment. |
| Introduce per-agent authentication | Medium | Replace shared-secret session impersonation with independently authenticated agent identity while preserving beacon compatibility. |
| Define durable central event delivery | Medium | Specify bounded persisted queues, acknowledgement, restart replay, and overflow policy before implementing stronger evidence guarantees. |
| Add browser and compatibility CI | Low | Real browser coverage for login switching and connection panels; explicit supported Python-version matrix. |

## Final Recommendation

Review and commit the fixes as a coherent architecture-hardening change, then
validate the actual lab deployment. Do not interpret local passing tests as proof
that the host's MCP bind address, Exegol image dependencies, or CE data paths are
correct. Preserve the documented migration and shared-secret limitations.

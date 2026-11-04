# Review Summary

Review date: November 3, 2026. This second pass covers Minimal-RMM,
Minimal-RMM-profiles, and Minimal-RMM-plugins, including the uncommitted fixes
from the [first architecture review](architecture-review.md). The review combines
source inspection, contract checks, regression tests, and local integration tests.
It is not a claim that every execution path or Windows dependency was exercised.

## Verdict

**Ready with follow-ups for source review.** Reproduced defects were corrected.
Deployment acceptance still requires the actual Exegol/Windows lab and rebuilt
plugin artifacts. No remote deployment, publication, commit, or push was performed.

## Blocking Issues

No reproduced finding remains unresolved in the reviewed scope. Fixed findings:

| Severity | Component | Defect and correction |
| --- | --- | --- |
| High | Rclone plugin | Directory uploads mirrored the source and deleted existing destination files. Copy directories without deleting unrelated destination data. |
| High | Authenticated HTTP clients | Redirects could forward custom beacon/workspace credentials, bearer tokens, or request bodies. Reject redirects in the shared operator client, workspace upstream, Go/PowerShell agents, and SOCKS/TUN plugins. |
| High | Release assembly | Reused manifest filenames were validated after download. Validate filenames before constructing destination paths. |
| High | Browser ownership | Agent-build and asset-upload work could cross login changes at asynchronous boundaries. Bind actions and results to their originating token and authentication generation. |
| Medium | Asset inventory | Corrupt metadata silently became an empty store and mutations could replace bytes first. Reject corruption before mutation and preserve existing files and enablement state. |
| Medium | Asset credentials | Display URLs retained user information and download errors echoed upstream details. Strip URL credentials and return generic download failures. |
| Medium | Profile child process | Cloudflared inherited unrelated RMM credentials. Strip inherited RMM and tunnel configuration, then supply only the selected connector token. |
| Medium | Profile validation | Invalid manifests could break discovery; malformed API values could crash or change the active connector. Skip malformed bundles and reject invalid API inputs before lifecycle changes. |
| Medium | HTTP boundaries | Invalid JSON, negative lengths, and upstream transport failures produced misleading statuses or blocked reads. Validate object bodies and return appropriate HTTP errors. |
| Medium | Workspace forwarding | Fallback forwarding dropped query parameters. Preserve operation parameters while removing the local query token. |
| Medium | Event/session parity | Workspace event paging and prefix resolution differed from central. Match recent-tail/forward semantics and literal owner-scoped session resolution. |
| Medium | TUN cleanup | Successful sockets queued during shutdown were not closed. Drain and close pending results after workers stop. |
| Medium | WebUI controls | Zero jitter became 30 percent and profile errors were overwritten by refresh. Preserve explicit zero and retain operation failures after refreshing state. |
| Medium | PowerShell dispatch | Registration ran after command dequeue, so a registration failure discarded delivered work. Register before polling for the next command. |

The [Plugins report](../../Minimal-RMM-plugins/docs/code-review.md) provides
plugin-specific evidence. [WebUI lifecycle documentation](webui-connection-lifecycle.md)
describes the browser guards and behavioral tests. Profiles tests cover provider
readiness, exit, cleanup, and credential isolation using a fake provider.

## Non-Blocking Issues

- **Medium, runtime acceptance:** real Exegol MCP connectivity, BloodHound CE
  startup/import/query/proxy, Windows agent execution, native ONNX linking, and
  screenshot behavior remain unverified on the deployment host.
- **Medium, trust boundary:** shared beacon authentication still permits a holder
  of that secret to impersonate a known session. Workspace partitioning does not
  provide independent per-agent cryptographic identity.
- **Medium, durability:** central event queues are memory-only; asset bytes and
  metadata are individually atomic replacements, not one crash-safe transaction.
  One workspace process must own each workspace data directory.
- **Medium, command delivery:** dequeue has no acknowledgment/replay protocol.
  The registration ordering fix removes one avoidable loss window, but a failed
  delivery or agent crash can still lose a dequeued command.
- **Medium, performance:** SOCKS polling includes sequential connection/read work;
  no load benchmark establishes acceptable behavior for many slow targets.
- **Low, compatibility:** Python checks use the configured 3.12 SDK. Host Go and
  PowerShell checks do not establish all supported Windows/runtime combinations.
- **Low, packaging:** existing plugin binaries are unchanged. Source fixes require
  rebuild and workspace installation before they affect the lab.

## Test Assessment

The final integrated Python suite passed 485 tests. Profiles passed 12 tests;
Plugins passed 23 Python tests and all four host Go suites, including the race
detector. The RMM Go agent race suite and server TUN helper tests passed, and
Windows/Linux agent cross-builds succeeded. `make check`, additional AI-job and
plugin-release Node suites, Compose validation, shell syntax, and whitespace
checks passed. The PowerShell redirect regression exercised 15 GET/POST/file
cases without contacting a redirect destination. All 303 relative file links
checked across 94 Markdown files resolved; external URLs and anchors were not checked.

Coverage includes Python API/storage/lifecycle tests, real loopback HTTP tests,
behavioral Node tests, Go race tests, Windows/Linux agent cross-builds, profile
fake-provider tests, plugin release/archive validation, and PowerShell regression
checks. No cloud upload or live target collection is part of these tests.

Documentation updates cover workspace plugin ownership, central profile paths,
redirect policy, malformed inventory behavior, session/event semantics, and
browser authentication lifecycle. Existing contracts and generated-artifact
limitations remain explicit; local checks do not certify full live parity.

## Follow-Up Tickets

No external tickets were created.

| Follow-up | Priority | Acceptance evidence |
| --- | --- | --- |
| Rebuild and validate Windows plugins | High before deployment | Existing release/bundle checks plus matching-agent runtime tests, including preserved destination data and relay shutdown. |
| Validate the actual workspace deployment | High before demo | Authenticated login, Exegol MCP selection, CE proxy, DCOnly collection/import, and a known graph query. |
| Strengthen identity and evidence durability | Medium | Per-agent authentication and a defined persisted event-delivery policy before stronger isolation guarantees. |
| Add deployment/browser compatibility coverage | Medium | Real browser login-switch scenarios, supported Python matrix, and bounded relay load tests. |

## Final Recommendation

Review the changes across all three repositories together. Commit them when
approved, rebuild plugin artifacts through the existing release workflow, and
run the lab acceptance sequence before recording the demonstration.

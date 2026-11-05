# Full AI v3 review

## Verdict

**Not ready as a general replacement for all enabled execution skills.**
The bounded execution and evidence foundation is implemented, but passing tests
is not proof of capability coverage. This branch remains experimental and has
not been deployed or exercised against a live agent and Exegol.

## Open acceptance issue

`lateral-movement.md` permits autonomous technique selection and use of existing
authentication material. V3 can load that instruction body, but its capsules do
not expose the required remote execution, staging, transfer, deployment, or
credential-use adapters. Its exact-command agent workflow requires commands to
be supplied by the operator. The same limitation affects deployment and other
execution-oriented optional skills. File and embedding workflows inspect
records rather than performing those skills' full operations.

This does not meet the original requirement that enabled applicable skills can
carry out all their functionality. Loading a body is different from providing
the capabilities its directives require. Do not present the current registry as
complete support for every enabled skill.

The remaining architecture work is to give execution methods explicit capability
requirements and supported adapters with verifiable outcomes, report unavailable
capabilities before execution, and add acceptance tests for the corresponding
workflows. New methods must retain the role, dependency, disabled-skill, evidence,
and context contracts. Exposing an unrestricted shell to every specialist would
remove those guarantees.

## Findings fixed during this review

| Finding | Correction and evidence |
| --- | --- |
| Legacy rollback ignored the UI's no-operation and scope promises | Explicit `legacy` workflow required; other modes and nonempty v3 scope rejected before connecting tools. Transport engine selection is frozen during setup. Regressions cover rejection and an engine-change race. |
| TUN subnet validation checked only the network address | Effective routes checked for every destination, maximum 256, against the authoritative relay device. Regression includes a more-specific route through another interface and proves no scan is dispatched. |
| Collection admission skipped the required baseline | A completed baseline for the selected session is required before the collection specialist can run. |
| Fixed baseline omitted facts required by its skill | Added SID, full token output, computer domain membership, OS/build, DNS/routes, process architecture and filesystem context. Failed sections record explicit unknowns. PowerShell syntax is checked with the actual parser without executing the script. |
| Large baseline facts could make completion impossible | Full facts are archived; a compact adapter receipt records section states. `read_evidence(part=source)` retrieves the authorized full source, also supporting Nmap output. A large-source regression checks paging and completion classification. |
| Malformed native call names, directory actions, or evidence parts could interrupt the run | Scalar checks reject these as bounded compiler errors before operational dispatch. |
| Supervisor progress displayed the preceding specialist | Planning explicitly resets role, task, summary, and active methods; all-provider integration checks the transition. |
| Collection waiting exceeded its documented time bound | A monotonic 1,850-second deadline bounds owner reads and sleeps, with cancellation checkpoints. |
| V3 prompts advertised an unavailable legacy evidence tool | V3 disables the legacy `read_context` index and uses receipt-authorized `read_evidence` instead. Provider envelope tests check the obsolete directive is absent. |

## Validation scope

The full Python suite includes provider protocols, kernel/evidence contracts,
HTTP/job recovery, ownership/settings security, BloodHound broker and worker
integration, and workspace behavior. `make check` additionally runs syntax,
operator parity, JavaScript UI contracts, Markdown, and YAML checks.

The first full discovery exposed stale settings/security fixtures outside the
narrower previous check target. Comparison with HEAD confirmed that settings
already refresh effective values immediately and workspace skill snapshots
already require directory/override methods. Tests now exercise those current
contracts and explicitly select network execution when asserting Exegol
prerequisites. Assertions covering credentials, ownership, redaction, permissions,
and restart behavior remain in place. A separate planning test confirms no
Exegol resolution or central administrator credential forwarding.

Tests use fixtures and mocked provider/agent/container replies. PowerShell is
parsed, not run on a target. Nmap/proxychains interoperability, collector behavior
inside the installed Exegol image, and a complete live lateral movement workflow
are not established by this validation. No target scan was performed.

## Unchanged deliberate limits

No automatic kernel reopen or mutation replay after restart; independent runs
retain durable evidence for inspection. Windows-only baseline; AI collection
supports SOCKS, while scan routing supports SOCKS and TUN. Instruction size is
still bounded by the configured context budget; fresh episodes prevent growth
with run history, but cannot fit arbitrarily large skill bodies.

`bloodhound-queries/catalog.json` remains unchanged and is a query aid.

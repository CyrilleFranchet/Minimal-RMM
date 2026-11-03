# Lab skill review for the Ludus demo

The project runs in a Ludus range to produce a recorded PCAP and a demo.
Recording is a project outcome, not a separate AI skill. The skills focus on
reliable execution of the requested lab scenario.

Reviewed on 2026-11-03 against the local skills and the public Claude-Red main
branch. This is a targeted workflow review, not a validation of every upstream
technique or a live lab acceptance test. The initial review imported no skill
bodies. A subsequent pass adapted NetExec, both privilege-escalation skills,
all 16 web skills, and AD analysis methodology from a pinned snapshot. See the
[adaptation map, license, and validation boundaries](claude-red-adaptations.md).
No external installation script was executed.

## Comparison

[Claude-Red's README](https://github.com/SnailSploit/Claude-Red) describes a
catalog of topic-specific skills with on-demand loading. Its
[contributor guide](https://github.com/SnailSploit/Claude-Red/blob/main/CONTRIBUTING.md)
encourages focused workflows and references, but suggests much longer files
than this application's prompt budget warrants. The
[reporting skill](https://github.com/SnailSploit/Claude-Red/blob/main/Skills/utility/offensive-reporting/SKILL.md)
emphasizes preserving evidence during work and handing off reproducible results.
Those structural ideas informed the initial infrastructure changes. The later
adaptation adds concrete investigation branches, prerequisite checks, and
false-positive controls rather than relying on infrastructure alone.

The inspected
[fast-checking skill](https://github.com/SnailSploit/Claude-Red/blob/main/Skills/utility/offensive-fast-checking/SKILL.md)
is a broad checklist and uses a metadata heading rather than the contributor
guide's YAML frontmatter. Treat upstream files as material to review, not as a
uniform, tested integration contract.

Minimal-RMM reads immediate `.md` files and resolves available IDs. It now
injects only the two core bodies and an optional metadata catalog, with a
`load_skill` tool for on-demand body retrieval. It has no recursive discovery
or automatic dependency loading. A referenced skill is not automatically
loaded. Keep the existing format and select a set relevant to the deliverable.
Saved UI selections and workspace overlays can
override what a repository edit appears to provide; verify execution details
and the installed skill content on the actual deployment.

## Changes and observed reasons

| Gap | Change |
|-----|--------|
| Mandatory credential investigation unrelated to capture | Local baseline now limits secret checks to tasks that need authentication and lack it. |
| Route-consent contradiction | Pivot workflow defers to actual session authorization and operator boundaries. |
| Serial connectivity loops and ambiguous results | Pivot guidance distinguishes small diagnostics from inventory and timeout from refusal. |
| Long-running commands treated as failed after a wait timeout | Dispatch guidance records an event cursor, matching completion, and artifacts before retry. |
| Different collection method may change the demo | AD collection preserves the requested observable protocol when considering fallbacks. |
| Partial work lost in prose | Completion guidance retains compact phase checkpoints and next unfinished work. |

## Deadline-oriented acceptance scenarios

Use these as operator trials with the deployed runtime; Markdown lint alone
cannot prove the model will behave correctly.

| Scenario | Required behavior |
|----------|-------------------|
| Exegol requested but MCP missing | Report the runtime prerequisite; do not silently substitute agent-side tooling. |
| Command wait expires | Inspect the recorded operation and matching events; do not duplicate execution. |
| Model request times out after a tool completes | Retry only the model request; retain tool results. |
| Collector fallback changes the protocol | Report the mismatch against the requested demo before substituting. |
| Existing file would be overwritten | Preserve it and choose a new artifact path or obtain applicable consent. |

For the first lab rehearsal, verify the runtime prerequisites and complete one
representative scenario with matching command results before expanding it.

## Remaining limitations

- Prompt checkpoints are not durable execution checkpoints unless explicitly
  stored. A failed job cannot automatically reconstruct its full conversation.
- The current runtime reports loaded IDs, not a skill-content hash or proof of
  model compliance. Unknown selected IDs can still be omitted by resolution.
- Capture tools and the lab topology must be verified on the deployment; this
  review did not start captures, run scans, or validate a replay.

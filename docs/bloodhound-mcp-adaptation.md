# BloodHound MCP resource adaptation

Reviewed source: [mwnickerson/bloodhound_mcp](https://github.com/mwnickerson/bloodhound_mcp),
revision `cdb17097e761c8a8622cb93bc3ba49a9e150bb6e`. The upstream GPLv3 license
is preserved in
[third_party/bloodhound-mcp/LICENSE](../third_party/bloodhound-mcp/LICENSE).
This is a reorganized, reworded adaptation of that project's ten MCP
resources (schema/Cypher reference and AD/Azure/ADCS/OpenGraph guides), not a
verbatim import, an installed dependency, or an executed tool. The upstream
server itself was not run or integrated; see
[tech-plan.md](tech-plan.md#evaluate-mwnickersonbloodhound_mcp-as-a-workspace-scoped-pathfinding-tool)
for why direct integration is deferred.

## Not a SpecterOps product

SpecterOps' "Open-source Tools" resource page links to this project but does
not claim authorship, co-maintenance, or a commitment to track BloodHound CE
API changes on its behalf. Treat it as an independent, community-maintained
third-party dependency like any other — the adaptation below carries the same
revision-pinning and re-review obligation as the `claude-red` adaptation (see
[claude-red-adaptations.md](claude-red-adaptations.md)), not an upstream
contract.

## What was adapted and what was not

Adapted into `ai-skills/bloodhound-attack-methodology.md`:

- Node/property/edge schema reference (AD + Azure), reworded and reorganized
  under `bloodhound-analysis`'s existing interpretation-table conventions.
- ADCS ESC1–ESC13/GoldenCert conditions, converted to a table of underlying
  misconfigurations with required corroborating checks, matching the
  "condition observed, not exploitation confirmed" framing already used for
  AD edges in `bloodhound-analysis.md`.
- Azure/Entra and hybrid-identity interpretation notes.
- A short OpenGraph/custom-node note stating that custom node ingestion is
  **not currently supported** by this project's collector or query catalog —
  this is a documentation gap flag, not new capability.

**Not adapted:**

- The upstream `cypher/offensive-queries` resource (ready-to-run Cypher
  templates for DCSync, Kerberoasting, GPO abuse, etc.). Including them as
  copy-pasteable Cypher would hand the model an execution path around the
  validated catalog (`bloodhound-queries/catalog.json`) the same way the
  upstream server's own `cypher_query.run` tool would — see the "Schema
  reference" boundary note at the top of the new skill file. The ESC and edge
  explanations that overlap with that resource are covered as interpretation
  guidance instead, without the runnable Cypher bodies.
- The upstream server's tools, resources transport, and credential model
  (global `BLOODHOUND_TOKEN_ID`/`TOKEN_KEY` per MCP process) were not
  adopted; this project keeps per-workspace credentials
  (`RMM_BLOODHOUND_TOKEN_ID`/`TOKEN_KEY` stored per workspace root) and the
  existing `bloodhound(action=...)` adapter as the only execution path.

## Validation boundary

This is a documentation-only change: no new executable action, catalog entry,
or MCP tool was added. Existing skill-discovery and Markdown-formatting
checks cover the new file; they do not verify the adapted schema/methodology
prose against a live BloodHound CE instance. Treat it as reference knowledge
to interpret existing catalog results, not as a validated claim about any
specific environment.

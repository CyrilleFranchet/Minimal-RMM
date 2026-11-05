# Claude-Red adaptations

Reviewed source: [SnailSploit/Claude-Red](https://github.com/SnailSploit/Claude-Red/tree/a8de813edc9f66f60d44cfe42d47750e89d9930b),
revision `a8de813edc9f66f60d44cfe42d47750e89d9930b`. The upstream MIT license and
copyright notice are preserved in [third_party/claude-red/LICENSE](../third_party/claude-red/LICENSE).
These are curated adaptations, not verbatim imports or validated exploit packs.
Upstream installation scripts were not executed.

## Adaptation map

The following upstream skill directories map to local IDs under `ai-skills/`,
with the project's `id`, `title`, `description`, and `default` frontmatter.
Web entries use `web-*` filenames and IDs; their upstream sources retain
`offensive-*` names. Each adapted skill links its pinned upstream source.

| Upstream directory | Local skill IDs |
|--------------------|-----------------|
| `Skills/active-directory` | `offensive-netexec` |
| `Skills/privesc` | `offensive-windows-privesc`, `offensive-linux-privesc` |
| `Skills/web` | `web-sqli`, `web-xss`, `web-ssrf`, `web-ssti`, `web-xxe`, `web-idor`, `web-file-upload`, `web-rce` |
| `Skills/web` | `web-deserialization`, `web-race-condition`, `web-request-smuggling`, `web-open-redirect`, `web-parameter-pollution`, `web-graphql`, `web-waf-bypass`, `web-business-logic` |
| `Skills/active-directory/offensive-active-directory` | Methodology adapted into `bloodhound-analysis`; collection remains a separate project-specific skill. |

All 16 web skills present in the pinned snapshot are represented. Future
upstream additions are not automatically installed. New versions require review.

## What changed for this project

- Select tests from evidence and prerequisites; avoid running whole technique
  catalogs merely because a skill loaded.
- Use installed tools in the verified Exegol workspace and live MCP schemas.
  Inspect the intended target through RMM when host-local evidence is needed;
  an Exegol shell is not the target host.
- Keep baselines, control requests, authentication contexts, and false-positive
  checks. Distinguish an observed weakness from demonstrated effective access.
- Optimize the Ludus demo for reproducible behavior and attributable telemetry,
  not blanket stealth claims. Expected traffic is not proof of detection.
- Preserve `operator-boundaries`, existing task authorization, and the no-install
  rule. A skill does not authorize unrelated credential collection, persistence,
  broad exploitation, or changes to pre-existing state.

## Collection and analysis separation

`bloodhound-ce-python-collection.md` uses Python BloodHound **CE**, whose installed
command is `bloodhound-ce-python`, with `DCOnly` enforced by the adapter. Its
handoff is a verified run with coverage and artifacts. `bloodhound-analysis.md`
consumes that run and chooses existing catalog queries from the question and
evidence; it does not silently trigger collection or execute discovered paths.
The current adapter still needs source/target SIDs at run creation and supports
a bounded [query catalog](bloodhound-query-catalog.md). Skill separation does
not remove the run-creation API limitation.

ldeep is a [planned second collection method](tech-plan.md#ad-collection-ldeep-as-a-second-method),
not an installed skill or an automatic fallback. Its output compatibility and
coverage must be established before enabling it.

## Use with the current playbook runtime

These files remain source material in `ai-skills/`; they are not automatically
loaded by AI chat. Copy the relevant instructions into the single Playbook
editor, make the objective and scope explicit, and replace references to other
skills with the instructions the task actually needs. No core bodies,
dependencies, `/skill-id` invocations, or saved skill selections are injected.

Legacy discovery helpers preserve renamed IDs and asset metadata for
compatibility. That mapping is not part of production chat execution and does
not migrate instructions into a playbook. Existing files and operator overlays
remain untouched. See [Playbook execution](web-ai-skills.md).

## Validation boundary

Source attribution and the pinned upstream snapshot describe where the retained
reference files came from. They do not establish live compatibility or model
compliance. Current playbook tests verify single-document injection, tool
execution without preparation, bounded response recovery, and absence of
skill-directory reads. Rehearse the actual supplied playbook in the authorized
lab and retain tool results and evidence before relying on its outcome.

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

## Loading and migration

Only `operator-boundaries` and `task-completion-report` bodies enter the initial
prompt. These adaptations have `default: false`; enabled optional skills appear
as metadata and are activated for each immediate step with `prepare_step`.
`load_skill` supports read-only inspection during preparation; explicit
`/skill-id` invocations load before the first provider request. Availability is
distinct from loading. Existing saved selections continue to exclude skills
not selected: enable the new entries in the AI panel when using such a preset.

The retired `ad-collection-analysis` selection expands to
`bloodhound-ce-python-collection` and `bloodhound-analysis`. The server and
workspace share `resolve_ai_skills`; `expand_skill_ids` maps the old selection,
and `expand_skill_overrides` preserves legacy disablement unless a replacement
has an explicit setting. The browser expands the old ID before filtering its
saved selection. Discovery excludes the retired ID even if a stale overlay
still contains it, preventing the combined instructions from reappearing.
No persisted files or operator-installed overlays are deleted. Migrate any
custom additions from old overlay copies into the appropriate replacement.

The 16 web IDs similarly map from `offensive-<topic>` to `web-<topic>` in the
server, workspace, and browser. Existing disablement follows the rename unless
the new ID has an explicit setting. NetExec and privilege-escalation IDs are
unchanged. Source attribution links keep the actual upstream filenames.

## Validation boundary

Tests verify discovery, selection migration, disablement, and isolation of
optional bodies from the initial prompt. Markdown checks validate formatting.
These checks do not prove model compliance or live tool compatibility. Rehearse
one scoped case per selected skill against the Ludus lab before relying on it
for the demo; retain commands, evidence, and observed detector results separately.

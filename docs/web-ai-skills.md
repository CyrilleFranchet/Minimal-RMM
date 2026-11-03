# Web AI skills

Operators can use built-in Markdown skill files and install additional skill
files from URLs. The web AI assistant loads enabled skills and injects the
core documents into the model system prompt on each AI request. Only
`operator-boundaries` and `task-completion-report` are loaded initially.
Other available skills contribute ID, title, and a bounded description to a
catalog; the model calls the read-only `load_skill` tool when instructions are
needed. Their bodies appear as tool results, not in the initial system prompt.

Skills are **not** Cursor IDE skills — they are server-side instructions for the in-browser AI panel only.

## Directory

Built-in default: `ai-skills/` at the repo root (next to `server_rmm.py`). In
workspace mode, URL-installed files are persisted under
`RMM_WORKSPACE_DATA/assets/skills/` and overlay built-ins by skill ID.

The server can add another built-in directory with:

```bash
export RMM_AI_SKILLS_DIR=/path/to/my-skills
```

Operators can install `.md` files from the WebUI, CLI, or API. See
[operator-managed assets](operator-assets.md) for URL upload, enable/disable,
and workspace persistence.

Each skill is one `*.md` file with optional YAML frontmatter:

```markdown
---
id: my-skill-id
title: Short title for the UI
description: One-line summary (tooltip)
default: true
---

# Skill body

Markdown instructions for the model…
```

| Field | Required | Notes |
|-------|----------|--------|
| `id` | No | Lowercase `a-z`, digits, `_`, `-` (max 64). Defaults to filename without `.md`. |
| `title` | No | Shown in the AI panel checkbox list. |
| `description` | No | Tooltip in the web UI. |
| `default` | No | Legacy preference metadata; enabled skills are now available on demand when no selection is supplied. |
| `always` | No | Always available even with an explicit selection; UI checked and locked. Only the two core IDs are eagerly injected. |

Core bodies are appended under `## Operator skills`. Optional bodies are held
in the per-run snapshot until requested. Even a custom `always` skill is loaded
on demand; only the two core IDs receive initial body injection.

## REST API

```http
GET /api/v1/ai/skills
```

Returns `{ "skills": [ { "id", "title", "description", "default", "always", "enabled", "filename" } ], "count", "directory" }` (no body text).

```http
POST /api/v1/ai/chat
```

Add optional JSON field:

```json
{ "skill_ids": ["windows-user-profile-path", "another-skill"] }
```

- **Omitted** or **`null`**: all enabled skills are available.
- **Empty array `[]`**: only skills with `always: true` are available.
- **Non-empty array**: those IDs plus every `always: true` skill are available.

Availability does not imply body injection. Asset-level disable overrides are
still respected. Existing saved checkbox selections remain availability filters.

## Web UI

AI panel → **Skills** → checkboxes. Selection is synchronized to the
server/workspace SQLite UI state (`rmm_ai_skills_enabled`) after connection.
Checkboxes now select availability on demand, not eager prompt inclusion.
The execution panel lists bodies actually loaded, and updates after `load_skill`.

On connect, the UI calls `GET /api/v1/ai/skills` and renders the list. The
Skills section is open by default so the shipped skills are visible without
another click. Core skills are marked always loaded; other skills are marked
load on demand.

`rmm_ai_skill_context.prepare_skill_context` builds the catalog and wraps either
transport's dispatcher. Its run-local `dispatch` accepts exact catalog IDs,
never filesystem paths, and avoids returning a body twice. Unknown IDs return
an error; a backend tool named `load_skill` fails initialization rather than
shadowing the internal tool. Loading does not run the skill's commands.

The catalog uses existing descriptions as activation hints. Focused workflow
phases, prerequisites, expected evidence, and stop conditions are useful
patterns from Claude-Red; importing entire technique catalogs is unnecessary.
Bodies loaded during a run remain in its conversation. This is lazy loading,
not automatic context eviction or a guarantee that the model chooses correctly.

## Shipped skills

Select `exegol-rmm-pivot` when using Exegol and verify MCP is connected in execution
details. Instructions cannot supply tools missing from the live tool list.

See [lab skill review](lab-skill-review.md) for the upstream comparison,
remaining runtime limitations, and acceptance scenarios.

| File | Purpose |
|------|---------|
| `ai-skills/operator-boundaries.md` | **Always enabled.** Prohibit installations on execution hosts; require explicit, scoped operator consent before local or remote settings and password changes. |
| `ai-skills/agent-command-dispatch.md` | **On demand.** CMD vs PowerShell dispatch; always use `PS:` for PowerShell (CMD breaks `\|` in `powershell -Command`). |
| `ai-skills/windows-identity-and-groups.md` | **On demand.** Identity and groups via `whoami` / token; do not use `net user %USERNAME%` on domain accounts. |
| `ai-skills/windows-ad-recon-stealth.md` | **On demand.** Prefer **ADWS** (`Get-AD*`) over direct LDAP and `net.exe` RPC recon. |
| `ai-skills/windows-user-profile-path.md` | Do not assume `C:\Users\<username>`; resolve profile path on the agent before file ops. |
| `ai-skills/exegol-rmm-pivot.md` | **On demand.** Route work between Minimal-RMM and Exegol MCP; choose RMM control tools, Exegol execution tools, and SOCKS5/TUN based on where the operation must run. |
| `ai-skills/task-completion-report.md` | **Always enabled.** End every task with a concise work report; the server appends authoritative UTC start and completion timestamps. |
| `ai-skills/document-embeddings.md` | **On demand.** Explain document classification, use saved profiles through RMM tools, distinguish operator commands from target commands, and diagnose execution/results. |
| `ai-skills/python-bloodhound-collection.md` | **On demand.** Python BloodHound CE with enforced DCOnly; persisted collection, import, verification, and evidence handoff. ldeep is planned separately. |
| `ai-skills/bloodhound-analysis.md` | **On demand.** Consume verified runs, choose graph queries, assess candidate-path prerequisites and coverage, and distinguish expected telemetry from detection. |
| `ai-skills/offensive-netexec.md` | **On demand.** Evidence-led protocol selection and targeted authentication/access validation in Exegol. |
| `ai-skills/offensive-windows-privesc.md`, `ai-skills/offensive-linux-privesc.md` | **On demand.** Target-local privilege assessment, effective permission checks, and bounded validation. |
| `ai-skills/web-*.md` | **On demand.** All 16 web topics in the pinned Claude-Red snapshot; see the [adaptation map and provenance](claude-red-adaptations.md). |
| `ai-skills/local-recon-before-network.md` | **On demand.** Establish agent identity, token, OS, interfaces/routes, and domain context before network discovery; distinguish local DC hints from live directory lookups; then hunt scoped local secrets (scheduled tasks, web configs, etc.) to progress faster, with Seatbelt-style tools as a last resort. |
| `ai-skills/lateral-movement.md` | **On demand.** Evaluate authorized remote-access attempts, verify completion before changing methods, and report success, partial completion, failure, or uncertainty with sanitized evidence. |
| `ai-skills/agent-deployment.md` | **On demand.** Deploy an RMM agent on another authorized machine end to end: `deploy_agent_script` or `build_go_agent` produce server-configured artifacts, `queue_upload` stages them, a lateral technique executes them, and registration in `list_sessions` is the only success proof; artifact contents stay out of chat output. |

### Operator boundaries

`operator-boundaries` applies across technique skills, including lateral
movement and deployment. Existing authorization for a specific setting or
credential change remains valid; broad task authorization does not supply it.
Installations on execution hosts remain prohibited under this skill.

These are model instructions, not a tool-level enforcement mechanism.
`always: true` includes the skill regardless of chat skill selection, but
runtime asset disable overrides, workspace overlays, or an alternate skills
directory can still remove or replace it. Hard enforcement requires separate
tool authorization controls.

### Why `PS:` matters for the AI

The agent default shell is **cmd.exe**. Lines sent as `powershell -Command "script | pipeline"` are parsed by CMD first; pipe characters split the line so cmdlets like `Select-Object` run as CMD programs and fail. Skills instruct the model to prefix PowerShell with **`PS:`** (script body only, no `powershell.exe` wrapper). See `docs/client-command-execution.md` for agent-side behavior.

## Code

| Module | Role |
|--------|------|
| `rmm_ai_skills.py` | Load skills, `compose_system_prompt()` |
| `rmm_ai.py` | Passes selected skill documents into the required MCP chat loop; `_append_task_timing()` appends server-generated UTC task timing to successful responses |
| `server_rmm.py` | `GET …/ai/skills`, `skill_ids` on `POST …/ai/chat` |
| `web/ai.js` | Skill list UI, persistence, chat payload |

## Adding a skill

1. Create `ai-skills/your-skill-id.md` for a built-in skill, or install a
   Markdown URL asset for one workspace.
2. Set frontmatter (`default: true` if it should be on for new operators).
3. Enable or disable the skill from **AI → Skills** or **Config → Runtime
   assets and beacon transport**.
4. Reload the web UI (or reconnect). Files are read on each chat and skills
   request; no server restart is required.

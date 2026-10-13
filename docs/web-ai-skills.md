# Web AI skills

Operators can use built-in Markdown skill files and install additional skill
files from URLs. The web AI assistant loads enabled skills and injects the
selected documents into the model system prompt on each `POST /api/v1/ai/chat`
request.

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
| `default` | No | `true` / `false` — checked by default when the operator has no saved selection. |
| `always` | No | `true` / `false` — always injected, even if the request supplies a specific skill list. The UI renders it checked and locked. |

The body (after frontmatter) is appended under `## Operator skills` in the system prompt.

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

- **Omitted** or **`null`**: skills with `default: true` in frontmatter.
- **Empty array `[]`**: no skills (base prompt only).
- **Non-empty array**: those skill ids plus every `always: true` skill.

## Web UI

AI panel → **Skills** → checkboxes. Selection is stored in `sessionStorage` (`rmm_ai_skills_enabled`) for the browser tab.

On connect, the UI calls `GET /api/v1/ai/skills` and renders the list.

## Shipped skills

| File | Purpose |
|------|---------|
| `ai-skills/agent-command-dispatch.md` | **Default.** CMD vs PowerShell dispatch; always use `PS:` for PowerShell (CMD breaks `\|` in `powershell -Command`). |
| `ai-skills/windows-identity-and-groups.md` | **Default.** Identity and groups via `whoami` / token; do not use `net user %USERNAME%` on domain accounts. |
| `ai-skills/windows-ad-recon-stealth.md` | **Default.** Prefer **ADWS** (`Get-AD*`) over direct LDAP and `net.exe` RPC recon. |
| `ai-skills/windows-user-profile-path.md` | Do not assume `C:\Users\<username>`; resolve profile path on the agent before file ops. |
| `ai-skills/task-completion-report.md` | **Always enabled.** End every task with a concise work report; the server appends authoritative UTC start and completion timestamps. |

### Why `PS:` matters for the AI

The agent default shell is **cmd.exe**. Lines sent as `powershell -Command "script | pipeline"` are parsed by CMD first; pipe characters split the line so cmdlets like `Select-Object` run as CMD programs and fail. Skills instruct the model to prefix PowerShell with **`PS:`** (script body only, no `powershell.exe` wrapper). See `docs/client-command-execution.md` for agent-side behavior.

## Code

| Module | Role |
|--------|------|
| `rmm_ai_skills.py` | Load skills, `compose_system_prompt()` |
| `rmm_ai.py` | Passes `skill_ids` into MCP and direct chat loops; `_append_task_timing()` appends server-generated UTC task timing to successful responses |
| `server_rmm.py` | `GET …/ai/skills`, `skill_ids` on `POST …/ai/chat` |
| `web/ai.js` | Skill list UI, persistence, chat payload |

## Adding a skill

1. Create `ai-skills/your-skill-id.md` for a built-in skill, or install a
   Markdown URL asset for one workspace.
2. Set frontmatter (`default: true` if it should be on for new operators).
3. Enable or disable the skill from **Config → Runtime assets and beacon
   transport**.
4. Reload the web UI (or reconnect). Files are read on each chat and skills
   request; no server restart is required.

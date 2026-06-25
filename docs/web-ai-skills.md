# Web AI — server skills

Operators can add **Markdown skill files** on the RMM server. The web AI assistant loads them and injects selected skills into the OpenAI **system prompt** on each `POST /api/v1/ai/chat` request.

Skills are **not** Cursor IDE skills — they are server-side instructions for the in-browser AI panel only.

## Directory

Default: `ai-skills/` at the repo root (next to `server_rmm.py`).

Override with environment variable:

```bash
export RMM_AI_SKILLS_DIR=/path/to/my-skills
```

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

The body (after frontmatter) is appended under `## Operator skills` in the system prompt.

## REST API

```http
GET /api/v1/ai/skills
```

Returns `{ "skills": [ { "id", "title", "description", "default", "filename" } ], "count", "directory" }` (no body text).

```http
POST /api/v1/ai/chat
```

Add optional JSON field:

```json
{ "skill_ids": ["windows-user-profile-path", "another-skill"] }
```

- **Omitted** or **`null`**: skills with `default: true` in frontmatter.
- **Empty array `[]`**: no skills (base prompt only).
- **Non-empty array**: exactly those skill ids.

## Web UI

AI panel → **Skills** → checkboxes. Selection is stored in `sessionStorage` (`rmm_ai_skills_enabled`) for the browser tab.

On connect, the UI calls `GET /api/v1/ai/skills` and renders the list.

## Shipped skill

| File | Purpose |
|------|---------|
| `ai-skills/windows-user-profile-path.md` | Do not assume `C:\Users\<username>`; resolve profile path on the agent before file ops. |

## Code

| Module | Role |
|--------|------|
| `rmm_ai_skills.py` | Load skills, `compose_system_prompt()` |
| `rmm_ai.py` | Passes `skill_ids` into MCP and direct chat loops |
| `server_rmm.py` | `GET …/ai/skills`, `skill_ids` on `POST …/ai/chat` |
| `web/ai.js` | Skill list UI, persistence, chat payload |

## Adding a skill

1. Create `ai-skills/your-skill-id.md` on the server host.
2. Set frontmatter (`default: true` if it should be on for new operators).
3. Reload the web UI (or reconnect) — no server restart required; files are read on each chat/skills request.

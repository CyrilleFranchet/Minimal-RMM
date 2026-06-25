# Web UI — shell command completion

Keyboard ergonomics for the web console shell input (`#shell-input`).

## Features

| Key | Action |
|-----|--------|
| **↑ / ↓** | Navigate command history for the selected session (newest at ↑ from empty line) |
| **Tab** | Complete from history + dispatch prefixes; longest shared prefix first, then cycle matches |
| **Shift+Tab** | Cycle completion matches backward |
| **Enter** | Run and wait (`/exec`) |
| **Ctrl+Enter** | Queue for next beacon |

A hint line under the input shows the top completion match or match count.

## History sources

1. Commands echoed in the console (`appendShellEcho`) — includes tool actions like `download …`
2. Events reloaded from `GET /api/v1/sessions/{id}/events` — operator `queued:` lines and `output` events with `command`
3. `sessionStorage` key `rmm_shell_history` — last 100 commands per session id (survives page refresh)

## Tab completion sources

- Session command history (most recent first in candidate list)
- Static agent dispatch prefixes: `cmd:`, `PS:`, `powershell:`, `pwsh:`
- Operator meta commands: `exfil`, `download`, `screenshot`

Agent-side path completion is out of scope (tech plan §4 v2).

## Operator meta commands (shell)

These commands are handled in the browser (same as `rmm_cli.py`) and POST to the operator API instead of being sent to the agent shell:

| Command | API | Notes |
|---------|-----|-------|
| `download <remote_path>` | `POST …/download` | Spaces in path: quote or omit extra tokens are joined |
| `exfil <remote_path> [profile]` | `POST …/exfil` | Profile defaults to Exfil panel selection / server default |
| `screenshot` | `POST …/screenshot` | No arguments |

Example: `exfil C:\Users\…\file.iso mega-lab` queues rclone upload; it is not passed to `cmd.exe`.

## Files

- `web/app.js` — `rememberShellCommand`, `navigateShellHistory`, `applyShellTabCompletion`
- `web/index.html` — `#shell-completion-hint`
- `web/style.css` — `.shell-completion-hint`

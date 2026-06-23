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

Agent-side path completion is out of scope (tech plan §4 v2).

## Files

- `web/app.js` — `rememberShellCommand`, `navigateShellHistory`, `applyShellTabCompletion`
- `web/index.html` — `#shell-completion-hint`
- `web/style.css` — `.shell-completion-hint`

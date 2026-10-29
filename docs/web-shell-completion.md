# Web UI — shell command completion

Keyboard ergonomics for the web console shell input (`#shell-input`).

## Features

| Key | Action |
|-----|--------|
| **↑ / ↓** | Navigate command history for the selected session (newest at ↑ from empty line) |
| **Tab** | Complete from history + dispatch prefixes; longest shared prefix first, then cycle matches repeatedly |
| **Shift+Tab** | Cycle completion matches backward |
| **Enter** | Queue for next beacon |
| **Ctrl+Enter** | Run and wait (`/exec`) |

A hint line under the input shows the top completion match or match count.

## History sources

1. Commands echoed in the console (`appendShellEcho`) — includes tool actions like `download …`
2. Events reloaded from `GET /api/v1/sessions/{id}/events` — operator `queued:` lines and `output` events with `command`
3. SQLite UI state key `rmm_shell_history` — last 100 commands per session id;
   browser storage remains a short-lived cache.

## Tab completion sources

- Session command history (most recent first in candidate list)
- Static agent dispatch prefixes: `cmd:`, `PS:`, `powershell:`, `pwsh:`; Tab completion inserts the trailing space
- Operator meta commands: `help`, `?`, `exfil`, `download`, `screenshot`, `pe`, `embeddings`

Agent-side path completion is out of scope (tech plan §4 v2).

## Operator meta commands (shell)

These commands are handled in the browser instead of being sent to the agent shell. Commands that queue an action POST to the operator API:

| Command | API | Notes |
|---------|-----|-------|
| `help` | — | Shows this local command reference |
| `?` | — | Alias for `help` |
| `download <remote_path>` | `POST …/download` | Spaces in path: quote or omit extra tokens are joined |
| `exfil <remote_path> [profile]` | `POST …/exfil` | File or folder; profile defaults to Exfil panel / server default |
| `screenshot` | `POST …/screenshot` | No arguments |
| `embeddings run <profile> --document <path>` | `POST /api/v1/embeddings/run` | Uses the selected session and a saved profile; repeat `--document` for up to 32 paths; quoted Windows and UNC backslashes are preserved |
| `pe <plugin> [export] [input]` | `POST …/pe` | Loads a Go PE plugin through the selected agent; export defaults to `Run` |

The REST, CLI, and MCP surfaces also support plugin-owned command dispatch by
passing `command`. Those commands remain in the plugin and are not added to
the browser shell parser.

Example: `exfil C:\Users\…\file.iso mega-lab` queues rclone upload; it is not passed to `cmd.exe`.

## Files

- `web/app.js` — `rememberShellCommand`, `navigateShellHistory`, `applyShellTabCompletion`
- `web/index.html` — `#shell-completion-hint`
- `web/style.css` — `.shell-completion-hint`

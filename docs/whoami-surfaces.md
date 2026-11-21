# Identity report operator surfaces

Use `/whoami` in the CLI or WebUI shell for a detailed report from a Windows Go
agent. `/whoami --tickets` also requests Kerberos ticket metadata. Bare `whoami`
remains an ordinary agent shell command. `/uid` retains its compact output.

The standalone CLI accepts `whoami --session ID [--tickets] [--timeout 120]` and
supports `--json`. Interactive shells reject unknown or repeated options.
The standalone command exits with status 1 for HTTP errors or agent report
failures, including in JSON mode; JSON output is retained for callers to inspect.

`POST /api/v1/sessions/ID/whoami` accepts `timeout` and boolean `tickets`.
The authenticated workspace proxy allows enough time for the requested wait.
Results use the existing session event transcript and its normal escaped text
rendering. Requests capture the selected session before awaiting a response;
switching sessions prevents stale replies from changing the current shell.

## Functions

- `RMMServer.session_whoami` restricts reports to Go sessions, audits the command,
  waits for the matching result, and preserves agent failures in `ok`.
- The authenticated POST handler validates options before queueing and uses
  normal workspace session authorization. Unknown fields are rejected.
- `RmmApiClient.session_whoami` forwards report options and extends its HTTP wait
  beyond the agent result timeout.
- `cmd_whoami` resolves the selected session and renders text or JSON output.
- `tool_session_whoami` resolves only a visible unambiguous session, validates
  the ticket flag, and returns structured errors, including timeout details.
- MCP `session_whoami` offloads the same shared tool. The web AI registry exposes
  the same name and options, preserving CLI/API/MCP parity.

See [collector details](whoami.md) for fields, Windows API behavior, and limits.

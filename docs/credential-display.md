# Credential display and ID copying

`creds list` in the standalone CLI and `/creds` or `/creds list` in the
interactive CLI and WebUI display each stored credential's ID, identity,
and plaintext password. Standalone `creds list --json` includes passwords too.
The WebUI places a keyboard-accessible **Copy ID** button beside each ID.
The compact button and ID stay together when the row wraps on narrow screens.
It copies only that ID and reports **Copied** or **Copy failed**. Copying
requires browser clipboard access, typically on HTTPS or localhost.

`GET /api/v1/creds?include_passwords=1` explicitly includes the `password`
field. Authentication and workspace scoping are unchanged. Without that
query parameter, list responses still omit passwords, including MCP and
shared AI tool calls. `rmm_creds.creds_list` and `RmmApiClient.creds_list`
accept the keyword-only `include_passwords` option, defaulting to false.
Displaying credentials does not add passwords to server audit events.
WebUI passwords are inserted as text so HTML characters stay literal.

Regression coverage lives in `tests/test_creds_routes.py`,
`tests/test_cli_dispatch.py`, and `tests/test_shell_completion.cjs`.

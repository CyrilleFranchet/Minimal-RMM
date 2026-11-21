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

## Credential store dialog

The top-right **Credentials** button opens a workspace credential manager without
requiring a selected agent. The list shows identity labels and credential IDs;
passwords are not fetched. **Add credential** accepts `DOMAIN\user` or a plain
username and a password, including an empty password. **Edit** changes the
identity in place, preserving its ID. Leave the password blank to retain it, or
select **Set empty password** to clear it. Delete actions require confirmation;
**Delete all credentials** affects only the authenticated workspace.

Closing the dialog or resetting the connection clears its form and list.
Responses from a previous opening or authentication/connection generation are
ignored. Secrets are not persisted in browser storage or inserted as HTML.
Failed requests preserve the form but disable further mutations until a
successful refresh, including when a network error leaves the outcome uncertain.
Check the refreshed store before repeating a mutation. A confirmed mutation stays
reported as successful if the following list refresh fails. The list is cleared
and mutations are disabled until a successful refresh, so stale rows cannot be
edited or deleted and a completed add is not presented as a failed add.

`PATCH /api/v1/creds/{cred_id}` accepts required string `user`, optional string
`domain` (defaults to empty), and optional string `password`. Omitting `password`
preserves it; an empty string clears it. Unknown fields and non-string values
are rejected. The route requires workspace authentication, returns 404 for an
unknown ID, and reports storage failures as 503 without replacing corrupt data.
The CLI exposes `creds update -i <cred_id> <identity> [--password <value>]`;
MCP/AI exposes `creds_update(cred_id, user, domain="", password=None)`.

Implementation reference: `rmm_creds.creds_update` performs the atomic scoped
update; `RmmApiClient.creds_update`, `cmd_creds_update`, `tool_creds_update`, and
MCP `creds_update` expose it to operators. In `web/credentials.js`, `context` and
`current` guard asynchronous work; `reset` clears dialog state; `editor` fills or
clears the form; `status` reports feedback; `request` performs API calls;
`invalidateList` clears stale rows and disables mutations; `refreshAfterMutation`
preserves confirmed success when reloading fails; `render` creates safe text
rows; `load` refreshes the list; and `perform` prevents
overlapping actions and handles failure recovery. Regression coverage includes
`tests/test_credentials_ui.cjs`, `tests/test_creds_routes.py`, and
`tests/test_cli_credential_update.py`. The CLI/client/tool tests verify omitted
versus empty passwords, exact password preservation, ID URL encoding, and
failure output without secret disclosure.

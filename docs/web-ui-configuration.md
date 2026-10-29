# Web UI configuration and persistence

Process settings have their own **Service settings** section in Config and
an authenticated `/api/v1/settings` API. They are saved to the owning service's
protected configuration file and apply after restart. See
[service settings](service-settings.md) for CLI/MCP, secrets, and migration.
The browser-state format described below remains separate.

The **Config** button opens sections for plugins, installed assets, beacon
transport, and import/export. Every section starts collapsed each time the
dialog opens; `initConfiguration` resets all nested `details` before showing it. It saves
supported settings to a versioned JSON document and restores them without a
page reload. After authentication, the same operator settings are synchronized
automatically to SQLite so they survive a browser restart and follow the
current server or workspace environment.

The API token remains a login credential in the browser. It is not loaded from
the state database because the UI needs that token to authenticate the first
state request.

## SQLite ownership

| Deployment | Database | Namespace |
| --- | --- | --- |
| Server mode | `RMM_UI_STATE_DB`, default `RMM_logs/ui-state.db` | `server` |
| Workspace mode | `RMM_WORKSPACE_DATA/workspace.db` | `workspace` |

The server database and workspace database are owner-readable (`0600`), and
the containing directories are owner-only where the operating system permits
it. Provider API keys, Exegol MCP tokens, and agent generator secrets are part
of this operator state because the workspace or server is already the selected
storage owner. Do not place the database on a shared, untrusted volume.

The REST endpoint is authenticated like the other operator routes:

```http
GET   /api/v1/ui/config
PATCH /api/v1/ui/config
```

The PATCH body is `{ "storage": { "session": { ... }, "local": { ... } } }`.
The server applies an allowlist and ignores the API token. Workspace mode
serves the same path locally from its existing SQLite database.

## File format

The top-level object is deliberately small and forward-compatible:

- `schema`: `minimal-rmm-ui-config`
- `version`: integer schema version, currently `1`
- `exported_at`: ISO 8601 export timestamp
- `storage.session`: allowlisted non-secret session settings
- `storage.local`: allowlisted local settings such as `rmm_theme`
- `secrets_included`: whether the optional `secrets` object is present
- `secrets`: optional API tokens and other sensitive values

Unknown fields are ignored. Imports are accepted only for the known schema name and version. A future schema should add a migration branch rather than changing the meaning of version `1`.

## Secret handling

Exports omit secrets by default. The explicit **Include API keys and other secrets** checkbox includes the RMM API token, provider keys, Exegol token, PowerShell agent beacon secret, and Go agent beacon secret. Secret-bearing files must be protected like passwords.

When importing a file without secrets, existing secrets in the current tab are left unchanged and missing secrets remain missing. Imported provider validation state is marked stale, so each provider must be validated again before it becomes available.

## Export allowlists

`web/config.js` currently exports these session settings:

- `rmm_sidebar_width`
- `rmm_agent_gen_prefs`
- `rmm_go_agent_prefs`
- `rmm_ai_panel_open`
- `rmm_ai_provider_state`
- `rmm_ai_provider`
- `rmm_exegol_mcp_enabled`
- `rmm_exegol_mcp_url`
- `rmm_ai_skills_enabled`

When secrets are explicitly included, it exports these direct secret keys:

- `rmm_api_token`
- `rmm_openai_api_key`
- `rmm_anthropic_api_key`
- `rmm_mistral_api_key`
- `rmm_exegol_mcp_token`

The PowerShell and Go beacon secrets are nested in their respective agent
preference objects and are exported under
`rmm_agent_gen_prefs.beaconSecret` and `rmm_go_agent_prefs.beaconSecret`.

## Automatically persisted state

The automatic SQLite state allowlist is maintained as `PERSISTED_SESSION_KEYS`
in `web/config.js` and includes the settings above plus:

- `rmm_shell_history`
- `rmm_event_cursors`
- `rmm_openai_api_key`
- `rmm_anthropic_api_key`
- `rmm_mistral_api_key`
- `rmm_exegol_mcp_token`

The theme (`rmm_theme`) is persisted in the SQLite `local` object. The
browser's `sessionStorage` and `localStorage` values are only a short-lived UI
cache while the authenticated deployment state is being loaded or updated.

Chat transcripts are not part of this configuration blob. They remain in the
existing server history files or workspace `ai_chats` table.

## Maintainer guide

The export/import allowlists, schema version, and automatic persistence keys
live in `web/config.js`. `make check-web-contracts` verifies that the
allowlisted export keys are documented and that the import, upload, and SQLite
state controls remain wired into the page. When adding a new setting:

1. Decide whether it is a normal setting or a secret.
2. Add its storage key to `SESSION_KEYS` or `SECRET_KEYS` for export/import.
3. Add it to `PERSISTED_SESSION_KEYS` if it must follow the server/workspace.
4. For nested secrets, keep the explicit secret export behavior used by
   `rmm_agent_gen_prefs.beaconSecret` and `rmm_go_agent_prefs.beaconSecret`.
5. Update this document and add an import/export or persistence test.
6. Bump `VERSION` only when the file format meaning changes; keep old
   migration code when practical.

## Code reference

| Symbol | Role |
| --- | --- |
| `UiStateStore` | SQLite persistence for server-mode UI state namespaces |
| `normalize_ui_state` | Apply the server-side UI state allowlist and size limit |
| `WorkspaceStore.get_ui_state` / `set_ui_state` | Persist workspace UI state in `workspace.db` |
| `loadRmmUiConfig` | Load state after authentication and refresh the WebUI |
| `persistRmmUiConfig` | Debounce UI changes into the authenticated state endpoint |

## Embedding profiles

Embedding profiles and saved result collections use dedicated SQLite records,
not the UI-state blob or Config export. Use the **Embeddings** panel to edit,
save, import, and export them. See [document embeddings](document-embeddings.md).

## Field help

Configuration and common login, agent, pivot, AI, and embedding controls include
native hover tooltips through `title` attributes. The same explanations are
available to assistive technology through `aria-description`. Plugin Source ID
and Label also have visible hints associated through `aria-describedby`, so
their definitions are available without hovering. Source ID is the unique
reference used by CLI/API calls; Label is the friendly display name.

## Deployment mode indicator

The application header shows **Standalone**, **Central · workspace mode**, or
**Workspace: ID** from the connected service's authenticated health response.
Config repeats the mode with a description of where plugin sources, credentials,
and history belong. A central server in workspace mode directs operators to
their workspace UI for those settings.

`renderDeploymentMode` in `web/config.js` renders the badge and Config hint from
`storage_mode` or `workspace_id`; missing or unknown values display **Mode
unknown**. `connect` and `probeServerHealth` update it from live responses, and
`disconnect` resets it. The display does not infer mode from the port, browser
storage, or environment files. Controller tests cover all four display states.

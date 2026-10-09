# Web UI configuration export

The **Config** button in the web UI opens an export/import panel. It saves supported browser settings to a versioned JSON document and restores them into `sessionStorage` (and the theme into `localStorage`). Importing a file refreshes the current UI without a page reload.

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

## Current allowlists

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

The PowerShell and Go Beacon secrets are nested in their respective agent preference objects and are exported under `rmm_agent_gen_prefs.beaconSecret` and `rmm_go_agent_prefs.beaconSecret`.

## Maintainer guide

The allowlists and schema version live in `web/config.js`. `make check-web-contracts` verifies that the allowlisted keys are documented and that the export/import controls remain wired into the page. When adding a new setting:

1. Decide whether it is a normal setting or a secret.
2. Add its storage key to `SESSION_KEYS` or `SECRET_KEYS`.
3. For nested secrets, remove them from the normal export and add an explicit secret field, following `rmm_agent_gen_prefs.beaconSecret` and `rmm_go_agent_prefs.beaconSecret`.
4. Update this document and add an import/export test or manual checklist item.
5. Bump `VERSION` only when the format meaning changes; keep old migration code when practical.

The feature intentionally excludes shell history, event cursors, and chat transcripts. Those are runtime/session data rather than UI configuration.

# Persistent service settings

Configure service behavior in **Config → Service settings**, through the CLI,
or through MCP. The configuration belongs to the service you connect to:
central infrastructure settings belong to the central administrator; workspace
settings belong to that workspace. A workspace token cannot administer the
central server's tunnel or credential registry.

## Save and apply

Settings are stored in an owner-only JSON file: `RMM_logs/settings.json` for
the server, or `settings.json` inside the workspace data directory. Select a
custom file with `--config FILE` or `RMM_CONFIG_FILE`. Back up this file with
the service's data; it contains credentials and must not be committed to Git.

**Restart the affected Python service after saving settings.** Running services
use an immutable startup snapshot. Saving a tunnel configuration and restarting
only its connector does not load the new service settings. Restart the central
service, then start the profile if autostart is disabled.

Precedence is **explicit startup flags → nonempty environment overrides → saved
settings → built-in defaults**. The settings view identifies the source and
restart requirements. A saved value cannot override an environment variable or
startup flag. Remove that override from the service manager/container launcher
and restart before expecting the saved value to take effect. Empty legacy
environment values are treated as unset.

Secret fields are write-only. Read responses indicate whether a credential is
configured, without returning its value. Leaving a secret field unchanged keeps
it; removing a saved value restores environment/default resolution. Secret files
are protected by filesystem permissions, not encrypted at rest.

## First startup without environment secrets

Initialize configuration locally before starting an authenticated service:

```bash
python3 rmm_settings.py --scope server --config RMM_logs/settings.json set RMM_PROFILE_AUTOSTART false
python3 rmm_settings.py --scope server --config RMM_logs/settings.json set RMM_API_TOKEN --value-file /private/path/admin-token
python3 rmm_settings.py --scope server --config RMM_logs/settings.json set RMM_BEACON_SECRET --value-file /private/path/beacon-token
python3 server_rmm.py --config RMM_logs/settings.json
```

The input files contain the secret value only. `--value-file -` reads standard
input; use this for secret-manager integration rather than command-line literals
that enter shell history or process arguments. Keep `RMM_STORAGE_MODE=workspace`
when running the central service in workspace mode.

The offline command also supports `show`, `unset KEY`, and `import FILE`.
Offline imports accept a JSON object of setting names and typed values, or an
object wrapping them under `settings`. Configure a
workspace's `RMM_C2_URL`, `RMM_WORKSPACE_ID`, `RMM_WORKSPACE_TOKEN`, and
`RMM_WORKSPACE_LOCAL_TOKEN` locally, then start `workspace_service.py` with the
same configuration file and persistent data directory. Add the matching ID/token
to the central `RMM_WORKSPACES` registry separately.

## Connected interfaces

```bash
python3 rmm_cli.py settings show
python3 rmm_cli.py settings set RMM_ASSET_MAX_BYTES 134217728
python3 rmm_cli.py settings unset RMM_ASSET_MAX_BYTES
python3 rmm_cli.py settings import /private/path/settings.json
```

Connected CLI imports use `{"settings":{"KEY":value}}`; `-` reads stdin.
Use `settings set KEY --value-file FILE` (or `-`) for secrets.

REST uses `GET /api/v1/settings` and `PATCH /api/v1/settings`:

```json
{"settings":{"RMM_ASSET_MAX_BYTES":134217728,"RMM_PROFILE_AUTOSTART":false}}
```

A `null` value removes the saved override. MCP exposes `settings(action="show")` and `settings(action="update", settings={...})` for
reading and updating the same configuration. Use the connected service's
configuration schema for allowed keys, types, defaults, and ownership scope.

## Environment migration inventory

Existing environment configuration remains compatible. Move values into saved
settings, remove their environment definitions, and restart. Existing saved
plugin sources, managed rclone assets, and embedding profiles remain their own
configuration stores.

| Category | Settings and migration |
| --- | --- |
| Transport | `RMM_PROFILE_NAME`, `RMM_PROFILE_AUTOSTART`, `RMM_BEACON_PUBLIC_URL`; see [named tunnels](rmm-profiles.md) |
| Cloudflare | `RMM_CLOUDFLARE_MODE`, `RMM_CLOUDFLARE_PUBLIC_URL`, `RMM_CLOUDFLARE_TOKEN`, `RMM_CLOUDFLARE_BINARY`, `RMM_CLOUDFLARE_ORIGIN_HOST`, legacy `CLOUDFLARED_BIN` |
| Request and asset limits | `RMM_MAX_BODY_BYTES`, `RMM_MAX_WORKSPACE_PAYLOAD_BYTES`, `RMM_WORKSPACE_MAX_BODY`, `RMM_ASSET_MAX_BYTES` |
| Workspace synchronization | `RMM_WORKSPACE_EVENT_LIMIT`, `RMM_WORKSPACE_AUTO_CLAIM`, `RMM_WORKSPACE_POLL_INTERVAL` |
| Authentication and connection | `RMM_API_TOKEN`, `RMM_BEACON_SECRET`, `RMM_WORKSPACES`, `RMM_C2_URL`, `RMM_WORKSPACE_ID`, `RMM_WORKSPACE_TOKEN`, `RMM_WORKSPACE_LOCAL_TOKEN` |
| Listeners | `RMM_ADMIN_BIND`, `RMM_ADMIN_PORT`, `RMM_BEACON_PORT`, `RMM_BEACON_BIND`, `RMM_WORKSPACE_BIND`, `RMM_WORKSPACE_PORT`; explicit flags still win |
| Assets and helper paths | `RMM_PLUGINS_DIR`, `RMM_ASSETS_DIR`, `RMM_PROFILES_DIR`, `RMM_UI_STATE_DB`, `RMM_TUN_HELPER`, `RMM_WORKSPACE_TUN_HELPER`, `RMM_TUN_LOG_LEVEL`, `RMM_RCLONE_BIN` |
| AI | Server-only engine controls: `RMM_AI_MAX_TOOL_ROUNDS`, `RMM_AI_USE_MCP`. Owner-scoped defaults: `RMM_AI_SKILLS_DIR`, `RMM_EXEGOL_MCP_URL`, `RMM_EXEGOL_MCP_TOKEN`, `OPENAI_API_KEY` |
| Plugin sources | `RMM_PLUGIN_REPOSITORY` seeds the default source; edit existing repositories/branches through the source editor. Store credentials using `RMM_PLUGIN_GITHUB_TOKEN` or the source's `RMM_PLUGIN_TOKEN_<NAME>` reference |
| Rclone | `RMM_RCLONE_DEFAULT_PROFILE`, `RMM_RCLONE_MAX_BYTES`, `RMM_RCLONE_PROFILES`, `RMM_RCLONE_PROFILES_FILE`; managed profile assets remain available |

Workspace AI requests carry their configured provider/Exegol defaults to the
central execution service. Explicit request values win; a saved Exegol token
is used only for its configured endpoint. AI engine limits and MCP execution
mode belong to the central server. Internal AI tools retain the requesting
workspace's credentials and cannot administer central settings.

The following are deliberately outside ordinary service configuration:

- Mode and configuration/data location: `RMM_STORAGE_MODE`, `RMM_CONFIG_FILE`,
  `RMM_WORKSPACE_DATA`, `RMM_LOG_DIR`, and the corresponding startup flags.
- Docker host settings: published host ports and `RMM_PLUGIN_STORE` /
  `RMM_PROFILE_STORE` volume sources. Changing a saved listener setting cannot
  change Docker port publication or mounts. The shipped Compose command fixes
  its container listener ports; change Compose itself for a different layout.
- Client connection settings: CLI/MCP URL and authentication must be available
  before connecting. Use client configuration or connection flags.
- Agent environment overrides: base URL, beacon secret, proxy, sleep/jitter,
  retry/log options, and PowerShell HTTP options affect the agent process.
  They are distinct from service settings; use the agent generator where the
  option is supported.
- `RMM_PROFILE_*` subprocess protocol fields, operating-system variables, and
  `RMM_MSVC_TEST_*` test inputs are internal/runtime controls. `RMM_INTERNAL_MCP`
  is an internal subprocess marker that bypasses unrelated client config files.

## Implementation

If startup rejects `RMM_WORKSPACES`, supply valid JSON: either an object mapping
workspace IDs to nonempty string tokens, or a list of `id`/`token` records
(`workspace_id` is also accepted). IDs and tokens are trimmed as in the legacy
registry. Shell assignments must quote the whole JSON value, for example
`export RMM_WORKSPACES='{"alice":"replace-with-workspace-token"}'`.
Startup errors distinguish JSON syntax from invalid entries without displaying
credentials. An environment override still takes precedence over a saved value;
remove it only after the intended registry has been saved in configuration.

`rmm_settings.py` defines the shared typed configuration registry, validation,
protected persistence, redacted public views, startup resolution, and offline
administration commands. Its `getenv` accessor supplies the active snapshot to
service consumers without modifying the process environment. Service settings
APIs save the next-start configuration; they do not mutate live service state.

| Symbol | Responsibility |
| --- | --- |
| `SettingsError` | Report invalid configuration without echoing secret values |
| `field` / `FIELDS` | Define typed settings and UI metadata |
| `definition` | Restrict supported keys to the owning scope, including named plugin credentials |
| `validate` / `_decode_environment` | Validate typed input and decode legacy environment strings |
| `SettingsStore` | Own the file, startup snapshot, environment overrides, and atomic persistence |
| `SettingsStore._read` | Validate file schema, owner scope, and saved values |
| `SettingsStore.snapshot` | Return redacted active/saved metadata and pending restart state |
| `SettingsStore.default_value` | Report derived runtime defaults, including paths and dependent limits |
| `SettingsStore.update` | Validate an entire patch before atomic replacement |
| `getenv` | Adapt the startup snapshot to legacy string consumers |
| `active_store` | Locate the process owner or an isolated embedded-service store |
| `settings_api` | Share redacted API behavior and safe error handling |
| `record_flags` | Record explicit startup overrides and their command-line provenance |
| `bootstrap` | Select and load configuration before application imports |
| `main` | Implement offline show/set/unset/import commands |

The operator adapters are `RmmApiClient.settings`, CLI `read_settings_json` /
`cmd_settings`, shared `tool_settings`, and MCP `settings`. The browser
controller in `web/settings.js` groups rendering/status helpers, typed value and
change parsing, authenticated requests, and load/save/reset operations.
`tests/test_settings_security.py` exercises ownership guards, request credential
isolation, internal child-process configuration, AI defaults, and flag precedence.

The persisted file uses  `schema: "minimal-rmm-settings-v1"`, its `scope`, and a
`settings` object. Use the CLI for initialization so the schema and permissions
are correct. Client scope defaults to `~/.minimal-rmm/client-settings.json`.

See the [deployment guide](deployment.md) and [documentation index](README.md).

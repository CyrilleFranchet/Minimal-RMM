# Configuration and storage reference

Use the [deployment wrapper guide](deployment.md) for every service startup.
The configuration source is the selected owner's `settings.json`, not a second
`.env`, repository runtime directory, or home-directory service configuration.

## Where each kind of configuration lives

| Configuration/state | Canonical location relative to deployment root | Writer |
| --- | --- | --- |
| Central operational settings and secrets | `central/settings.json` | Preparation; central Config/settings API; offline settings tool |
| Workspace settings and secrets | `workspaces/<id>/settings.json` | Preparation; workspace Config/settings API; offline settings tool |
| Operator UI state, AI chat, embeddings and result collections | `workspaces/<id>/workspace.db` | Workspace APIs/UI |
| Plugin repositories and credential references | `workspaces/<id>/assets/plugin-sources.json` | Workspace source editor/API |
| Asset registration and explicit enablement | `workspaces/<id>/assets/metadata.json` | Workspace upload/install/enable APIs |
| Plugins and optional skill files | `workspaces/<id>/assets/plugins/` and `assets/skills/` | Workspace uploads/releases; built-in skills remain code assets |
| Rclone profile credentials | Registered files in `workspaces/<id>/assets/rclone/` | Workspace asset upload; explicit offline import |
| External beacon transport bundles | `central/profiles/` | Explicit trusted bundle installation |
| Central admin UI, AI jobs and build/cache state | `central/` | Central service |
| BloodHound lifecycle and workflow state | `workspaces/<id>/bloodhound/` | Owning workspace |
| Topology/registered owner IDs | `deployment.json` | Deployment preparation; contains no credential values |
| Container mapping/build definition | `compose.generated.json` | Wrapper; derived output, not editable operational source |

One directory convention does not mean every subsystem shares one JSON file.
Credentials/settings, binary assets, explicit enablement, relational event data,
and generated caches have different formats and lifecycles. They have one owner
and one predictable root. The central service never opens the workspace's files;
workspaces communicate with it through authenticated APIs.

## Precedence under the wrapper

Normal `rmm_deployment.py run` ignores inherited known service-setting variables,
including provider keys and stale rclone configuration. It keeps OS/tool
infrastructure such as PATH, certificate settings, Docker connectivity, and an
existing Kerberos cache selection. It does not load `.env` files.

The wrapper pins the selected owner's settings path, data root, asset directory,
central plugin cache, UI database, and transport bundle directory. A saved
storage path that conflicts with that convention is rejected with migration
guidance instead of silently opening a different installation. Relative saved
helper/executable paths resolve against the owner's directory; unqualified
PATH-based program names keep their normal meaning. Default TUN helper binaries
remain installed code assets, not an extra configuration root.

For normal startup the precedence is **wrapper-owned paths → saved owner settings
→ schema defaults**. Native `--allow-env-overrides` is an explicit compatibility
mode: valid same-scope environment settings then override saved values. It cannot
redirect wrapper-owned storage or change workspace identity. It is unavailable
for the dedicated central Docker launch. Do not use it for new installations.

The lower-level service modules still accept legacy flags/environment for
internal tests and compatibility. Their precedence is explicit flags → nonempty
environment → saved settings → defaults. This is not an alternate startup recipe.
The settings API labels those sources. Wrapper-derived path values can appear
as environment provenance because they are passed to the child process; their
source of truth is the wrapper's root argument.

## Editing and applying settings

Preparation generates secrets and valid scope envelopes. Do not copy a central
settings file into a workspace. Edit a selected owner through its Config panel,
CLI/MCP settings tool, or the offline helper with the exact file:

```bash
python3 rmm_settings.py --scope server \
  --config /workspace/rmm-deployment/central/settings.json \
  set RMM_AI_OUTPUT_RESERVE 16384
python3 rmm_settings.py --scope workspace \
  --config /workspace/rmm-deployment/workspaces/default/settings.json \
  set RMM_RCLONE_DEFAULT_PROFILE mega-lab
```

For secrets use `--value-file FILE` or `--value-file -` rather than placing them
in argv or shell history. The offline helper also supports `show`, `unset KEY`,
and `import FILE`; import accepts a settings object or a `settings` envelope.
Connected APIs use `GET/PATCH /api/v1/settings`, and MCP uses `settings`:

```json
{"settings":{"RMM_RCLONE_DEFAULT_PROFILE":"mega-lab","RMM_RCLONE_MAX_BYTES":104857600}}
```

A `null` removes a saved override. Secret fields are write-only in the API.
Files have owner-only permissions; secrets are not encrypted at rest. Store
private backups with the owner subtree. Stop/restart the affected service
through the wrapper after startup-setting changes. Those settings keep their
startup values until restart. The four managed BloodHound credential fields
are the exception: they refresh immediately so password changes and token
recovery work without restarting the workspace. Their provenance and restart
flags reflect the active credentials; explicit environment overrides still win.
Re-run `run --docker` after listener changes to regenerate container mappings.

## Rclone registration and migration

A file copied directly to `assets/rclone/` is **not active** until registered.
Upload it through the workspace WebUI or asset API/CLI, or use the wrapper's
`import-rclone` command offline. Explicitly disabled files stay disabled.
Malformed enabled files and duplicate profile names block profile loading with
a content-free error. The public rclone status shows the workspace directory,
registration source, and any load error; it never returns complete credentials.

The live workspace loader uses registered assets only. It does not consult
`RMM_RCLONE_PROFILES`, `RMM_RCLONE_PROFILES_FILE`, or repository examples when
profiles are missing/disabled/invalid. Those two settings remain compatibility
inputs for lower-level helpers; migrate an explicit old file with the wrapper
and remove old definitions. An example file is documentation, not a fallback
remote destination. Existing multiple registered profile assets remain valid,
provided their profile names do not collide. See [rclone](rclone-exfil.md).

## Bootstrap arguments and infrastructure variables

| Input | Meaning |
| --- | --- |
| `prepare --output` / `run --root` | Explicit deployment root on this machine/container |
| `--component single/control/workspace` | Which local service process(es) to supervise |
| `--workspace-id` | Registered owner to select; required for ambiguous multi-owner roots |
| `--preset single/distributed` | Topology; old `multi` metadata is retained for compatibility |
| `--central-url` | Administration origin reachable from the workspace; required for distributed preparation |
| `--docker` | Dedicated distributed central container only |
| `--admin-publish-bind`, `--beacon-publish-bind` | Host port publication addresses; listener ports come from central settings |
| `DEPLOYMENT_ROOT`, `WORKSPACE_ID`, `PYTHON` | Optional Makefile convenience inputs; not service settings |
| `RMM_CONFIG_FILE`, `RMM_LOG_DIR`, `RMM_WORKSPACE_DATA`, `RMM_STORAGE_MODE` | Wrapper-derived bootstrap values; do not maintain competing shell definitions |
| `PATH`, `KRB5CCNAME`, Docker/OS certificate variables | External tool/runtime environment; not duplicate service configuration |
| `RMM_INTERNAL_MCP`, `RMM_WORKSPACE_OPERATOR_RELAY` | Private runtime markers; wrapper strips inherited values |

The tables below enumerate the service schema from `rmm_settings.FIELDS`.
S = central/server, W = workspace, C = standalone operator client. Variable
names are also the JSON setting keys; normally set them in the matching owner
file rather than exporting them. Empty defaults mean unset. Derived storage
locations are supplied by the wrapper, regardless of the schema's empty default.

## Listeners

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_ADMIN_BIND` | S | string | `"127.0.0.1"` | Administration bind address |
| `RMM_ADMIN_PORT` | S | integer | `8080` | Administration port |
| `RMM_BEACON_BIND` | S | string | `"127.0.0.1"` | Beacon bind address |
| `RMM_BEACON_PORT` | S | integer | `8081` | Beacon port |
| `RMM_WORKSPACE_BIND` | W | string | `"127.0.0.1"` | Workspace bind address |
| `RMM_WORKSPACE_PORT` | W | integer | `8787` | Workspace port |

## Authentication

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_API_TOKEN` | S/C | string | `unset; secret` | Operator API token |
| `RMM_BEACON_SECRET` | S | string | `unset; secret` | Beacon shared secret |
| `RMM_WORKSPACES` | S | json | `unset; secret` | Workspace IDs and tokens |
| `RMM_WORKSPACE_LOCAL_TOKEN` | W | string | `unset; secret` | Local operator token |

## Connection

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_WORKSPACE_ID` | S/W/C | string | `unset` | Workspace ID |
| `RMM_WORKSPACE_TOKEN` | S/W/C | string | `unset; secret` | Workspace token |
| `RMM_C2_URL` | W | string | `"http://127.0.0.1:8080"` | Central server URL |
| `RMM_SERVER_URL` | C | string | `"http://127.0.0.1:8787"` | Operator service URL |
| `RMM_BASE_URL` | C | string | `unset` | Legacy operator URL |

## Workspace

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_WORKSPACE_AUTO_CLAIM` | W | boolean | `false` | Automatically claim sessions |
| `RMM_WORKSPACE_POLL_INTERVAL` | W | number | `1` | Sync interval (seconds) |

## Limits

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_WORKSPACE_EVENT_LIMIT` | S | integer | `10000` | Events per workspace |
| `RMM_MAX_BODY_BYTES` | S | integer | `33554432` | Maximum request bytes |
| `RMM_MAX_WORKSPACE_PAYLOAD_BYTES` | S | integer | `33554432` | Maximum workspace task bytes |
| `RMM_WORKSPACE_MAX_BODY` | W | integer | `33554432` | Maximum request bytes |
| `RMM_ASSET_MAX_BYTES` | S/W | integer | `67108864` | Maximum asset bytes |

## Storage paths

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_PLUGINS_DIR` | S | string | `unset` | Retired global plugin directory (ignored) |
| `RMM_PLUGIN_CACHE_DIR` | S | string | `unset` | Workspace plugin delivery cache |
| `RMM_ASSETS_DIR` | S | string | `unset` | Asset directory |
| `RMM_PROFILES_DIR` | S | string | `unset` | Transport bundle directory |
| `RMM_UI_STATE_DB` | S | string | `unset` | Browser state database |

## Beacon transport

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_PROFILE_NAME` | S | string | `"cloudflared"` | Transport profile |
| `RMM_PROFILE_AUTOSTART` | S | boolean | `true` | Start transport on boot |
| `RMM_BEACON_PUBLIC_URL` | S | string | `unset` | Static beacon URL fallback |

## Cloudflare

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_CLOUDFLARE_MODE` | S | string | `"quick"` | Cloudflare tunnel mode |
| `RMM_CLOUDFLARE_PUBLIC_URL` | S | string | `unset` | Named tunnel public URL |
| `RMM_CLOUDFLARE_TOKEN` | S | string | `unset; secret` | Named tunnel token |
| `RMM_CLOUDFLARE_BINARY` | S | string | `"cloudflared"` | Cloudflared executable |
| `CLOUDFLARED_BIN` | S | string | `unset` | Legacy cloudflared executable |
| `RMM_CLOUDFLARE_ORIGIN_HOST` | S | string | `unset` | Quick tunnel origin host |

## Plugin credentials

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_PLUGIN_REPOSITORY` | S/W | string | `"CyrilleFranchet/Minimal-RMM-plugins"` | Initial default plugin repository |
| `RMM_PLUGIN_GITHUB_TOKEN` | S/W | string | `unset; secret` | Default GitHub token |

## Rclone

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_RCLONE_BIN` | S/W | string | `unset` | Rclone executable |
| `RMM_RCLONE_DEFAULT_PROFILE` | S/W | string | `"mega-lab"` | Default remote profile |
| `RMM_RCLONE_MAX_BYTES` | S/W | integer | `104857600` | Maximum transfer bytes (0 = unlimited) |
| `RMM_RCLONE_PROFILES_FILE` | S/W | string | `unset` | Legacy remote profiles file |
| `RMM_RCLONE_PROFILES` | S/W | json | `unset; secret` | Legacy remote profiles JSON |

## TUN

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_TUN_HELPER` | S | string | `unset` | TUN helper executable |
| `RMM_WORKSPACE_TUN_HELPER` | W | string | `"tools/rmm-tun"` | TUN helper executable |
| `RMM_TUN_LOG_LEVEL` | S/W | string | `"info"` | TUN log level |

## AI

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_AI_MAX_TOOL_ROUNDS` | S | integer | `32` | Provider requests per checkpoint; five checkpoints give a default ceiling of 160 |
| `RMM_AI_USE_MCP` | S | boolean | `true` | Required for AI; false produces `exegol_requires_mcp` |
| `RMM_AI_CONTEXT_BUDGET` | S | integer | `65536` | AI context budget (conservative tokens) |
| `RMM_AI_OUTPUT_RESERVE` | S | integer | `4096` | AI output token reserve |
| `RMM_AI_SKILLS_DIR` | S/W | string | `unset` | Legacy skill discovery directory; not read by AI chat |
| `OPENAI_API_KEY` | S/W | string | `unset; secret` | Default OpenAI API key |
| `RMM_EXEGOL_MCP_URL` | S/W | string | `unset` | Exegol MCP URL |
| `RMM_EXEGOL_MCP_TOKEN` | S/W | string | `unset; secret` | Exegol MCP token |
| `RMM_EXEGOL_CONTAINER` | S/W | string | `unset` | Optional Exegol container override (automatic discovery when empty) |

## BloodHound

| Key | Scope | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `RMM_BLOODHOUND_URL` | S/W | string | `"http://127.0.0.1:1030"` | BloodHound CE URL |
| `RMM_BLOODHOUND_UI_BIND` | S/W | string | `"127.0.0.1"` | BloodHound UI proxy bind address |
| `RMM_BLOODHOUND_UI_PORT` | S/W | string | `"1031"` | BloodHound UI proxy port |
| `RMM_BLOODHOUND_UI_PUBLIC_URL` | S/W | string | `"http://127.0.0.1:1031"` | Browser-facing BloodHound proxy origin |
| `RMM_BLOODHOUND_TOKEN_ID` | S/W | string | `unset; secret` | BloodHound API token ID |
| `RMM_BLOODHOUND_TOKEN_KEY` | S/W | string | `unset; secret` | BloodHound API token key |
| `RMM_BLOODHOUND_ADMIN_PASSWORD` | S/W | string | `unset; secret` | BloodHound CE admin password |
| `RMM_BLOODHOUND_PENDING_PASSWORD` | S/W | string | `unset; secret` | Durable candidate for interrupted password changes |
| `RMM_BLOODHOUND_COLLECTOR` | S/W | string | `"bloodhound-ce-python"` | BloodHound CE collector executable |

Additional `RMM_PLUGIN_TOKEN_<NAME>` secret settings are source-specific credential references supported in the owner scope. The GitHub source editor stores the reference, never a token value in plugin-sources.json.

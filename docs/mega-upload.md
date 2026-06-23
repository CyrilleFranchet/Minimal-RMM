# MEGA upload

Operators can queue exfil of a **remote file on the agent** to [MEGA](https://mega.io/) using a **server-configured account**. The agent sends the file to the RMM server in chunks; the server uploads to MEGA and stores the public link in the session transcript (no copy under `RMM_logs/downloads/`).

## Flow

1. Operator queues via REST, CLI, MCP, or web UI.
2. Server enqueues `__MEGA__ <remote_path>` for the next beacon (requires MEGA env on server).
3. Agent reads the file and POSTs chunked JSON to `/result?type=mega_staging`.
4. Server assembles a temp file under `RMM_logs/mega_staging/`, uploads via `mega.py-v2`, deletes the staging file.
5. Event transcript shows `remote_path → https://mega.nz/#!…`.

## Server account configuration

Set on the machine running `server_rmm.py`:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RMM_MEGA_EMAIL` | yes | — | MEGA account email |
| `RMM_MEGA_PASSWORD` | yes | — | MEGA account password |
| `RMM_MEGA_FOLDER` | no | `RMM` | Destination folder in the account (created if missing) |
| `RMM_MEGA_MAX_BYTES` | no | 100 MB | Max file size before upload |

Install the library (Python 3.10+):

```bash
pip install mega.py-v2
```

Check configuration:

```bash
rmm_cli.py mega-config
# or
curl -s -H "Authorization: Bearer $RMM_API_TOKEN" http://127.0.0.1:8080/api/v1/mega/config
```

Agents **never** receive MEGA credentials.

## Operator API

```http
POST /api/v1/sessions/{id}/mega
Authorization: Bearer <RMM_API_TOKEN>
Content-Type: application/json

{"remote_path": "C:\\Users\\x\\doc.pdf"}
```

```http
GET /api/v1/mega/config
Authorization: Bearer <RMM_API_TOKEN>
```

Response (queue):

```json
{
  "ok": true,
  "session_id": "…",
  "queued": "__MEGA__ C:\\Users\\x\\doc.pdf",
  "max_bytes": 104857600,
  "mega": {
    "configured": true,
    "email": "o***@example.com",
    "folder": "RMM",
    "max_bytes": 104857600,
    "library_available": true
  }
}
```

## CLI and MCP

- `rmm_cli.py mega <remote_path>`
- `rmm_cli.py mega-config [--json]`
- Interactive: `mega C:\path\file.zip`
- MCP: `queue_mega(session_ref, remote_path)`, `get_mega_config()`

## Result event

`type`: `mega_upload`

```json
{
  "type": "mega_upload",
  "remote_path": "C:\\Users\\x\\doc.pdf",
  "success": true,
  "link": "https://mega.nz/#!…",
  "size": 12345,
  "error": null
}
```

## Security

- Files leave the agent and are stored on MEGA under the configured account (lab use only).
- Staging files in `RMM_logs/mega_staging/` are deleted after upload attempt.
- Use a dedicated lab MEGA account with limited quota.

## Related

- Server: `rmm_mega.py`, `queue_agent_mega`, `handle_result` (`mega_staging`, `mega_upload`)
- Agent: `Invoke-RmmMegaUpload`, `Send-RmmMegaStaging`
- Web UI: **Upload to MEGA** in the tools panel + status line

## Module reference (`rmm_mega.py`)

| Symbol | Role |
|--------|------|
| `MegaConfig` | Dataclass: email, password, folder, max_bytes |
| `load_mega_config()` | Read env into `MegaConfig` |
| `mega_public_config()` | Operator-safe dict (masked email, no password) |
| `require_mega_config()` | Raise `MegaConfigError` if account or library missing |
| `upload_file_to_mega(local_path, …)` | Login (cached), upload, return public link dict |
| `reset_mega_client()` | Clear cached MEGA session after errors |

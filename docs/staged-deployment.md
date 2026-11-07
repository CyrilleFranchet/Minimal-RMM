# Staged Deployment

Minimal-RMM supports two agent delivery modes controlled by the **Stageless** checkbox in the WebUI (`--stageless` in CLI, `stageless=True` in MCP/API).

## Default: Staged (stageless unchecked)

The default build produces a **small stage1 launcher** (~20 KB) instead of the full Go agent.

### What happens

1. The server compiles the Go agent (the stage2 binary, ~5–6 MB).
2. The binary is XOR-masked with a SHA-256 counter keystream and registered in the server's staging store at `/tools/stage/{staging_id}`. The entry expires after 24 hours or on first successful fetch.
3. The server compiles a compact C stage1 launcher using `x86_64-w64-mingw32-gcc` with the `BASE_URL` baked in. If MinGW is not installed, the server falls back to building a Go stage1 (larger, ~2 MB).
4. The downloaded ZIP contains:
   - `stage1.exe` — the compact launcher
   - `run-stage1.ps1` — PowerShell wrapper with `--stage <staging_id> --session <session_id>` pre-filled
   - `README.txt`

### Deployment

Copy `stage1.exe` and `run-stage1.ps1` to the target, then run:

```powershell
.\run-stage1.ps1
```

The wrapper waits for stage1 and prints its exit code: `0` = agent installed and started, `1` = staging failure (check the stage status endpoint below), `2` = stage1 rejected its arguments or has no base URL embedded. The C launcher attaches to a parent console when available and prints diagnostics; service and Explorer launches stay silent. A network failure can also prevent delivery of the status report.

stage1 connects back to the RMM server over HTTPS, fetches the masked Go agent, decodes it, writes it to the current user's temp directory (`%TEMP%\minimal-rmm-agent.exe` — no administrator rights required), and executes it. The session ID is fixed at build time so the agent registers with a known ID on first beacon.

### Checking the launcher status

stage1 posts one status line to the staging entry (`stage1 ok: agent installed and started` on success, `stage1 failed: ...` on failure). Read it from the operator API:

```bash
# REST
curl -H "Authorization: Bearer $RMM_API_TOKEN" \
  "$RMM_URL/api/v1/agent/go/stage/<staging_id>"

# CLI
rmm_cli agent go-stage <staging_id>

# MCP
stage_status(staging_id="<staging_id>")
```

The response includes the last status line, whether the payload was fetched, and the expiry time. A 404 means the entry is unknown, purged, or expired.

### Why staged?

- The operator-deployed file is tiny (~20 KB). Useful when writing to `ADMIN$` over SMB or staging via psexec/WMI.
- The Go agent never touches the target filesystem until stage1 runs — it only exists on the server.
- The staging ID is single-use: the entry is consumed on first fetch.

## Stageless (stageless checked)

Builds the full Go agent and packages it directly into the ZIP, same as the original behavior.

The ZIP contains:

- `minimal-rmm-agent-windows-amd64.exe` — the full agent binary
- `run-agent.ps1` — PowerShell wrapper with connection settings embedded

Use this when:

- You don't need lateral movement
- The target has no outbound HTTPS to the RMM server at stage1 time (unusual)
- You prefer a single self-contained executable

## Architecture

```text
Operator                  RMM Server                     Target
  |                           |                              |
  |-- build (stageless=F) --> |                              |
  |<-- stage1.zip (20 KB) --- | stores Go agent at           |
  |                           | /tools/stage/{id}            |
  |                           |                              |
  |-- deploy stage1.exe ----> |                              |
  |   run-stage1.ps1          |                              |
  |                           | <-- GET /tools/stage/{id} -- |
  |                           | -- masked Go agent --------> |
  |                           |   (decoded + executed)       |
  |                           | <--- beacon ---------------  |
  |<-- session registered --- |                              |
```

## Staging mask

The Go agent is masked with a SHA-256 counter keystream:

```text
seed = staging_id_bytes (16) || block_index_big_endian (8)
keystream_block = SHA-256(seed)   # 32 bytes
```

Applied in 32-byte blocks over the entire binary. Implemented in both `server_rmm.py` (`staging_mask`) and `stage1.c` (`stageMask`).

## Implementation files

| File | Change |
|---|---|
| `server_rmm.py` | `build_go_agent()`: `is_staged` branch returns stage1 ZIP; build handler passes `staging_fn` with the 24-hour build TTL (`STAGING_BUILD_TTL_SECONDS`); `create_staging(payload, ttl_seconds)`; `staging_info()` + `GET /api/v1/agent/go/stage/{id}` status endpoint |
| `agent-go/stage1/c/stage1.c` | C launcher: WinHTTP fetch with HTTP status check, staging mask decode, write to `%TEMP%` (no elevation), launch, `stage1 ok:/stage1 failed:` status lines |
| `agent-go/stage1/main.go` | Go stage1 (fallback): same temp-directory write path and status lines |
| `mcp_rmm_server.py` | `build_go_agent()`: `stageless` parameter; `stage_status()` tool |
| `rmm_tools.py` | OPENAI_TOOLS schemas: `stageless`, `stage_status`; `build_go_agent` handler forwards `stageless` |
| `rmm_cli.py` | `--stageless` argparse help; `agent go-stage <staging_id>` status command |
| `web/index.html` | Stageless checkbox: tooltip corrected, label updated |
| `web/go-agent.js` | Reads and POSTs `stageless` flag |

## Requirements

- `x86_64-w64-mingw32-gcc` (package: `mingw-w64`) for the compact C stage1. Falls back to Go stage1 if not available.
- The RMM server must be reachable from the target at the time stage1 runs (the `BASE_URL` is baked into stage1 at build time).
- The staging entry lives in server memory: restart the RMM server and staged builds must be rebuilt. The entry expires 24 hours after the build or on first successful fetch.

## Dedicated lateral service

The normal C and Go stage1 launchers are standalone programs with no Windows
service lifecycle. PsExec uses a separate compact service host built from
`agent-go/lateral-service/c/service.c`. It compiles the shared stage1 worker code
into its own image and runs that worker on a native thread. The agent embeds
`lateral-service.bin` and transfers only that host to ADMIN$. The calling agent
waits for worker completion, deletes the temporary service, and
removes the host binary after image handles close. It leaves the new agent alone.

A stopped worker permits cleanup even when staging failed; failure results retain
the exact deployment names and report failure separately from cleanup success.
The source records cleanup authority before waiting and removes it on successful
cleanup. It does not restore that authority from a stale timeout result. The
server can retry owned cleanup without target registration; the source checks
that the service is stopped before removing artifacts.

For PsExec, the worker budget is shorter than the server response timeout by the
source beacon sleep plus maximum jitter and 15 seconds, capped at half the total
timeout. This reserves time for delivery and the final result. An offline source,
exceptionally delayed delivery, or blocked network calls can still make the
result inconclusive; registration alone does not prove artifact cleanup.

A target-architecture MinGW compiler is required for the dedicated service.
The Go standalone launcher remains a manual-deployment fallback; it is never
substituted for the dedicated service. Rebuild and redeploy the calling agent
to obtain the new embedded host.

Builds serialize replacement of the shared service embed through completion of
the Go compilation. Concurrent amd64 and arm64 builds keep their own service
architecture; their compiler outputs and archives still use unique paths.

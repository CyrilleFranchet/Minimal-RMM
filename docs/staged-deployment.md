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

stage1 connects back to the RMM server over HTTPS, fetches the masked Go agent, decodes it, writes it to a temp path, and executes it. The session ID is fixed at build time so the agent registers with a known ID on first beacon.

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

Applied in 32-byte blocks over the entire binary. Implemented in both `server_rmm.py` (`_mask_staging_payload`) and `stage1.c` (`stageMask`).

## Implementation files

| File | Change |
|---|---|
| `server_rmm.py` | `build_go_agent()`: `is_staged` branch returns stage1 ZIP; HTTP handler passes `staging_fn=srv.create_staging` |
| `agent-go/stage1/c/stage1.c` | C launcher: WinHTTP fetch, staging mask decode, write + exec |
| `mcp_rmm_server.py` | `build_go_agent()`: `stageless` parameter, corrected docstring |
| `rmm_tools.py` | OPENAI_TOOLS schema: `stageless` description corrected |
| `rmm_cli.py` | `--stageless` argparse help corrected |
| `web/index.html` | Stageless checkbox: tooltip corrected, label updated |
| `web/go-agent.js` | Reads and POSTs `stageless` flag |

## Requirements

- `x86_64-w64-mingw32-gcc` (package: `mingw-w64`) for the compact C stage1. Falls back to Go stage1 if not available.
- The RMM server must be reachable from the target at the time stage1 runs (the `BASE_URL` is baked into stage1 at build time).

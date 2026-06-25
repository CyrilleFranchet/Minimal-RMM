# Web UI — PowerShell agent generator

Operators can build a **ready-to-run `client_rmm.ps1`** from the web console without hand-editing the script.

## Location

Sidebar → **Deploy agent (PowerShell)** (visible after login).

## Fields

| Field | Maps to |
|-------|---------|
| Server URL | `$u` / `RMM_BASE_URL` (no trailing slash) |
| Beacon secret | `$beaconSecret` / `RMM_BEACON_SECRET` |
| Session ID | New GUID each run, or fixed `$sessionId` |
| Sleep / Jitter / Max retries | `$baseSleepSeconds`, `$jitterPercent`, `$maxRetries` |
| HTTP proxy | `$httpProxy` / `RMM_HTTP_PROXY` |
| Persistent HTTP | `$persistentHttp` / `RMM_PERSISTENT_HTTP` |
| Proxy default credentials | `$httpProxyUseDefaultCredentials` |
| Verbose HTTP | `$verboseHttp` / `RMM_VERBOSE` |

## Output modes

1. **Full script** (default) — fetches `client_rmm.ps1` from the server, patches the configuration block, and offers **Copy script** / **Download .ps1**
2. **Config snippet only** — paste over lines 45–57 in an existing copy of the agent
3. **Environment variables** — shell block before launching an unmodified script

## API

`GET /api/v1/agent/script` (operator Bearer token) returns:

```json
{ "filename": "client_rmm.ps1", "content": "..." }
```

The Web UI uses this endpoint; operators can also fetch the template with curl for automation.

## Deploy steps

1. Open **Deploy agent (PowerShell)** and fill in server URL + beacon secret.
2. **Download .ps1** or **Copy script** (full script mode).
3. Save as `client_rmm.ps1` on the Windows lab host.
4. Run: `powershell -ExecutionPolicy Bypass -File .\client_rmm.ps1`

Form values are stored in `sessionStorage` for this browser tab only. The generated script is built in the browser; only the template fetch hits the server.

## Related

- Agent config block: `client_rmm.ps1` (lines 45–83)
- README: Client (`client_rmm.ps1`) section

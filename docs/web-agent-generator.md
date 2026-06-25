# Web UI — PowerShell agent generator

Operators can build a **`client_rmm.ps1` configuration snippet** from the web console without hand-editing the script.

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

1. **Script variables** — paste over lines 45–57 in `client_rmm.ps1`
2. **Environment variables** — shell block before launching the script (timing still comes from the script or server `__CONFIG__`)

## Deploy steps

1. Copy `client_rmm.ps1` to the Windows lab host.
2. Generate configuration in the web UI; **Copy configuration**.
3. Paste into the script config block (or set env vars).
4. **Copy** the run command: `powershell -ExecutionPolicy Bypass -File .\client_rmm.ps1`

Form values are stored in `sessionStorage` for this browser tab only (not sent to the server).

## Related

- Agent config block: `client_rmm.ps1` (lines 45–83)
- README: Client (`client_rmm.ps1`) section

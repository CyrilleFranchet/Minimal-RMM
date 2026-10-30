# Web UI — PowerShell agent generator

Operators can build a **ready-to-run `client_rmm.ps1`** from the web console without hand-editing the script. The repository source is stored at `agent-powershell/client_rmm.ps1`; the API continues to return the downloaded artifact as `client_rmm.ps1`.

## Location

Sidebar → **Deploy agent (PowerShell)** (pinned to the bottom of the sidebar; session lists scroll above it).

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

Form values are synchronized to the authenticated server/workspace SQLite UI
state. Browser storage remains a short-lived cache. The generated script is
built in the browser; only the template fetch hits the server.

## Server-side configured rendering (AI/CLI autonomy)

`POST /api/v1/agent/deploy/script` renders the template server-side with the
active beacon URL and the server's own `RMM_BEACON_SECRET`, and stores the
result under `RMM_logs/agent-deploy/`. The response contains the artifact
path, size, SHA-256, base URL, and download URL — never the secret. The AI
tool `deploy_agent_script` (MCP) and the CLI command `agent deploy-script`
use this endpoint, so a deployment script can be produced, staged with
`queue_upload`, and executed laterally without any manual WebUI step. When no
beacon transport is active, the endpoint answers `no_active_beacon_url`.

## Go agent build

The sidebar has a separate **Deploy agent (Go)** section below the PowerShell
section. It uses
`GET /api/v1/agent/go` to display the checked-in source and
`POST /api/v1/agent/go/build` to compile a fixed target on the server. See
`docs/go-agent.md` for the supported targets and current feature parity.

## Related

- Agent config block: `client_rmm.ps1` (lines 45–83)
- README: Client (`client_rmm.ps1`) section

### Portable artifact uploads

Deployment and Go build responses include `artifact_ref`, such as
`artifact:agent-deploy/client_rmm-example.ps1`. Pass this value as
`queue_upload.local_path` or the CLI upload source. `RmmApiClient.upload_file`
fetches the artifact from the configured server's authenticated artifact API
before uploading its bytes; it does not require a shared filesystem or write
secret-bearing files to the MCP host. Existing local file uploads still work.
Artifact access retains the server's existing workspace authorization rules.
Download failures stop the upload without returning artifact content.

The Go build endpoint requires `config` to be a JSON object when supplied;
null, arrays, strings, numbers, and booleans return HTTP 400.

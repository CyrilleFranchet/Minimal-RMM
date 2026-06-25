# minimal_rmm

A minimal **remote monitoring and management (RMM)** proof of concept: a Python HTTP server with a **REST operator API**, a **CLI client** for automation, and a Windows PowerShell beacon client.

**Use only on systems you own or are explicitly authorized to manage.** Misuse may violate law and policy.

---

## Architecture

| Component | Role |
|-----------|------|
| `server_rmm.py` | Threaded HTTP server: **beacon API** (`/register`, `/cmd`, `/result`) + **operator API** (`/api/v1/…`) in parallel |
| `rmm_cli.py` | Operator CLI — calls the REST API (scriptable, `--json`) |
| `web/` | Operator **web UI** — served at `/ui/` (same origin as API) |
| `client_rmm.ps1` | Windows beacon — unchanged protocol |

1. **Server** listens on HTTP. By default it runs **headless** (API only). Use `--cli` for the legacy embedded console.
2. **Beacon client** registers, polls `/cmd`, posts results to `/result`.
3. **Operator** uses `rmm_cli.py` (or any HTTP client) to list sessions, queue commands, wait for output, etc.

There is no interactive TCP shell: latency is at least one beacon interval (plus jitter).

---

## Security

The server **requires secrets by default** (no `--insecure`):

| Secret | Env / flag | Protects |
|--------|------------|----------|
| Operator API | `RMM_API_TOKEN` / `--token` | `/api/v1/*` — list sessions, queue commands, full control |
| Beacon | `RMM_BEACON_SECRET` / `--beacon-secret` | `/register`, `/cmd`, `/result`, `/ping` — impersonation / hijack |
| rclone exfil (optional) | `RMM_RCLONE_PROFILES` or `RMM_RCLONE_PROFILES_FILE` | Named cloud profiles; agent uploads via rclone (see `docs/rclone-exfil.md`) |

Also: listens on **127.0.0.1** by default (`--bind 0.0.0.0` only behind a firewall), **beacon session IDs** validated (no path-like `id`), artifact files use a **hash prefix** (not `session_id[:8]`), **path traversal** blocked on uploaded filenames, **10 MB** POST body cap, constant-time token checks.

**Lab only:** `python server_rmm.py --insecure` restores the old open API/beacon (never on a real network).

Set the same `RMM_BEACON_SECRET` on Windows clients (`$env:RMM_BEACON_SECRET = "…"`).

---

## Quick start

### Server (headless, recommended)

```bash
cd minimal_rmm
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt   # required for rmm_cli interactive; optional for server --cli
export RMM_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RMM_BEACON_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python server_rmm.py              # default port 8080, bind 127.0.0.1
python server_rmm.py 9000 --bind 0.0.0.0   # expose on LAN (use firewall + secrets)
# Optional rclone exfil (see docs/rclone-exfil.md):
python server_rmm.py --rclone-profiles /path/to/profiles.json
# or: export RMM_RCLONE_PROFILES_FILE="$PWD/tools/rclone/profiles.example.json"
```

### Operator CLI

```bash
export RMM_SERVER_URL="http://127.0.0.1:8080"
export RMM_API_TOKEN="change-me"   # if server uses a token

python rmm_cli.py                  # interactive console (default)
python rmm_cli.py health
python rmm_cli.py sessions list
python rmm_cli.py session use abc12345    # prefix ok (4+ chars)
python rmm_cli.py exec whoami --wait 120
python rmm_cli.py run "dir C:\\"          # queue only, no wait
python rmm_cli.py events --since 0
```

Selected session is stored in `~/.rmm_cli_state.json`.

### Web UI

With the server running, open **<http://127.0.0.1:8080/ui/>** (or your tunnel URL + `/ui/`). Paste your `RMM_API_TOKEN` to connect. The UI uses **WebSocket** (`/api/v1/ws`) for live session and output updates (new agents appear automatically; no manual refresh). The shell supports **↑/↓ command history** and **Tab completion** (session history + `cmd:` / `PS:` / `powershell:` / `pwsh:` prefixes). Completed agent downloads appear in the **Downloads from agent** panel. **Session history** lists archived transcripts from killed sessions. Sidebar **Deploy agent (PowerShell)** generates a ready-to-run **`client_rmm.ps1`** (or config/env snippet) — see `docs/web-agent-generator.md`. The token is kept in `sessionStorage` for the browser tab only.

**AI Assistant** (button **AI** in the header): opens a chat panel on the right. Set your **OpenAI API key** in the panel (stored in `sessionStorage` for this tab). The server runs an agent loop that spawns **`mcp_rmm_server.py`** over stdio and calls its tools (`POST /api/v1/ai/chat`). Optionally enable **Exegol MCP** in the panel to merge tools from a running [Exegol MCP](https://docs.exegol.com/mcp/getting-started) server (HTTP, default `http://127.0.0.1:8000/mcp`). Install `pip install -r requirements.txt` (includes `mcp`; Python 3.10+). Set `RMM_AI_USE_MCP=0` to call `rmm_tools` directly without MCP. Server env: `RMM_EXEGOL_MCP_URL`, `RMM_EXEGOL_MCP_TOKEN`. The selected session in the sidebar is passed as context.

Same-origin hosting avoids CORS; do not expose `/ui/` on the public internet without TLS and a strong API token. Sending an OpenAI key to your RMM server is only appropriate on a trusted/self-hosted instance.

### MCP server (Cursor / Claude Desktop)

Expose RMM operator actions as MCP tools for external AI clients:

```bash
pip install -r requirements-mcp.txt
export RMM_SERVER_URL=http://127.0.0.1:8080
export RMM_API_TOKEN=your-operator-token
python mcp_rmm_server.py
```

Copy `mcp.example.json` into your Cursor MCP config (`~/.cursor/mcp.json`) and fix the script path.

MCP tools mirror `rmm_cli.py` operator actions:

| CLI (interactive / subcommand) | MCP tool |
|-------------------------------|----------|
| `health` | `health` |
| `list` / `sessions list` | `list_sessions` |
| `info` / `session info` | `get_session` |
| `kill` | `kill_session` |
| `exec` | `exec_command` |
| `run` / bare command | `queue_command` |
| `persist` | `queue_persistent` |
| `stop` | `stop_persistent` |
| `set_sleep` / `set_jitter` / `config set-*` | `patch_config` |
| `events` | `get_events` |
| `download` | `queue_download` |
| `exfil` | `queue_exfil` |
| `rclone-config` | `get_rclone_config` |
| `upload` | `queue_upload` |
| `screenshot` | `queue_screenshot` |
| `socks list` | `list_socks` |
| `socks` / `socks stop` | `start_socks` / `stop_socks` |

Interactive-only: `use`, `background`, `clear`, `help`, `quit` (session selection is via `session_ref` on each tool).

### Embedded console (optional)

```bash
python server_rmm.py --cli
```

### Tunnel (example)

Start the server first and note the port in its startup banner (`RMM listening on …:PORT`). Point **cloudflared at that same port**:

```bash
python server_rmm.py 8081          # example: listen on 8081
cloudflared tunnel --url http://127.0.0.1:8081
```

Default port is **8080** if you omit the port argument. A mismatch (server on 8081, tunnel on 8080) causes **HTTP 524** from the client.

Use the HTTPS URL as `RMM_BASE_URL` on the client (no trailing slash).

### Windows client

```powershell
$env:RMM_BASE_URL = "https://your-tunnel-or-host.example.com"
$env:RMM_BEACON_SECRET = "same-value-as-server"
powershell -ExecutionPolicy Bypass -File .\client_rmm.ps1
```

---

## Operator REST API (`/api/v1/`)

Base: `http://<host>:<port>/api/v1/`

Send operator token on every `/api/v1/*` request:

- `Authorization: Bearer <token>`, or
- `X-RMM-Token: <token>`

Beacon endpoints require `X-RMM-Beacon-Token: <RMM_BEACON_SECRET>` (or query `beacon_token=`) unless the server was started with `--insecure`.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | `{"status":"ok","sessions":N}` |
| `GET` | `/sessions` | — | List active sessions |
| `GET` | `/sessions/{id}` | — | Session detail (`id` = full GUID or unique prefix) |
| `DELETE` | `/sessions/{id}` | — | Kill session (client gets `__EXIT__` on next beacon) |
| `PATCH` | `/sessions/{id}/config` | `{"sleep_seconds":60,"jitter_percent":30}` | Beacon tuning |
| `POST` | `/sessions/{id}/commands` | `{"command":"…","type":"oneshot\|persistent"}` | Queue command |
| `POST` | `/sessions/{id}/exec` | `{"command":"…","timeout":120}` | Queue and **wait** for next result event |
| `POST` | `/sessions/{id}/upload` | `{"remote_path":"…","content_b64":"…"}` | Queue `__UPLOAD__` |
| `POST` | `/sessions/{id}/download` | `{"remote_path":"…"}` | Queue `__DOWNLOAD__` |
| `POST` | `/sessions/{id}/exfil` | `{"remote_path":"…","profile":"mega-lab","dest":"…"}` | Queue `__EXFIL__` (agent rclone upload; link in events) |
| `GET` | `/rclone/config` | — | rclone binary + profile status |
| `POST` | `/sessions/{id}/screenshot` | — | Queue `__SCREENSHOT__` |
| `GET` | `/socks` | — | List active SOCKS relays (`relays[]`: url, session, agent, channel) |
| `POST` | `/sessions/{id}/socks` | `{"port":1080}` or `{"stop":true}` | Start/stop SOCKS5 on `127.0.0.1` via agent |
| `POST` | `/ai/chat` | `{"openai_api_key":"sk-…","messages":[…],"model":"gpt-4o-mini","selected_session_id":null,"exegol_mcp_enabled":false,"exegol_mcp_url":null,"exegol_mcp_token":null}` | OpenAI agent loop via MCP (RMM + optional Exegol) |
| `GET` | `/sessions/{id}/events?since=0&limit=50` | — | Poll result events (fallback) |
| `GET` | `/sessions/{id}/downloads` | — | List `__DOWNLOAD__` artifacts for session (remote path, size, artifact URL) |
| `GET` | `/history` | — | List archived (ended) session transcripts |
| `GET` | `/history/{id}` | — | Archived session metadata |
| `GET` | `/history/{id}/events?since=0&limit=500` | — | Read-only event transcript from disk |
| `DELETE` | `/history/{id}` | — | Remove archived transcript from disk (ended sessions only) |
| `GET` | `/artifacts/{downloads\|screenshots}/{filename}` | `?token=` | Download saved artifact (auth required) |
| `WS` | `/ws?token=…&session=…` | — | Live events + session list (WebSocket) |

### Automation example (curl)

```bash
curl -s -H "Authorization: Bearer $RMM_API_TOKEN" \
  "$RMM_SERVER_URL/api/v1/sessions" | jq .

curl -s -X POST -H "Authorization: Bearer $RMM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"whoami","timeout":180}' \
  "$RMM_SERVER_URL/api/v1/sessions/SESSION_PREFIX/exec" | jq .
```

---

## Beacon HTTP API (client)

Unchanged — used by `client_rmm.ps1`.

| Method | Path | Query | Response |
|--------|------|--------|----------|
| `GET` | `/register` | `id`, `h`, `u` | `REGISTERED` / `UPDATED` / `403 TERMINATED` |
| `GET` | `/cmd` | `id` | JSON `{"command":"…","type":"…"}` |
| `GET` | `/ping` | `id` | `PONG` |
| `GET` | `/tools/rclone.exe` | `id` | Beacon auth — agent bootstrap binary |
| `POST` | `/result` | `id`, `type` | Body: output, file JSON, screenshot base64, … |

See sections below for command tokens and result types.

---

## Command channel (beacon)

`GET /cmd?id=<session_id>` returns JSON:

| `type` | Meaning |
|--------|---------|
| `execute` | One-shot command |
| `persistent` | Repeated until `__STOP__` |
| `config` | `__CONFIG__ <sleep> <jitter>` |
| `none` | Empty (unknown session) |

### Server → client: special `command` values

| `command` | Purpose |
|-----------|---------|
| `__CONFIG__ <sleep> <jitter>` | Push beacon interval and jitter % |
| `__EXIT__` | Session killed — client exits |
| `__STOP__` | Clear persistent command |
| `__DOWNLOAD__ <path>` | Client uploads file (`type=file_upload`) |
| `__EXFIL__` + newline + JSON | Agent rclone upload to configured profile (`cloud_upload` in events) |
| `__UPLOAD__ <path>` + newline + JSON | Client writes remote file |
| `__SCREENSHOT__` | Screenshot PNG |
| `__KEYLOG__ start\|stop\|dump` | Keylogger |
| `__INSTALL_PERSIST__` / `__REMOVE_PERSIST__` | Client persistence hooks |

Other strings are user commands (`PS:`, `powershell:`, `pwsh:`, `cmd:` prefixes on the client).

Queue the same tokens via `rmm_cli.py run` / `exec` or `POST …/commands`.

---

## Result types (`POST /result`)

| `type` | Body |
|--------|------|
| `output` (default) | JSON `{"rmm_cmd":"…","rmm_output":"…"}` or plain text |
| `file_upload` | JSON with base64 `content` → `RMM_logs/downloads/` (large files sent in **6 MB chunks** with `offset` / `eof`; no fixed size cap) |
| `cloud_upload` | JSON with `link`, `dest`, `profile`, `remote_path`, `success`, … |
| `screenshot` | Base64 PNG → `RMM_logs/screenshots/` |
| `keylog` | Text → `RMM_logs/keylogs/` |
| `config_ack` | Logged / event stream |

Events are also exposed via `GET /api/v1/sessions/{id}/events`.

### SOCKS relay (`/socks`)

When an operator runs **`socks [port]`** (default **1080**), the server binds a **SOCKS5** listener on `127.0.0.1` and sets **`socks_active": true`** on **`GET /cmd`** (control only). The agent then starts a **dedicated background worker** (separate runspace from the main beacon). The worker prefers **WebSocket** on **`GET /socks`** (`Upgrade: websocket`, same IPv4 + `Host` routing as the beacon). If WebSocket fails (or **`$httpProxy`** is set), it falls back to **`GET/POST /socks`** polling. **`socks stop`** clears the relay and stops the worker. SOCKS log lines appear in the PowerShell client console. The main **`/cmd` / `/register` / `/result`** beacon is unchanged.

Use **`socks stop`** (or kill the session) to tear down.

**Troubleshooting:** On the agent you should see `[+] SOCKS WebSocket channel active` or `[+] SOCKS channel active (/socks HTTP poll)`, then `SOCKS outbound TCP host:port` when a tool uses the proxy. On the server: `SOCKS connect request` and `SOCKS remote connect ok`. Point tools at **`socks5://127.0.0.1:1080` on the machine running the server** (not the agent). Set `$verboseHttp = $true` for WS wire URL / Host debug lines.

**SMB / “NETBIOS connection timed out”:** The relay only tunnels **TCP** (e.g. port **445**). It does not carry NetBIOS name (137/138) or session (139) traffic. Prefer **`10.x.x.x:445`** or `\\10.x.x.x\share` through SOCKS, not a hostname that your PC resolves locally. Wireshark on the target path may show `STATUS_LOGON_FAILURE` (bad credentials on the remote host) while the Windows client still reports a generic NetBIOS timeout. Check server logs for `SOCKS remote connect ok` and agent logs for outbound TCP to the target IP.

| Beacon | Purpose |
|--------|---------|
| `GET /socks?id=…` | JSON poll **or** **WebSocket upgrade** on the same path (`Upgrade: websocket`); WS: server sends `{"op":"tasks",…}`, agent sends `{"op":"responses",…}` |
| `GET /socks-ws?id=…` | Alias for WebSocket upgrade only (optional) |
| `POST /socks?id=…` | HTTP fallback: agent posts `{"responses":[…]}` |

Operator API: `POST /api/v1/sessions/{id}/socks` with `{"port":1080}` or `{"stop":true}`.

---

## CLI reference (`rmm_cli.py`)

Run **`python rmm_cli.py`** with no arguments for an **interactive console** (like the server’s embedded CLI): `list`, `use <id>`, remote commands, `exec`, `download`, `upload`, `screenshot`, etc. Agent output streams in the background while a session is selected.

| Command | Description |
|---------|-------------|
| *(default)* / `interactive` | Interactive REPL |
| `health` | API health |
| `sessions list` | List sessions with **last seen** and **beacon_status** (`online` / `stale` / `offline`; `--json`) |
| `session use <id>` | Select session (saved in `~/.rmm_cli_state.json`) |
| `session info` | Session metadata |
| `session kill` | Kill session |
| `run <command>` | Queue command (`--type oneshot\|persistent`) |
| `exec <command>` | Run and wait (`--wait` seconds, `-f` command file) |
| `config set-sleep <n>` | 1–3600 |
| `config set-jitter <n>` | 0–100 |
| `download <remote_path>` | Queue `__DOWNLOAD__` |
| `exfil <remote_path> [--profile NAME]` | Queue rclone exfil from agent |
| `rclone-config` | Show rclone profiles + binary status |
| `upload <local> <remote>` | Queue `__UPLOAD__` |
| `socks [port]` / `socks stop` | SOCKS5 on 127.0.0.1 via remote agent (default port 1080) |
| `events` | Poll result events (`--since`, `--limit`) |

Global flags: `--url`, `--token` (or `RMM_SERVER_URL`, `RMM_API_TOKEN`).

---

## Embedded server CLI (`--cli`)

Same commands as before (`list`, `use`, `set_sleep`, shell commands, etc.). Prefer `rmm_cli.py` for automation.

---

## Client (`client_rmm.ps1`)

All settings live in a **configuration block at the top of the script** (`$u`, `$beaconSecret`, `$httpProxy`, …). Optional environment variables override those variables when set: `RMM_BASE_URL`, `RMM_BEACON_SECRET`, `RMM_HTTP_PROXY`, `RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS`, `RMM_PERSISTENT_HTTP`, `RMM_VERBOSE`. rclone exfil uses server-side profiles (no credentials in the agent script).

- **URL:** edit `$u` or set `RMM_BASE_URL`.
- **Beacon secret:** edit `$beaconSecret` or set `RMM_BEACON_SECRET` (must match the server).
- **HTTP proxy:** edit `$httpProxy` (e.g. `http://proxy.corp:8080`) or set `RMM_HTTP_PROXY` when the host cannot reach the tunnel directly. Use `$httpProxyUseDefaultCredentials` / `RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS=1` for Windows-integrated proxy auth.
- **Session id:** new GUID each run unless you set `$sessionId` in the script.
- **Registration:** retries **indefinitely** until the server is back. Re-registers every beacon and after errors so a restarted server is picked up automatically. Only stops on explicit server kill (`TERMINATED` / `__EXIT__`), not on network or auth errors.
- **Debug:** set `$verboseHttp = $true` or `RMM_VERBOSE=1` to log each HTTP call (logical URL, wire IPv4, `Host` header, status, error bodies).

**HTTP 524 (Cloudflare):** the tunnel reached Cloudflare but **cloudflared could not get a timely response from your origin**. Common cause: **port mismatch** — server on `8081` but `cloudflared tunnel --url http://127.0.0.1:8080`. Restart cloudflared with the port shown in the server banner (`Tunnel: cloudflared tunnel --url http://localhost:PORT`). Also ensure `server_rmm.py` is running on that host. This is not a wrong beacon token (that is usually `401`/`403`).

---

## Files

| Path | Role |
|------|------|
| `server_rmm.py` | HTTP server + operator API |
| `rmm_cli.py` | Operator CLI |
| `web/` | Static web operator UI (`index.html`, `app.js`, `agent-gen.js`, `style.css`) |
| `client_rmm.ps1` | Windows beacon |
| `requirements.txt` | `prompt_toolkit` (required for `rmm_cli.py` interactive) |
| `RMM_logs/` | Runtime logs and artifacts |
| `~/.rmm_cli_state.json` | CLI selected session |
| `~/.rmm_cli_history` | Operator command history (prompt_toolkit / readline) |
| `~/.RMM_history` | Embedded CLI history |

**Shared transcript:** commands and agent output for a session are stored on the server as **events** (`operator`, `output`, `config_ack`, …). Web UI and `rmm_cli.py` both poll `/api/v1/sessions/{id}/events` and receive WebSocket broadcasts — reopening a session shows the same history from any operator client.

---

## License

Add a `LICENSE` file if you want standard terms published on GitHub; this repo does not ship one by default.

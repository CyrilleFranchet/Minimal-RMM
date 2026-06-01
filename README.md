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

With the server running, open **http://127.0.0.1:8080/ui/** (or your tunnel URL + `/ui/`). Paste your `RMM_API_TOKEN` to connect. The UI uses **WebSocket** (`/api/v1/ws`) for live session and output updates, and supports **download**, **upload**, and **screenshot** actions. The token is kept in `sessionStorage` for the browser tab only.

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

Copy `mcp.example.json` into your Cursor MCP config (`~/.cursor/mcp.json`) and fix the script path. Tools: `list_sessions`, `get_session`, `exec_command`, `queue_command`, `patch_config`, `get_events`, `kill_session`, `queue_download`, `queue_screenshot`.

### Embedded console (optional)

```bash
python server_rmm.py --cli
```

### Tunnel (example)

```bash
cloudflared tunnel --url http://localhost:8080
```

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
| `POST` | `/sessions/{id}/screenshot` | — | Queue `__SCREENSHOT__` |
| `POST` | `/ai/chat` | `{"openai_api_key":"sk-…","messages":[…],"model":"gpt-4o-mini","selected_session_id":null,"exegol_mcp_enabled":false,"exegol_mcp_url":null,"exegol_mcp_token":null}` | OpenAI agent loop via MCP (RMM + optional Exegol) |
| `GET` | `/sessions/{id}/events?since=0&limit=50` | — | Poll result events (fallback) |
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
| `file_upload` | JSON with base64 `content` → `RMM_logs/downloads/` |
| `screenshot` | Base64 PNG → `RMM_logs/screenshots/` |
| `keylog` | Text → `RMM_logs/keylogs/` |
| `config_ack` | Logged / event stream |

Events are also exposed via `GET /api/v1/sessions/{id}/events`.

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
| `upload <local> <remote>` | Queue `__UPLOAD__` |
| `events` | Poll result events (`--since`, `--limit`) |

Global flags: `--url`, `--token` (or `RMM_SERVER_URL`, `RMM_API_TOKEN`).

---

## Embedded server CLI (`--cli`)

Same commands as before (`list`, `use`, `set_sleep`, shell commands, etc.). Prefer `rmm_cli.py` for automation.

---

## Client (`client_rmm.ps1`)

- **URL:** `RMM_BASE_URL` or edit `$u` in the script.
- **Beacon secret:** `RMM_BEACON_SECRET` (must match the server).
- **Session id:** new GUID each run unless changed in script.
- **Registration:** retries **indefinitely** until the server is back. Re-registers every beacon and after errors so a restarted server is picked up automatically. Only stops on explicit server kill (`TERMINATED` / `__EXIT__`), not on network or auth errors.
- **Debug:** `RMM_VERBOSE=1` logs each HTTP call (logical URL, wire IPv4, `Host` header, status, error bodies).

**HTTP 524 (Cloudflare):** the tunnel reached Cloudflare but the origin did not answer in time. On the host running `cloudflared`, ensure `python server_rmm.py --bind 0.0.0.0` is up and the tunnel targets `http://127.0.0.1:8080` (or your port). This is not a wrong beacon token (that is usually `401`/`403`).

---

## Files

| Path | Role |
|------|------|
| `server_rmm.py` | HTTP server + operator API |
| `rmm_cli.py` | Operator CLI |
| `web/` | Static web operator UI (`index.html`, `app.js`, `style.css`) |
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

# minimal_rmm

A minimal **remote monitoring and management (RMM)** proof of concept: a Python HTTP server with a **REST operator API**, a **CLI client** for automation, and a Windows PowerShell beacon client.

**Use only on systems you own or are explicitly authorized to manage.** Misuse may violate law and policy.

The repository documentation is organized as a [wiki-style index](docs/README.md).
The [architecture overview](docs/architecture.md) is the canonical map of the
control plane, Exegol workspaces, agents, plugins, pivots, storage, and
privilege boundaries.

---

## Architecture

The component summary below is intentionally short. Use the
[architecture overview](docs/architecture.md) for deployment diagrams and
current data-flow details.

| Component | Role |
|-----------|------|
| `server_rmm.py` | Threaded HTTP server with separate administration (`/api/v1/…`, `/ui/`) and beacon (`/register`, `/cmd`, `/result`, pivots) listeners |
| `rmm_profile.py` | Generic external transport process manager; Cloudflared is supplied by `Minimal-RMM-profiles` |
| `rmm_asset_store.py` | URL-installed plugin, AI skill, and rclone asset store |
| `rmm_plugin_releases.py` | Verified private GitHub release installation through [CLI, WebUI, and MCP](docs/plugin-releases.md) |
| `rmm_embeddings.py` | Saved embedding profiles and explicit result collections; [CLI/WebUI/MCP guide](docs/document-embeddings.md) |
| `rmm_ui_state.py` | SQLite-backed operator WebUI configuration state |
| `rmm_cli.py` | Operator CLI — calls the REST API (scriptable, `--json`) |
| `web/` | Operator **web UI** — served at `/ui/` (same origin as API) |
| `rmm_tun.py` + `server-tun/` | TUN lifecycle and user-space IP stack frontend; ownership follows storage mode |
| `rmm_workspace.py` | Workspace registry and bounded transient event broker for multi-operator mode |
| `workspace_service.py` | Per-operator local API, SQLite persistence, artifact store, and upstream proxy |
| `workspace_pivot.py` | Workspace-local SOCKS/TUN listeners and central transport bridge |
| `Dockerfile` + `docker-compose.yml` | Containerized control plane and optional workspace sidecar |
| `agent-powershell/client_rmm.ps1` | Windows PowerShell beacon — unchanged protocol |

1. **Server** listens on HTTP. By default it runs **headless** (API only). Use `--cli` for the legacy embedded console.
2. **Beacon client** registers, polls `/cmd`, posts results to `/result`.
3. **Operator** uses `rmm_cli.py` (or any HTTP client) to list sessions, queue commands, wait for output, etc.

There is no interactive TCP shell: latency is at least one beacon interval (plus jitter).

The default `RMM_STORAGE_MODE=server` keeps the single-server deployment. Set
`RMM_STORAGE_MODE=workspace` to use one local workspace service per operator or
Exegol container. In workspace mode, the central server routes sessions and
agent data but the workspace owns durable transcripts, artifacts, AI chat, the
SOCKS listener, and the TUN device. See
[`docs/workspace-deployment.md`](docs/workspace-deployment.md).

---

## Security

The server **requires secrets by default** (no `--insecure`):

| Secret | Env / flag | Protects |
|--------|------------|----------|
| Operator API | `RMM_API_TOKEN` / `--token` | `/api/v1/*` — list sessions, queue commands, full control |
| Beacon | `RMM_BEACON_SECRET` / `--beacon-secret` | `/register`, `/cmd`, `/result`, `/ping` — impersonation / hijack |
| rclone exfil (optional) | `RMM_RCLONE_PROFILES` or `RMM_RCLONE_PROFILES_FILE` | Named cloud profiles in server or workspace scope; agent uploads via rclone (see `docs/rclone-exfil.md`) |

Also: listens on **127.0.0.1** by default (`--bind 0.0.0.0` only behind a firewall), **beacon session IDs** validated (no path-like `id`), artifact files use a **hash prefix** (not `session_id[:8]`), **path traversal** blocked on uploaded filenames, **32 MB** default POST body cap (`RMM_MAX_BODY_BYTES`), constant-time token checks.

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
python3 run_server.py             # ensure the server TUN helper, then start on 8080/8081
python3 run_server.py --admin-port 9000 --beacon-port 9001 --bind 0.0.0.0 \
  --beacon-bind 0.0.0.0                         # expose listeners deliberately
# Optional rclone exfil (see docs/rclone-exfil.md):
# Server mode: profiles are loaded by the RMM server.
python3 run_server.py --rclone-profiles /path/to/profiles.json
# or: export RMM_RCLONE_PROFILES_FILE="$PWD/tools/rclone/profiles.example.json"

# Advanced: start without rebuilding local artifacts
python3 server_rmm.py
```

### Docker control plane and Exegol workspace

The Compose deployment separates the central broker from the operator data
plane. Copy `.env.example` to `.env`, replace all placeholder secrets, then
start the control plane and the example workspace sidecar:

```bash
cp .env.example .env
# Create tools/rclone/profiles.json with the operator's remotes.
docker compose up --build -d rmm-control
docker compose --profile workspace up --build -d
```

Open the workspace UI at **<http://127.0.0.1:8787/ui/>**. Its SQLite database,
artifacts, history, and local TUN state live in the workspace volume. The
control plane does not receive `/dev/net/tun` or `CAP_NET_ADMIN`. The rclone
profile file is mounted into the workspace service only. The central control
plane receives the selected job payload transiently and does not load the
workspace profile file. An existing Exegol container can run
`workspace_service.py` instead of the example sidecar; set its local
`RMM_RCLONE_PROFILES_FILE`, `RMM_RCLONE_DEFAULT_PROFILE`, and
`RMM_RCLONE_MAX_BYTES` values there. The full setup is in
[`docs/workspace-deployment.md`](docs/workspace-deployment.md).

The control plane mounts a persistent plugin store at `/app/plugins`. Build
agent plugins in the private
[`Minimal-RMM-plugins`](https://github.com/CyrilleFranchet/Minimal-RMM-plugins)
repository, then copy the release bundle's `*.dll` and `*.ps1` artifacts into
that directory. Set `RMM_PLUGIN_STORE=./plugin-store` in `.env` for a host bind
mount, or copy the files into the named Docker volume. The server does not
compile agent plugins. See
[`docs/plugin-architecture.md`](docs/plugin-architecture.md) and
[`docs/plugin-repository.md`](docs/plugin-repository.md).

The control plane also mounts a persistent RMM-Profile store at
`/app/profiles`. Install the trusted `cloudflared/` bundle from the private
[`Minimal-RMM-profiles`](https://github.com/CyrilleFranchet/Minimal-RMM-profiles)
repository and make `cloudflared` available in the control-plane image. With
`RMM_PROFILE_AUTOSTART=1`, the server starts the profile, extracts the generated
`trycloudflare.com` URL, and exposes it through `profile status` and the WebUI.
Use that URL for the agent beacon. See
[`docs/rmm-profiles.md`](docs/rmm-profiles.md).

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

With the server running, open **<http://127.0.0.1:8080/ui/>** (or your tunnel URL + `/ui/`). Paste your `RMM_API_TOKEN` to connect. In workspace mode, open the local workspace URL instead; the local service can use an optional `RMM_WORKSPACE_LOCAL_TOKEN` and does not need the global API token in the browser. Use **Config** in the header to export or import a versioned UI configuration JSON file; authenticated settings also persist in the server or workspace SQLite database, while the API token remains a browser login credential (see `docs/web-ui-configuration.md`). **Dark mode** is the default; use the **☀ / ☽** toggle in the header to switch. The UI uses **WebSocket** (`/api/v1/ws`) for live session and output updates in server mode. Workspace mode uses the same polling API while the local service owns the durable event stream. The shell queues commands with **Enter**, runs `/exec` wait via **Ctrl+Enter**, and supports **↑/↓ command history** plus **Tab completion** (session history + `cmd:` / `PS:` / `powershell:` / `pwsh:` prefixes). Type **`help`** or **`?`** in the shell for its local commands; all other commands are sent to the selected agent. The main agent workspace uses tabs for **Shell**, **Files & Screenshots**, and **Pivots**. Global **Downloads** and **Screenshots** controls in the application header list artifacts from every agent and show the originating agent. SOCKS5 and TUN can run independently at the same time (see `docs/web-socks.md`, `docs/downloads-browser.md`, `docs/tun-pivot.md`, and `docs/workspace-deployment.md`). **Session history** lists archived transcripts from killed sessions. The sidebar has separate **Deploy agent (PowerShell)** and **Deploy agent (Go)** sections — see `docs/web-agent-generator.md` and `docs/go-agent.md`.

**AI Assistant** (button **AI** in the header): opens a chat panel on the right. Configure and validate an **OpenAI**, **Anthropic**, or **Mistral** API key; only providers with a successfully validated key can be selected. Keys, validation state, model choice, Exegol MCP settings, and skill selection persist in the authenticated server or workspace SQLite UI state. The model list comes from the selected provider's API, and a custom model ID remains available. Conversation history is saved per session on the server in default mode (`RMM_logs/history/{id}/ai_chat.json`) and in the workspace SQLite database in workspace mode; use **Reset chat** to start over (see `docs/web-ai-chat-memory.md`). The server runs an agent loop that spawns **`mcp_rmm_server.py`** over stdio and calls its tools (`POST /api/v1/ai/chat`). Workspace-scoped AI tool calls forward the workspace headers to keep the same session ownership. See `docs/ai-providers.md` for the provider protocol and API details. **Skills** — built-in Markdown files and URL-installed workspace files can be enabled or disabled in the AI panel or Config panel and are injected into the system prompt (see `docs/web-ai-skills.md` and `docs/operator-assets.md`). Optionally enable **Exegol MCP** in the panel to merge tools from a running [Exegol MCP](https://docs.exegol.com/mcp/getting-started) server (HTTP, default `http://127.0.0.1:8000/mcp`). Install `pip install -r requirements.txt` (includes `mcp`; Python 3.10+). Set `RMM_AI_USE_MCP=0` to call `rmm_tools` directly without MCP. Server env: `RMM_EXEGOL_MCP_URL`, `RMM_EXEGOL_MCP_TOKEN`, `RMM_AI_SKILLS_DIR`. The selected session in the sidebar is passed as context.

The AI agent permits 32 tool rounds per internal checkpoint by default for multi-step tasks. The server automatically continues the existing provider conversation at each checkpoint, with an emergency ceiling of 160 total rounds. Set `RMM_AI_MAX_TOOL_ROUNDS` before starting the server to change the checkpoint size. If the emergency ceiling is reached, the web UI shows the tool-call trace from the failed request.

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

After changing operator tools, run `make check-parity` (see `docs/mcp-parity.md`).

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
| `pe list` | `list_plugins` |
| `pe load <plugin>` / `pe <plugin>` | `queue_pe_load` |
| `socks list` | `list_socks` |
| `socks` / `socks stop` | `start_socks` / `stop_socks` |
| `tun list` | `list_tun` |
| `tun start` / `tun stop` | `start_tun` / `stop_tun` |
| — | `list_history`, `get_history_session`, `get_history_events`, `delete_history`, `clear_history` |
| — | `list_session_downloads` |
| — | `list_artifacts` |
| — | `get_agent_script` |
| — | `queue_keylog`, `install_persistence`, `remove_persistence` |

Full REST ↔ MCP mapping: `docs/mcp-parity.md`.

Interactive-only: `use`, `background`, `clear`, `help`, `quit` (session selection is via `session_ref` on each tool).

### Embedded console (optional)

```bash
python server_rmm.py --cli
```

### RMM-Profile transport

The server defaults to administration port `8080` and beacon port `8081`.
Install the Cloudflared profile from
[`Minimal-RMM-profiles`](https://github.com/CyrilleFranchet/Minimal-RMM-profiles)
under `RMM_PROFILES_DIR`, make `cloudflared` available in the server
environment, and leave `RMM_PROFILE_AUTOSTART=1`. The server starts the profile,
forwards it to the beacon port, extracts the generated `trycloudflare.com` URL,
and publishes that URL through `profile status` or the WebUI.

```bash
python rmm_cli.py profile status
python rmm_cli.py profile start cloudflared
```

Use the returned HTTPS URL as `RMM_BASE_URL` on the client. Use the
administration URL for the CLI, MCP, and WebUI. To run without a profile for a
local lab, point the agent directly at the beacon listener URL.

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

Workspace services use the scoped pair `X-RMM-Workspace: <id>` and
`X-RMM-Workspace-Token: <workspace-token>`. The central server accepts either
the global operator token or a configured workspace pair; workspace requests
see only sessions claimed by that workspace.

Beacon endpoints require `X-RMM-Beacon-Token: <RMM_BEACON_SECRET>` (or query `beacon_token=`) unless the server was started with `--insecure`.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| `GET` | `/health` | — | `{"status":"ok","sessions":N}` |
| `GET` | `/workspace/candidates` | — | List active sessions that no workspace has claimed |
| `POST` | `/workspace/claims` | `{"session_id":"…"}` | Assign one active session to the authenticated workspace |
| `GET` | `/workspace/sessions` | — | List sessions claimed by the authenticated workspace |
| `GET` | `/workspace/events?since=N&limit=1000` | — | Read the bounded transient event queue for the workspace |
| `POST` | `/workspace/sessions/{id}/{socks\|tun}/tasks` | `{"tasks":[…]}` | Forward workspace-local pivot tasks to the central agent bridge |
| `GET` | `/workspace/sessions/{id}/{socks\|tun}/responses` | — | Drain agent responses for a workspace-local pivot |
| `GET` | `/sessions` | — | List active sessions |
| `GET` | `/sessions/{id}` | — | Session detail (`id` = full GUID or unique prefix) |
| `DELETE` | `/sessions/{id}` | — | Kill session (client gets `__EXIT__` on next beacon) |
| `PATCH` | `/sessions/{id}/config` | `{"sleep_seconds":60,"jitter_percent":30}` | Beacon tuning |
| `POST` | `/sessions/{id}/commands` | `{"command":"…","type":"oneshot\|persistent"}` | Queue command |
| `POST` | `/sessions/{id}/exec` | `{"command":"…","timeout":120}` | Queue and **wait** for next result event |
| `POST` | `/sessions/{id}/upload` | `{"remote_path":"…","content_b64":"…"}` | Queue `__UPLOAD__` |
| `POST` | `/sessions/{id}/download` | `{"remote_path":"…"}` | Queue `__DOWNLOAD__` |
| `POST` | `/sessions/{id}/exfil` | `{"remote_path":"…","profile":"mega-lab","dest":"…"}` | Queue `__EXFIL__` (agent rclone upload of file or folder; link in events for files) |
| `GET` | `/rclone/config` | — | Active server/workspace rclone profile and execution status |
| `GET` | `/rclone/capabilities` | — | Central plugin and binary bootstrap capabilities |
| `GET` | `/profile` | — | Installed and active RMM-Profile status |
| `POST` | `/profile` | `{"name":"cloudflared"}` or `{"stop":true}` | Start or stop the selected profile |
| `GET` | `/transport` | — | Administration URL, beacon URL, ports, and profile status |
| `GET` | `/assets` or `/assets/{kind}` | — | List URL-managed plugins, skills, and rclone files |
| `POST` | `/assets/{kind}` | `{"url":"https://…","name":"optional.ext"}` | Download and store an operator asset |
| `PATCH` | `/assets/{kind}/{name}` | `{"enabled":false}` | Enable or disable an asset |
| `DELETE` | `/assets/{kind}/{name}` | — | Remove an asset |
| `POST` | `/sessions/{id}/screenshot` | — | Queue `__SCREENSHOT__` |
| `GET` | `/plugins` | — | List agent plugin artifacts in the runtime plugin store (name, size, SHA-256) |
| `POST` | `/sessions/{id}/pe` | `{"plugin":"hello.dll","export":"Run","input":"…"}` or `{"plugin":"hello.dll","command":"scan","input":"…"}` | Queue a generic plugin load; plugin-owned commands are dispatched inside the DLL |
| `GET` | `/socks` | — | List active SOCKS relays (`relays[]`: url, session, agent, channel) |
| `POST` | `/sessions/{id}/socks` | `{"port":1080,"bind_host":"127.0.0.1"}` or `{"stop":true}` | Start/stop SOCKS5 via the agent pivot plugin |
| `GET` | `/tun` | — | List active TUN relays; listener/device ownership depends on storage mode |
| `POST` | `/sessions/{id}/tun` | `{"device":"tun://rmm0","routes":["10.10.0.0/16","10.20.0.0/16"]}` or `{"stop":true}` | Start/stop a TUN relay; server mode creates it centrally, workspace mode creates it in the local workspace |
| `GET` | `/ai/skills` | — | List server AI skills (metadata only) |
| `GET` | `/sessions/{id}/ai/chat` | — | Load persisted AI chat for session (live or archived) |
| `DELETE` | `/sessions/{id}/ai/chat` | — | Clear persisted AI chat |
| `POST` | `/ai/providers/validate` | `{"provider":"anthropic","api_key":"…"}` | Validate a configured OpenAI, Anthropic, or Mistral key and return its available models |
| `POST` | `/ai/chat` | `{"provider":"anthropic","api_key":"…","messages":[…],"model":"…","skill_ids":[…],…}` | Provider agent loop via MCP (RMM + optional Exegol); persists chat in the active storage owner |
| `GET` | `/sessions/{id}/events?since=0&limit=50` | — | Poll result events (fallback) |
| `GET` | `/sessions/{id}/downloads` | — | List `__DOWNLOAD__` artifacts for session (remote path, size, artifact URL) |
| `GET` | `/artifacts?kind=downloads|screenshots` | — | List downloads or screenshots from every agent with agent identity and session status |
| `GET` | `/history` | — | List archived (ended) session transcripts |
| `GET` | `/history/{id}` | — | Archived session metadata |
| `GET` | `/history/{id}/events?since=0&limit=500` | — | Read-only event transcript from disk |
| `DELETE` | `/history` | — | Remove all archived transcripts (ended sessions only) |
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
| `file_upload` | JSON with base64 `content` → server `RMM_logs/downloads/` in server mode or the workspace artifact store in workspace mode (varied **2 / 5 / 10 / 20 MB** raw chunks on `POST /result` with `offset` / `eof`; no fixed size cap) |
| `cloud_upload` | JSON with `link`, `dest`, `profile`, `remote_path`, `success`, … |
| `screenshot` | Base64 PNG → server `RMM_logs/screenshots/` or the workspace artifact store |
| `keylog` | Text → server `RMM_logs/keylogs/` or the workspace artifact store |
| `config_ack` | Logged / event stream |

Events are also exposed via `GET /api/v1/sessions/{id}/events`.

### SOCKS relay (`/socks`)

When an operator runs **`socks [port]`** (default **1080**), server mode binds a
**SOCKS5** listener on `127.0.0.1`; workspace mode starts that listener in the
local workspace service and leaves only the agent transport on the central
server. Both modes set **`socks_active": true`** on **`GET /cmd`** (control
only). The agent then starts the **SOCKS5 pivot plugin** outside the main
beacon loop. PowerShell agents fetch the deployed `socks.ps1` artifact through
the authenticated plugin endpoint and run its HTTP poll worker in a dedicated
runspace. Windows amd64 Go agents fetch the deployed `socks.dll` artifact and
run it in a cancellable child process. **`socks stop`** clears the relay and
stops the plugin. The main **`/cmd` / `/register` / `/result`** beacon is
unchanged.

Use **`socks stop`** (or kill the session) to tear down.

**Troubleshooting:** The listener is **SOCKS5 with no authentication** and
supports TCP `CONNECT`; the TUN frontend uses `UDP ASSOCIATE` through its
private relay. Configure `proxychains` with `socks5`, not `socks4` or `http`.
Check the central or workspace logs for plugin delivery/readiness, `SOCKS
connect request`, and `SOCKS remote connect ok`. A public Go SOCKS pivot needs
`socks-plugin` and `socks.dll`; a TUN relay needs `tun-plugin` and `tun.dll`.
Point tools at **`socks5://127.0.0.1:1080` in the namespace that owns the
listener** (the workspace container in workspace mode).

The server handles fast agent connect responses immediately, including responses that arrive before the waiting thread begins blocking.

**SMB / “NETBIOS connection timed out”:** The regular SOCKS relay carries **TCP** (e.g. port **445**). It does not carry NetBIOS name (137/138) or session (139) traffic. Prefer **`10.x.x.x:445`** or `\\10.x.x.x\share` through SOCKS, not a hostname that your PC resolves locally. Wireshark on the target path may show `STATUS_LOGON_FAILURE` (bad credentials on the remote host) while the Windows client still reports a generic NetBIOS timeout. Check server logs for `SOCKS remote connect ok` and agent logs for outbound TCP to the target IP. The TUN path adds UDP socket forwarding for tools that use normal UDP sockets.

| Beacon | Purpose |
|--------|---------|
| `GET /socks?id=…` | JSON poll for the shipped pivot plugins; the server also accepts a **WebSocket upgrade** (`Upgrade: websocket`) for compatible future plugins |
| `GET /socks-ws?id=…` | Alias for WebSocket upgrade only (optional) |
| `POST /socks?id=…` | HTTP fallback: agent posts `{"responses":[…]}` |

Operator API: `POST /api/v1/sessions/{id}/socks` with `{"port":1080}` or `{"stop":true}`.

### RMM-owned TUN relay (`/tun`)

In the default server storage mode, the RMM server creates and owns the TUN
device. In workspace mode, the central server keeps only the agent transport;
`workspace_service.py` creates the TUN device and routes inside the operator's
Exegol namespace. In both modes, `rmm-tun` provides the user-space IP/TCP/UDP
stack and the Go agent only opens ordinary target-side TCP and UDP sockets, so
it does not create a TUN device or require administrator privileges. This
currently requires a Windows amd64 Go agent with the built `tun.dll` transport.

Build the server-side TUN helper once. Install `tun.dll` from the external
plugin bundle before starting a Go-agent relay:

```bash
make tun-build
```

Start a relay and route target networks through it:

```bash
python rmm_cli.py tun start <session> tun://rmm0 \
  --route 10.10.0.0/16 --route 10.20.0.0/16 --interface eth0
```

Repeat `--route` for every target network. The process that owns the TUN device
must be able to create and configure it. On Linux, this requires
`/dev/net/tun` and permission to create network interfaces; a workspace
container needs the TUN device and `CAP_NET_ADMIN`. The central control-plane
container does not need either capability. On macOS, use an explicit `utun<N>`
device such as `utun4`.
Use `tun list` to inspect active relays and `tun stop <session>` to tear one
down. Full setup, Exegol routing, and current TCP/UDP limits are in
`docs/tun-pivot.md`.

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
| `exfil <remote_path> [--profile NAME]` | Queue rclone exfil (file or folder) from agent |
| `rclone-config` | Show rclone profiles + binary status |
| `upload <local> <remote>` | Queue `__UPLOAD__` |
| `socks [port]` / `socks stop` | SOCKS5 on 127.0.0.1 via remote agent (default port 1080) |
| `tun list` / `tun start` / `tun stop` | TUN relay through a Go agent; device ownership follows storage mode |
| `assets list/add/enable/disable/remove` | Manage URL-installed plugins, AI skills, and rclone files |
| `profile status/start/stop` | Manage the dynamic RMM-Profile connector |
| `events` | Poll result events (`--since`, `--limit`) |

Global flags: `--url`, `--token` (or `RMM_SERVER_URL`, `RMM_API_TOKEN`).

---

## Embedded server CLI (`--cli`)

Same commands as before (`list`, `use`, `set_sleep`, shell commands, etc.). Prefer `rmm_cli.py` for automation.

---

## Client (`client_rmm.ps1`)

All settings live in a **configuration block at the top of the script** (`$u`, `$beaconSecret`, `$httpProxy`, …). Optional environment variables override those variables when set: `RMM_BASE_URL`, `RMM_BEACON_SECRET`, `RMM_HTTP_PROXY`, `RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS`, `RMM_PERSISTENT_HTTP`, `RMM_VERBOSE`. rclone exfil uses server-side profiles in server mode and workspace-local profiles in workspace mode; credentials never go into the agent script.

- **URL:** edit `$u` or set `RMM_BASE_URL`.
- **Beacon secret:** edit `$beaconSecret` or set `RMM_BEACON_SECRET` (must match the server).
- **HTTP proxy:** edit `$httpProxy` (e.g. `http://proxy.corp:8080`) or set `RMM_HTTP_PROXY` when the host cannot reach the tunnel directly. Use `$httpProxyUseDefaultCredentials` / `RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS=1` for Windows-integrated proxy auth.
- **Session id:** new GUID each run unless you set `$sessionId` in the script.
- **Registration:** retries **indefinitely** until the server is back. Re-registers every beacon and after errors so a restarted server is picked up automatically. Only stops on explicit server kill (`TERMINATED` / `__EXIT__`), not on network or auth errors.
- **Debug:** set `$verboseHttp = $true` or `RMM_VERBOSE=1` to log each HTTP call (logical URL, wire IPv4, `Host` header, status, error bodies).

**HTTP 524 (Cloudflare):** the profile reached Cloudflare but could not get a timely response from the beacon origin. Check `python rmm_cli.py profile status`, confirm that `beacon_port` matches the server listener, and inspect the profile process log. The administration port is not the Cloudflared origin. A wrong beacon token usually returns `401` or `403`.

---

## Files

| Path | Role |
|------|------|
| `server_rmm.py` | HTTP server + operator API |
| `rmm_profile.py` | Generic RMM-Profile process contract and lifecycle |
| `rmm_asset_store.py` | Persistent URL-installed operator assets |
| `rmm_cli.py` | Operator CLI |
| `web/` | Static web operator UI (`index.html`, `app.js`, `agent-gen.js`, `style.css`) |
| `rmm_workspace.py` | Workspace authentication and transient central event delivery |
| `workspace_service.py` | Local per-operator WebUI, persistence, and upstream API proxy |
| `workspace_pivot.py` | Local SOCKS/TUN listeners and pivot task synchronization |
| `Dockerfile` / `docker-compose.yml` | Containerized control plane and workspace sidecar |
| `agent-powershell/client_rmm.ps1` | Windows beacon source |
| `requirements.txt` | `prompt_toolkit` (required for `rmm_cli.py` interactive) |
| `RMM_logs/` | Server-mode runtime logs/artifacts and central control-plane state |
| `RMM_WORKSPACE_DATA` | Workspace-mode SQLite database, history, and artifacts inside each operator container |
| `~/.rmm_cli_state.json` | CLI selected session |
| `~/.rmm_cli_history` | Operator command history (prompt_toolkit / readline) |
| `~/.RMM_history` | Embedded CLI history |

**Shared transcript:** commands and agent output for a session are stored as
**events** (`operator`, `output`, `config_ack`, …) by the active storage
owner. Server mode keeps them on the server; workspace mode persists them in
the operator's local workspace. Web UI and `rmm_cli.py` both poll
`/api/v1/sessions/{id}/events` and receive WebSocket broadcasts in server mode
— reopening a session shows the same history from any client in that mode.
Workspace mode uses the local polling service, which owns the durable event
stream. Large live WebSocket event bodies stay bounded, and the web UI
hydrates truncated live results from the full REST transcript (see
`docs/web-live-full-results.md`).

In workspace mode, the central broker keeps the event queue only long enough
for the authenticated workspace service to consume it. The local SQLite
database is the transcript owner, and the local service exposes the same API
paths to its WebUI and CLI. Central `RMM_logs` contains control-plane session
metadata and build artifacts, not workspace transcripts or operator files.

---

## License

Add a `LICENSE` file if you want standard terms published on GitHub; this repo does not ship one by default.

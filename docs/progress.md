# Progress Log

> Living document — update at the end of every work session.  
> Claude reads this via `/catchup` (`/.claude/commands/catchup.md`).

## Current Status

| Field | Value |
|-------|--------|
| **Phase** | Web shell completion (§4) shipped; traffic charts / §6 bug next |
| **Branch** | `main` |
| **Last updated** | 2026-06-17 |
| **HEAD** | `ec4392e` — global SOCKS relay list (`GET /api/v1/socks`) |
| **Commits** | ~60 since initial import |
| **Tests** | None in-repo (manual lab validation only) |

---

## Repository map (from code)

| Path | Lines (approx.) | Role |
|------|-----------------|------|
| `server_rmm.py` | ~2k | Threaded HTTP server: beacon + `/api/v1` operator API + embedded `--cli` |
| `client_rmm.ps1` | ~2k | Windows PowerShell beacon, command execution, SOCKS worker |
| `rmm_cli.py` | ~1.3k | Operator CLI (`RmmApiClient`), interactive REPL + subcommands |
| `rmm_socks.py` | ~570 | SOCKS5 listener on server; task queue; TCP relay via agent |
| `rmm_ws.py` | ~210 | Stdlib WebSocket (operator event hub + agent SOCKS channel) |
| `rmm_tools.py` | ~430 | Shared operator tools (MCP + web AI fallback) |
| `mcp_rmm_server.py` | ~170 | FastMCP server (16 tools) |
| `rmm_mcp_client.py` | ~290 | Spawns MCP over stdio; optional Exegol MCP merge |
| `rmm_ai.py` | ~280 | OpenAI chat loop with RMM (+ Exegol) tools |
| `web/` | static | Operator UI (`/ui/`), WebSocket events, AI panel (`ai.js`) |
| `rmm_run_on_host.py` | ~215 | Batch recon by hostname (API automation example) |
| `rmm_kill_host_sessions.py` | ~115 | Kill all sessions matching hostname |
| `docs/progress.md` | — | This file |
| `CLAUDE.md` | — | Agent onboarding / architecture summary |

Runtime artifacts: `RMM_logs/{downloads,screenshots,keylogs}`, `~/.rmm_cli_state.json`, `~/.rmm_cli_history`.

---

## Architecture (as implemented)

```text
                    ┌─────────────────────────────────────────┐
                    │           server_rmm.py                 │
                    │  RMMServer + RMMHandler (threaded)      │
                    ├─────────────────────────────────────────┤
 Beacon (secret)    │  /register /cmd /result /ping /socks    │
                    │  SocksManager ← rmm_socks.py              │
 Operator (token)   │  /api/v1/*  +  /api/v1/ws (events)      │
                    │  /ui/* static  +  POST /api/v1/ai/chat  │
                    └──────────────┬──────────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         ▼                         ▼                         ▼
  client_rmm.ps1            rmm_cli.py / web/          mcp_rmm_server.py
  (main loop +              (REST)                     (FastMCP tools)
   SOCKS runspace)
```

**Beacon model:** poll-based, no interactive TCP shell. Latency ≥ one sleep interval (+ jitter). Commands are JSON on `GET /cmd`; results on `POST /result`.

**SOCKS model:** SOCKS5 binds on **operator host** (`127.0.0.1:N`). Bytes traverse: local SOCKS client → server listener → task queue → agent (WS or HTTP `/socks`) → remote TCP on agent LAN.

---

## Development timeline (git)

1. **Foundation** (`29d7cbb` … `6f519dd`) — minimal server + PS client; REST API; headless mode; `rmm_cli.py`; web UI; beacon secret; WebSocket events; file API.
2. **Operator UX** (`e72b27e` … `89e2a9b`) — interactive CLI default; prompt_toolkit; auth/connection fixes.
3. **Security hardening** (mid history) — dual tokens, session ID validation, artifact path safety, `secure_compare`, default `127.0.0.1` bind.
4. **AI / MCP** — `rmm_tools.py`, `mcp_rmm_server.py`, web AI panel, `rmm_ai.py`, optional Exegol MCP (`rmm_mcp_client.py`).
5. **SOCKS relay** (`594c0cd` … `ec4392e`) — dedicated `/socks` channel; runspace isolation (PS 5.1/7); WebSocket pull protocol; Cloudflare/tunnel fixes; reliability + global `socks list`.

---

## Completed (by subsystem)

### Server (`server_rmm.py`, `rmm_ws.py`)

- [x] `ThreadingHTTPServer` — concurrent beacons, operator API, WebSocket handlers
- [x] Session registry: register, touch, kill, prefix resolution, `beacon_status` (online/stale/offline)
- [x] Command queue: oneshot FIFO, persistent until `__STOP__`, idle `__CONFIG__` push
- [x] Result handling: `output`, `file_upload`, `mega_upload`, `fileio_upload` (legacy), `screenshot`, `keylog`, `config_ack`
- [x] Event transcript per session (`deque`, max 500); operator action logging; WebSocket broadcast (`OperatorEventHub`)
- [x] Operator REST `/api/v1`: health, sessions CRUD, config PATCH, commands, exec (blocking), upload, download, mega, screenshot, socks, events, artifacts, AI chat
- [x] `GET /api/v1/socks` — list active relays with hostname, WS channel, tunnel count
- [x] Embedded `--cli` (readline / prompt_toolkit): full command set including keylog, persist, socks list
- [x] Security: `RMM_API_TOKEN`, `RMM_BEACON_SECRET`, `--insecure` lab flag; `MAX_BODY_BYTES` via `RMM_MAX_BODY_BYTES` (default 32 MB); path traversal guards on artifacts
- [x] Chunked download reassembly (`_save_file_upload`: `.part` staging, `upload_id` / `offset` / `eof`)
- [x] `GET /api/v1/sessions/{id}/downloads` — per-session download artifact index (`download_artifacts`, disk backfill)
- [x] `queue_agent_download` / `register_download_artifact` — track remote path from queue + agent `remote_path` field
- [x] `queue_agent_mega` — `POST …/mega` queues `__MEGA__` (agent stages → server uploads to MEGA; link in events)
- [x] `GET /api/v1/mega/config` — masked MEGA account status for operators
- [x] Session transcript persistence — `RMM_logs/history/{id}/events.jsonl` + `meta.json`; archive on kill
- [x] `GET /api/v1/history` — list ended sessions; `GET …/history/{id}/events` read-only transcript

### SOCKS (`rmm_socks.py`)

- [x] `SessionSocksBridge` — SOCKS5 handshake, CONNECT, per-tunnel TCP relay
- [x] Task ops: `connect`, `send` (base64), `close`; pending sends until remote connect ack
- [x] `SocksManager` — per-session bridge; `list_relays()` for operator inventory
- [x] Agent WebSocket attach on `GET /socks` upgrade; pull-based task delivery (`pull` → `tasks`)
- [x] HTTP poll fallback when WS disconnected (`fetch_tasks` returns `[]` if WS up)
- [x] Per-tunnel `client_lock` on operator-side SOCKS socket (send vs recv thread safety)
- [x] `MAX_SENDS_PER_PULL` (32) to keep WS frames under proxy limits

### Windows client (`client_rmm.ps1`)

- [x] Config block + env overrides (`RMM_BASE_URL`, `RMM_BEACON_SECRET`, proxy, verbose, persistent HTTP)
- [x] Register with infinite retry; `sync=1` on first register to adopt script sleep/jitter
- [x] HTTP transport: IPv4-only tunnel resolution, `Host` header, optional corporate proxy + default credentials
- [x] User commands: bare `cmd.exe`, `cmd:`, `PS:` / `powershell:`, `pwsh:`; cwd tracking via `RMM_CWD_SIG`
- [x] Internal commands: `__DOWNLOAD__`, `__MEGA__`, `__UPLOAD__`, `__SCREENSHOT__`, `__KEYLOG__`, `__INSTALL_PERSIST__`, `__REMOVE_PERSIST__`, `__STOP__`, `__CONFIG__`
- [x] `__MEGA__` — chunked staging to server + `rmm_mega.py` upload to configured MEGA account (`RMM_MEGA_MAX_BYTES`, default 100 MB)
- [x] Chunked exfil (`Send-RmmFileDownload`, 6 MB chunks → `file_upload` with `remote_path` metadata)
- [x] Keylogger job (`__KEYLOG__ start|stop|dump`) → temp file → `keylog` result type
- [x] Persistence installer copies script to `%APPDATA%` + Run key (with current URL/sleep/jitter)
- [x] SOCKS: `Sync-RmmSocksChannelFromServer` on `socks_active` from `/cmd`; dedicated runspace worker
- [x] SOCKS WS: `Connect-RmmSocksClientWebSocket` (IPv4 + Host), pull loop, chunked responses, no cancelled `ReceiveAsync`
- [x] SOCKS HTTP fallback; 12× WS retry before fallback; host log queue → main console
- [x] Runspace isolation: `RmmHostAnchor`, `BeginInvoke`, function import into worker runspace (PS 5.1 safe)

### Operator CLI (`rmm_cli.py`)

- [x] `RmmApiClient` mirrors REST API
- [x] Interactive REPL (default): list, use, info, exec, run, persist, stop, files, screenshot, socks/socks list/socks stop, events, health, background streaming output
- [x] Subcommands: `health`, `sessions list`, `session use|info|kill`, `run`, `exec`, `config set-sleep|set-jitter`, `download`, `upload`, `events`, `socks list`
- [x] Session state `~/.rmm_cli_state.json`; tab completion; `--json` mode
- [x] `print_socks_relays()` for global SOCKS inventory

### Web UI (`web/`)

- [x] Login via API token (`sessionStorage`)
- [x] Session sidebar with beacon status, sleep/jitter display
- [x] Shell: queue command, exec (wait), kill session; **↑/↓ history + Tab completion** (§4)
- [x] Files: download queue, upload (base64), screenshot, **MEGA upload** (server account; link in transcript)
- [x] **Downloads from agent** panel — list `GET …/downloads`, download/preview, WS refresh on `file_upload`
- [x] Live session list — WebSocket + 12 s poll; client-side beacon status refresh; kill closes console
- [x] **Session history** sidebar — browse archived transcripts (`GET /api/v1/history`)
- [x] Beacon config apply (PATCH sleep/jitter)
- [x] WebSocket `/api/v1/ws` + polling fallback; shared event transcript with CLI
- [x] AI assistant panel (`ai.js` + `POST /api/v1/ai/chat`); OpenAI key in tab; optional Exegol MCP settings

### MCP & AI (`mcp_rmm_server.py`, `rmm_tools.py`, `rmm_ai.py`)

- [x] **18 MCP tools:** `health`, `list_sessions`, `get_session`, `exec_command`, `queue_command`, `queue_persistent`, `stop_persistent`, `patch_config`, `get_events`, `kill_session`, `queue_download`, `queue_mega`, `get_mega_config`, `queue_upload`, `queue_screenshot`, `list_socks`, `start_socks`, `stop_socks`
- [x] `session_ref` = hostname, id prefix, or full UUID (`_resolve_session_id`)
- [x] Web AI can use MCP stdio or direct `execute_tool` (`RMM_AI_USE_MCP=0`)

### Auxiliary scripts

- [x] `rmm_run_on_host.py` — find session by hostname, set sleep, run default recon command list
- [x] `rmm_kill_host_sessions.py` — kill all sessions for hostname

### Documentation

- [x] `README.md` — setup, API tables, SOCKS troubleshooting, MCP mapping
- [x] `CLAUDE.md` — project overview for agents
- [x] `docs/downloads-browser.md` — web downloads panel + `GET …/downloads` API
- [x] `docs/web-shell-completion.md` — shell ↑/↓ history and Tab completion
- [x] `mcp.example.json` — Cursor MCP config template

---

## Operator feature parity

| Capability | REST API | CLI interactive | CLI subcommand | MCP | Web UI | Server `--cli` |
|------------|:--------:|:---------------:|:--------------:|:---:|:------:|:--------------:|
| Health check | ✅ | ✅ | ✅ | ✅ | ✅ (on connect) | ❌ |
| List sessions | ✅ | ✅ `list` | ✅ | ✅ | ✅ | ✅ |
| Session detail | ✅ | ✅ `info` | ✅ | ✅ | ✅ (sidebar) | ✅ |
| Kill session | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Queue command | ✅ | ✅ / bare line | ✅ `run` | ✅ | ✅ | ✅ |
| Exec + wait | ✅ | ✅ `exec` | ✅ | ✅ | ✅ | ✅ |
| Persistent cmd | ✅ | ✅ `persist` | via API | ✅ | ❌ | ✅ |
| Stop persistent | ✅ | ✅ `stop` | via API | ✅ | ❌ | ✅ |
| Patch sleep/jitter | ✅ | ✅ `set_*` | ✅ `config` | ✅ | ✅ | ✅ |
| Download file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Upload to MEGA | ✅ | ✅ `mega` | ✅ | ✅ `queue_mega` | ✅ | ✅ `mega` |
| List session downloads | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| Session history (archived) | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| Upload file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Screenshot | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| SOCKS start/stop | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ |
| SOCKS list all | ✅ | ✅ `socks list` | ✅ `socks list` | ✅ | ❌ | ✅ `socks list` |
| Events / transcript | ✅ | ✅ | ✅ | ✅ | ✅ WS | ❌ |
| Keylog | queue only | ❌ | ❌ | ❌ | ❌ | ✅ |
| Install/remove persist | queue only | ❌ | ❌ | ❌ | ❌ | ✅ |

**Gaps to note:** Web UI and CLI subcommands lack SOCKS start/stop and keylog. MCP has no keylog/persist-install tools (use `queue_command` with `__KEYLOG__` / `__INSTALL_PERSIST__` if needed).

---

## In Progress

- [ ] None tracked in-repo.

---

## Up Next

- [ ] **Server restart / beacon config bugs** — see [tech plan §7](tech-plan.md#7-server-restart-resets-agent-sleepjitter-bug)
- [ ] **Web UI — queued command result placement** — see [tech plan §6](tech-plan.md#6-web-ui--queued-command-result-placement-bug): show results under echoed command, not at transcript tail
- [ ] **Web UI — traffic & beacon charts** — see [tech plan §1](tech-plan.md#1-beacon--traffic-visualization-web-ui): `GET …/metrics`, poll/byte counters, Chart.js panel
- [ ] **Web UI — interactive shell (cmd / PowerShell)** — see [tech plan §5](tech-plan.md#5-web-ui--interactive-shell-mode-cmd--powershell): shell mode selector, cwd prompt, `PS:` / `pwsh:` dispatch
- [ ] **Web UI:** SOCKS controls + global relay list (`GET /api/v1/socks`)
- [ ] **Chunked upload** (symmetry with download; large `content_b64` still single POST today)
- [ ] **CLI subcommands:** `screenshot`, `socks start|stop` (only interactive today)
- [ ] **MCP:** optional `queue_keylog` / `install_persistence` wrappers (or document `queue_command` tokens)
- [ ] **Tests:** SOCKS task ordering, chunked download reassembly, API auth, WS handshake
- [ ] **Docs:** `docs/prd.md`; fix README security line (still says 10 MB cap)
- [ ] **LICENSE** file (README notes absence)

---

## Key decisions

| Topic | Decision |
|-------|----------|
| Control plane | Beacon HTTP only (`/cmd`); SOCKS control via `socks_active` flag, not shell command |
| SOCKS data plane | WebSocket on `GET /socks` (upgrade); HTTP poll fallback; pull-based tasks (no push) |
| WS receive | Never cancel `ClientWebSocket.ReceiveAsync` (Aborted state on .NET) |
| Task order | Server returns connect/close before send; agent sorts connect → send → close |
| SOCKS bind | Listener on **RMM server** host; tools use `socks5://127.0.0.1:port` there |
| Downloads | 6 MB chunks, `upload_id` + `offset` + `eof`; server stages `.part` files |
| Register sync | First `/register` sends `s`, `j`, `sync=1` so server adopts client script timing |
| Operator surfaces | New features should land REST → CLI → MCP together (`rmm_tools.py`) |
| AI | OpenAI key from browser; server spawns MCP locally; Exegol optional |
| Security default | Dual secrets; localhost bind; no `--insecure` in production |

---

## Deviations from planned docs

- `docs/tech-plan.md` covers traffic charts; `docs/mega-upload.md` documents MEGA upload; `docs/downloads-browser.md` documents the web downloads panel (shipped).
- Agent `file_upload` JSON may include `remote_path` (full agent path) alongside `filename`.
- SOCKS uses custom JSON task protocol over WebSocket, not a generic byte-stream tunnel.
- Embedded `server_rmm.py --cli` remains alongside `rmm_cli.py` (duplicate UX).
- Keylog + persistence exist on client and embedded CLI but are intentionally absent from MCP/web.

---

## Known issues

| Issue | Detail |
|-------|--------|
| SMB / “NETBIOS timeout” via SOCKS | Relay is TCP-only (`:445`); not 137–139. Windows may show generic timeout on `STATUS_LOGON_FAILURE`. Use target IP. |
| Upload size | Single base64 POST per file; no chunking (downloads are chunked). |
| SOCKS throughput | Pull-loop latency; adequate for interactive use, not bulk transfer optimized. |
| Proxy idle WS | Cloudflare/tunnels may drop long-idle WebSockets; `KeepAliveInterval=20s` on agent. |
| ~~Register + Web UI WS deadlock~~ | **Fixed:** single `_io_lock` on operator WS blocked `broadcast_sessions` during idle `recv_json` → Cloudflare 524 (~100s). Split send/recv locks in `rmm_ws.py`; async/debounced session broadcast; lighter register path. |
| ~~Beacon hang after large results~~ | **Fixed:** `/result` waited for history write + full-body WS push before HTTP 200; slow clients could block origin. Now respond 200 immediately, process async; truncate WS event bodies; 15s WS send timeout; client shows `Beacon poll…` and reports failed result POSTs. |
| No automated tests | Regressions caught manually only. |
| Web UI queued results | Queued command output appends at transcript **tail** instead of under the echoed command — see [tech plan §6](tech-plan.md#6-web-ui--queued-command-result-placement-bug). |
| Server restart vs beacon config | After server restart: reconnecting agents may get default **`__CONFIG__ 60 30`**; operator PATCH/`Apply config` updates the server immediately but the agent can keep the old sleep for a full cycle — see [tech plan §7](tech-plan.md#7-server-restart-resets-agent-sleepjitter-bug). |
| README stale | Security section still mentions 10 MB body cap; default is 32 MB + chunking. |
| Web ↔ CLI parity | No SOCKS or keylog in web UI; no interactive cmd/PS mode or traffic/beacon charts yet. |

---

## Lab validation notes (2026-06-02)

- SOCKS end-to-end confirmed: SMB negotiate + NTLM through `socks5://127.0.0.1:1080` on server host.
- Wireshark on target path showed `STATUS_LOGON_FAILURE` (credentials), not relay failure.
- WS stability fixes: task order (`824a086`), Aborted recv (`a965e4d`), socket lock (`9fd848c`).

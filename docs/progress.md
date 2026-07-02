# Progress Log

> Living document — update at the end of every work session.  
> Claude reads this via `/catchup` (`/.claude/commands/catchup.md`).

## Current Status

| Field | Value |
|-------|--------|
| **Phase** | NDR detection investigation + idle /cmd padding |
| **Branch** | `main` |
| **Last updated** | 2026-07-02 |
| **HEAD** | idle /cmd padding for NDR activity filter |
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
- [x] Command queue: oneshot FIFO, persistent until `__STOP__`, idle `__CONFIG__` push (deferred until `config_synced`; `config_pending` after PATCH)
- [x] Load `sessions.json` on startup — restore sleep/jitter per session id after server restart
- [x] Result handling: `output`, `file_upload`, `cloud_upload`, `exfil_progress`, `download_progress`, `screenshot`, `keylog`, `config_ack`
- [x] Result events: `file_upload` / `screenshot` include `command` for web UI pairing
- [x] Event transcript per session (`deque`, max 500); operator action logging; WebSocket broadcast (`OperatorEventHub`)
- [x] Operator REST `/api/v1`: health, sessions CRUD, config PATCH, commands, exec (blocking), upload, download, exfil, screenshot, socks, events, artifacts, AI chat
- [x] `GET /api/v1/socks` — list active relays with hostname, WS channel, tunnel count
- [x] Embedded `--cli` (readline / prompt_toolkit): full command set including keylog, persist, socks list
- [x] Security: `RMM_API_TOKEN`, `RMM_BEACON_SECRET`, `--insecure` lab flag; `MAX_BODY_BYTES` via `RMM_MAX_BODY_BYTES` (default 32 MB); path traversal guards on artifacts
- [x] Beacon/API responses include explicit `Connection: close` and `close_connection = True` so HTTP/1.1 exchanges are terminated after each request/response (no `Content-Length` — see below)
- [x] Agent `HttpWebResponse` always closed via `try/finally` in `Invoke-RmmRestMethod` and `Save-RmmBeaconFile`; `Get-RmmHttpErrorBody` always closes response in its own `finally` — prevents TCP RST caused by unread data in the receive buffer when GC finalizes an unclosed response
- [x] **TCP RST fix** (`c9702d9` → `54b8d0d` → `f28a0b4` → `843d6fe`): `c9702d9` added `Content-Length` to fix HTTP framing; this caused `ReadToEnd()` to stop early, leaving the 85-byte Cloudflare `TLS close_notify` in the TCP receive buffer — `Socket.Close()` fires RST on unread data. `54b8d0d` removed `Content-Length` (server fix). `f28a0b4` forced `KeepAlive=true` on every `HttpWebRequest`: with `KeepAlive=false` .NET skips the pool-drain step and `Socket.Close()` races against the buffered alert; with `KeepAlive=true` the pool-drain reads those bytes first → FIN. To prevent `KeepAlive=true` from letting Cloudflare multiplex all beacons over one persistent TLS session: `be68622` assigns a random `ConnectionGroupName` per request (no pool sharing between requests) and `843d6fe` calls `ServicePointManager.FindServicePoint(...).CloseConnectionGroup(guid)` in the `finally` block to tear down the idle socket immediately. Net result: one TCP flow per beacon exchange, always closed with FIN.
- [x] Chunked download reassembly (`_save_file_upload`: `.part` staging, `upload_id` / `offset` / `eof`)
- [x] `GET /api/v1/sessions/{id}/downloads` — per-session download artifact index (`download_artifacts`, disk backfill)
- [x] `queue_agent_download` / `register_download_artifact` — track remote path from queue + agent `remote_path` field
- [x] `queue_agent_exfil` — `POST …/exfil` queues `__EXFIL__` (agent rclone upload; `cloud_upload` in events)
- [x] `GET /api/v1/rclone/config` — rclone binary + masked profile status; `GET /tools/rclone.exe` beacon bootstrap
- [x] Session transcript persistence — `RMM_logs/history/{id}/events.jsonl` + `meta.json`; archive on kill; **orphan archive on server restart** (`server_restart`)
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
- [x] Register with infinite retry; `sync=1` on first register and on `-Reconnect` to adopt script sleep/jitter
- [x] Fast poll after reconnect or config change (`RmmFastPoll`) — skip one sleep cycle for timely `/cmd`
- [x] HTTP transport: IPv4-only tunnel resolution, `Host` header, optional corporate proxy + default credentials
- [x] User commands: bare `cmd.exe`, `cmd:`, `PS:` / `powershell:`, `pwsh:`; cwd tracking via `RMM_CWD_SIG`; **hidden child processes** (no console flash) — see `docs/client-command-execution.md`
- [x] CMD execution — temp `.cmd` batch file + `WorkingDirectory` (no `/S /c ""` or embedded `cd /d`)
- [x] Internal commands: `__DOWNLOAD__`, `__EXFIL__`, `__UPLOAD__`, `__SCREENSHOT__`, `__KEYLOG__`, `__INSTALL_PERSIST__`, `__REMOVE_PERSIST__`, `__STOP__`, `__CONFIG__`
- [x] `__EXFIL__` — bootstrap rclone from server, ephemeral `RCLONE_CONFIG_*` env, `rclone copyto` / `rclone copy` (folders) + optional `link`; live `exfil_progress` POSTs during upload
- [x] Chunked exfil (`Send-RmmFileDownload`, varied **2 / 5 / 10 / 20 MB** chunks → `POST /result` `file_upload`; live `download_progress` POSTs; **paced by default** — `$downloadBurst` / `RMM_DOWNLOAD_BURST` for burst mode)
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
- [x] **Light / dark theme** — default dark; toggle on login + header; `localStorage` `rmm_theme` — see `docs/web-ui-theme.md`
- [x] **Deploy agent** sidebar — generate full `client_rmm.ps1` or config snippet; panel pinned to sidebar bottom (see `docs/web-agent-generator.md`)
- [x] **Resizable sidebar** — drag handle between sidebar and console; width stored in `sessionStorage`
- [x] Session sidebar with beacon status, sleep/jitter display, **first connection time** (`first_seen`); hover **Beacon** (sleep/jitter dialog) and **Kill** on live rows
- [x] Shell: queue command by default (**Enter**), exec wait via **Ctrl+Enter**, kill session; **↑/↓ history + Tab completion** (§4)
- [x] Shell **operator meta commands** — `exfil`, `download`, `screenshot` routed to REST (not agent `cmd.exe`); see `docs/web-shell-completion.md`
- [x] **Queued result placement** (§6) — command blocks; results render under echoed line via `ev.command` / tool kind
- [x] Files: download queue, upload (base64), screenshot, **rclone exfil** (profile dropdown + live upload progress bar, same UI as downloads); panel renamed **Files & screenshot** (beacon config moved to sidebar)
- [x] **Downloads from agent** panel — list `GET …/downloads`, download/preview, WS refresh on `file_upload`; live download progress bar in shell
- [x] Live session list — WebSocket + 12 s poll; client-side beacon status refresh; kill closes console
- [x] **Session history** sidebar — browse archived transcripts (`GET /api/v1/history`); hover **Delete** removes one archive; **Clear all** removes every ended archive (`DELETE /api/v1/history`)
- [x] Beacon config apply (PATCH sleep/jitter) — per-session **Beacon** button opens modal dialog
- [x] WebSocket `/api/v1/ws` + polling fallback; shared event transcript with CLI
- [x] AI assistant panel (`ai.js` + `POST /api/v1/ai/chat`); OpenAI key in tab; optional Exegol MCP settings; **server skills** (`ai-skills/*.md`, `GET /api/v1/ai/skills`) — see `docs/web-ai-skills.md`
- [x] **Full live results after WS truncation** — large WebSocket event bodies remain bounded, and the web UI fetches the full event body from REST when `body_truncated` is set; see `docs/web-live-full-results.md`
- [x] **AI chat memory** — per-session history on server (`RMM_logs/history/{id}/ai_chat.json`); **Reset chat**; purge on kill/delete — see `docs/web-ai-chat-memory.md`
- [x] **Server unreachable modal** — gray backdrop + Retry / Disconnect when REST health or API fetch fails (network); periodic health probe while connected

### MCP & AI (`mcp_rmm_server.py`, `rmm_tools.py`, `rmm_ai.py`)

- [x] MCP tools mirror REST operator API — see `docs/mcp-parity.md`; enforced by `make check-parity` (`scripts/check_operator_parity.py`)
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
- [x] `docs/client-command-execution.md` — hidden CMD/PS process launch on the agent
- [x] `docs/prd.md` — product vision; EDR/AV compatibility as commercial gate
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
| Cloud exfil (rclone) | ✅ | ✅ `exfil` | ✅ | ✅ `queue_exfil` | ✅ profile select | ✅ `exfil` |
| List session downloads | ✅ | ❌ | ❌ | ✅ `list_session_downloads` | ✅ | ❌ |
| Session history (archived) | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ |
| Delete archived history | ✅ | ❌ | ❌ | ✅ `delete_history` | ✅ per row | ❌ |
| Clear all archived history | ✅ | ❌ | ❌ | ✅ `clear_history` | ✅ header button | ❌ |
| Agent script (`GET /agent/script`) | ✅ | ❌ | ❌ | ✅ `get_agent_script` | ✅ | ❌ |
| Upload file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Screenshot | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| SOCKS start/stop | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ |
| SOCKS list all | ✅ | ✅ `socks list` | ✅ `socks list` | ✅ | ❌ | ✅ `socks list` |
| Events / transcript | ✅ | ✅ | ✅ | ✅ | ✅ WS | ❌ |
| Keylog | queue only | ❌ | ❌ | ✅ `queue_keylog` | ❌ | ✅ |
| Install/remove persist | queue only | ❌ | ❌ | ✅ `install_persistence` / `remove_persistence` | ❌ | ✅ |

**Gaps to note:** Web UI and CLI subcommands lack SOCKS start/stop. MCP parity requirement: `docs/mcp-parity.md`.

---

## In Progress

- [ ] None tracked in-repo.

---

## Up Next

- [ ] **Web UI — archived sessions missing entered commands** — see [tech plan §11](tech-plan.md#11-web-ui--archived-sessions-missing-entered-commands-bug): history replay should show operator-typed command lines, not only output or internal `__DOWNLOAD__` tokens
- [ ] **Web UI — traffic & beacon charts** — see [tech plan §1](tech-plan.md#1-beacon--traffic-visualization-web-ui): `GET …/metrics`, poll/byte counters, Chart.js panel
- [ ] **Web UI — interactive shell (cmd / PowerShell)** — see [tech plan §5](tech-plan.md#5-web-ui--interactive-shell-mode-cmd--powershell): shell mode selector, cwd prompt, `PS:` / `pwsh:` dispatch
- [ ] **Web UI:** SOCKS controls + global relay list (`GET /api/v1/socks`)
- [ ] **Chunked upload** (symmetry with download; large `content_b64` still single POST today)
- [ ] **CLI subcommands:** `screenshot`, `socks start|stop` (only interactive today)
- [ ] **Tests:** SOCKS task ordering, chunked download reassembly, API auth, WS handshake
- [ ] **EDR/AV compatibility (commercial gate)** — see [PRD](prd.md#non-negotiable-edr-and-antivirus-compatibility): signing, lab matrix, review persistence/SOCKS/keylog detections
- [ ] **Docs:** fix README security line (still says 10 MB cap)
- [ ] **Docker operator deploy** — deferred; full spec in [tech plan §12](tech-plan.md#12-docker-deployment-rmm-server--exegol-mcp) (phased: RMM-only compose first, then Exegol network + SOCKS, then Exegol MCP). Interim: venv + `python server_rmm.py` (host or Exegol container).

---

## Key decisions

| Topic | Decision |
|-------|----------|
| Commercial viability | **EDR/AV must not routinely block the agent** — see `docs/prd.md`; otherwise the product is not sellable |
| Control plane | Beacon HTTP only (`/cmd`); SOCKS control via `socks_active` flag, not shell command |
| SOCKS data plane | WebSocket on `GET /socks` (upgrade); HTTP poll fallback; pull-based tasks (no push) |
| WS receive | Never cancel `ClientWebSocket.ReceiveAsync` (Aborted state on .NET) |
| Task order | Server returns connect/close before send; agent sorts connect → send → close |
| SOCKS bind | Listener on **RMM server** host; tools use `socks5://127.0.0.1:port` there |
| Downloads | Varied chunk sizes (2 / 5 / 10 / 20 MB raw) on `POST /result` `file_upload`; `upload_id` + `offset` + `eof`; paced by default (`$downloadBurst`) |
| Register sync | First `/register` sends `s`, `j`, `sync=1` so server adopts client script timing |
| Operator surfaces | New features should land REST → CLI → MCP together (`rmm_tools.py`) |
| AI | OpenAI key from browser; server spawns MCP locally; Exegol optional |
| Docker deploy | **Deferred** — spec in `docs/tech-plan.md` §12; no image/compose in repo yet; interim venv workflow unchanged |
| Security default | Dual secrets; localhost bind; no `--insecure` in production |

---

## Deviations from planned docs

- `docs/tech-plan.md` covers traffic charts; `docs/rclone-exfil.md` documents rclone exfil; `docs/downloads-browser.md` documents the web downloads panel (shipped).
- Agent `file_upload` JSON may include `remote_path` (full agent path) alongside `filename`.
- SOCKS uses custom JSON task protocol over WebSocket, not a generic byte-stream tunnel.
- Embedded `server_rmm.py --cli` remains alongside `rmm_cli.py` (duplicate UX).
- Keylog + persistence exist on client and embedded CLI but are intentionally absent from MCP/web.

---

## NDR detection analysis (2026-07-02)

### Why beaconator stopped detecting after ~4 days

Deep analysis of `beaconator` (decision tree) and `deep_tunnel_t1` (LSTM) packages.

**beaconator filters (in order):**
1. Learning period: **10 days** in production before any alert is emitted.
2. `filtered_non_novel_ja3_pair`: JA3 pair age > 4 days → filtered. The Windows .NET `HttpWebRequest`
   JA3 hash (`3b5074b1b5d032e5620f69f9f700ff0e`) combined with Cloudflare's JA3S
   (`84aaf6d03fc8c5bfb56d1d188735b268`) is seen daily from any Windows machine browsing
   Cloudflare-backed sites, so it is always > 4 days old by the time the learning period ends.
   **Changing the tunnel URL does not help** — the JA3 pair is independent of SNI/hostname.
3. `filtered_idle_beacons`: requires `len(distinct(bytes_sent)) >= 2` OR `len(distinct(bytes_received)) >= 2`.
   The random User-Agent makes `bytes_sent` vary → this filter is currently passed.

**deep_tunnel_t1 activity filter:**
- Requires SCV (squared coefficient of variation) of `bytes_sent` or `bytes_received` > 0.01.
- Current idle beacon: `bytes_received` is constant (~47 B) → SCV ≈ 0 → **blocked**.

**Fix applied:** Random padding field `_p` in idle `/cmd` responses (16–512 random bytes as hex).
- Result: `bytes_received` varies from ~96 to ~1088 bytes per session, SCV ≈ 0.23.
- This unblocks deep_tunnel_t1's activity filter.
- Client ignores the unknown field (only reads `command`, `type`, `socks_active` by name).

**Remaining blocker for beaconator:** the JA3 pair age filter. Requires a different TLS
client fingerprint (e.g. switch to .NET Core / HttpClient which produces a different JA3,
or force TLS 1.3 cipher suites). Changing the Cloudflare tunnel URL alone does not help.

**Double request per beacon cycle:** commit `caf9b1a` (2026-05-21) added `Register-RmmSession`
to every beacon cycle. This creates two TCP connections per cycle (~70ms apart), making
beacon_detector's inter-arrival distribution bimodal `[~70ms, ~5000ms]` instead of clean
`[~5000ms]`. This changes the statistical profile seen by both beaconator and deep_tunnel_t1.

---

## Known issues

| Issue | Detail |
|-------|--------|
| SMB / “NETBIOS timeout” via SOCKS | Relay is TCP-only (`:445`); not 137–139. Windows may show generic timeout on `STATUS_LOGON_FAILURE`. Use target IP. |
| Upload size | Single base64 POST per file; no chunking (downloads are chunked). |
| SOCKS throughput | Pull-loop latency; adequate for interactive use, not bulk transfer optimized. |
| Proxy idle WS | Cloudflare/tunnels may drop long-idle WebSockets; `KeepAliveInterval=20s` on agent. |
| ~~Register + Web UI WS deadlock~~ | **Fixed:** single `_io_lock` on operator WS blocked `broadcast_sessions` during idle `recv_json` → Cloudflare 524 (~100s). Split send/recv locks in `rmm_ws.py`; async/debounced session broadcast; lighter register path. |
| ~~Beacon hang after large results~~ | **Fixed:** `/result` waited for history write + full-body WS push before HTTP 200; slow clients could block origin. Now respond 200 immediately, process async; truncate WS event bodies; 15s WS send timeout; client shows `Beacon poll…` and reports failed result POSTs. Web UI hydrates truncated live bodies from REST so operators still see the complete output. |
| No automated tests | Regressions caught manually only. |
| ~~Web UI queued results~~ | **Fixed:** command blocks + `pendingCommandBlocks` map in `web/app.js`; results match via `ev.command` / tool kind (download, exfil, screenshot, upload); history replay uses same pairing. |
| ~~Download > 2 GB Int32~~ | **Fixed:** `Send-RmmFileDownload` uses `[long]` for file size, offset, and `[Math]::Max([long]0, …)` (PowerShell `[Math]::Max(0, …)` picked the Int32 overload). |
| ~~Server restart vs beacon config~~ | **Fixed:** load `sessions.json` on startup; defer idle `__CONFIG__` until `config_synced`; `config_pending` priority after PATCH; agent `-Reconnect` sends `sync=1`; fast poll after reconnect/config change. |
| ~~CMD nested quotes / cwd~~ | **Fixed:** temp `.cmd` under `%TEMP%` runs the operator line literally; `/d /c` passes only the script path; `WorkingDirectory` for cwd (no `/S` + `%CD%` on `H:\`). |
| Web UI archived commands | History sidebar replays events without the operator-entered command line (`rmm> …`) — shows output only, internal tokens (`__DOWNLOAD__`), or `result » cmd` meta; see [tech plan §11](tech-plan.md#11-web-ui--archived-sessions-missing-entered-commands-bug). |
| README stale | Security section still mentions 10 MB body cap; default is 32 MB + chunking. |
| Web ↔ CLI parity | No SOCKS or keylog in web UI; no interactive cmd/PS mode or traffic/beacon charts yet. |

---

## Lab validation notes (2026-06-02)

- SOCKS end-to-end confirmed: SMB negotiate + NTLM through `socks5://127.0.0.1:1080` on server host.
- Wireshark on target path showed `STATUS_LOGON_FAILURE` (credentials), not relay failure.
- WS stability fixes: task order (`824a086`), Aborted recv (`a965e4d`), socket lock (`9fd848c`).

# Tech Plan — Minimal-RMM

Living technical design for planned features. For completed work and parity gaps, see `docs/progress.md`. For API/protocol details, see `README.md`. **Product constraints** (including EDR/AV compatibility as a commercial gate): `docs/prd.md`.

**Status key:** `planned` → `in progress` → `done` (update this doc when implementation lands).

---

## 1. Beacon & traffic visualization (web UI)

**Status:** planned  
**Surfaces:** Web UI (primary); optional read-only API for CLI/MCP later  
**Goal:** Give operators a graphical view of data flow and beacon rhythm between each agent and the RMM server.

### Problem

Today the web UI shows `beacon_status` (online / stale / offline) and `last_seen` text in the session sidebar, but there is no time-series view of:

- When the agent polled `/cmd` (check-in cadence vs configured sleep + jitter)
- Bytes sent **agent → server** (results, file uploads, SOCKS WS frames)
- Bytes sent **server → agent** (`/cmd` responses, queued commands, SOCKS tasks)

Operators cannot quickly spot jitter drift, missed beacons, or bursty exfil without reading server logs.

### UX (target)

Add a **Traffic & beacon** panel on the selected session (below shell or in a collapsible `<details>` next to “Files & screenshot”):

1. **Beacon timeline** — horizontal axis = last N minutes (default 15, configurable 5–60). Vertical ticks or bars at each `/cmd` poll and each `/result` POST. Color by type: poll (empty queue), poll (command delivered), result posted. Overlay expected interval band from `sleep_seconds` + `jitter_percent`.
2. **Throughput chart** — stacked or dual-line area chart: bytes in vs bytes out per time bucket (e.g. 10 s bins). Separate series optional for SOCKS (`/socks` WS/HTTP) vs main beacon (`/cmd`, `/result`).
3. **Summary strip** — current interval estimate (median gap between polls), last poll ago, totals since session select (or since server start): ↑ sent / ↓ received.

Use a small client-side chart library (e.g. Chart.js or uPlot) loaded only from `/ui/` — no build step required.

```text
┌─ Traffic & beacon ─────────────────────────────────────┐
│  [====●====●=====●========●====]  beacon timeline      │
│  ▲ bytes  ── server→agent   ── agent→server            │
│  Totals: ↑ 2.4 MB   ↓ 890 KB   median poll: 58s        │
└────────────────────────────────────────────────────────┘
```

### Server instrumentation

Extend `Session` in `server_rmm.py` with in-memory metrics (no persistence in v1):

| Field | Updated when |
|-------|----------------|
| `beacon_poll_times` | `deque(maxlen=500)` of ISO timestamps on `GET /cmd` after auth |
| `beacon_result_times` | `POST /result` |
| `bytes_out_beacon` / `bytes_in_beacon` | Request/response body sizes on `/cmd`, `/result` |
| `bytes_out_socks` / `bytes_in_socks` | SOCKS WS frames + HTTP `/socks` bodies |

Hook points (all call `touch_session` today — co-locate counters there or in dedicated `record_beacon_poll` / `record_beacon_result` helpers):

- `handle_cmd` — count outbound command JSON size; record poll timestamp
- `handle_result` — count inbound body; record result timestamp
- `_handle_socks_agent_websocket` / SOCKS HTTP handlers — per-frame byte counts

Optional: emit lightweight WebSocket events (`type: "metrics"`) on each poll so the chart updates live without polling REST.

### Operator API

```http
GET /api/v1/sessions/{id}/metrics?window=900
```

Response (example):

```json
{
  "session_id": "…",
  "sleep_seconds": 60,
  "jitter_percent": 30,
  "beacon_status": "online",
  "last_seen": "2026-06-01T12:00:00",
  "window_seconds": 900,
  "polls": [{"t": "…", "bytes_out": 120, "had_command": false}],
  "results": [{"t": "…", "bytes_in": 4096}],
  "buckets": [
    {"t_start": "…", "bytes_in_beacon": 0, "bytes_out_beacon": 512, "bytes_in_socks": 0, "bytes_out_socks": 0}
  ],
  "totals": {"bytes_in_beacon": 890000, "bytes_out_beacon": 2400000, "bytes_in_socks": 0, "bytes_out_socks": 0}
}
```

Auth: same bearer token as other `/api/v1` routes.

### Web client (`web/app.js`)

- On session select: `GET …/metrics?window=900`, render charts in new DOM section (`web/index.html` + `web/style.css`).
- Subscribe to existing operator WS; on `metrics` events (if implemented), append points without full refetch.
- Fallback: poll metrics every 10 s while panel is open.

### Out of scope (v1)

- Historical metrics across server restarts
- Per-command byte attribution in the chart (events transcript already covers command text)
- Agent-side counters (server-side observation is enough for v1)

### Traffic viz — implementation order

1. Server counters + `GET …/metrics`
2. Static charts in web UI
3. Optional WS push for live updates
4. Document endpoint in `README.md`; add row to parity table in `docs/progress.md`

### Traffic viz — acceptance criteria

- Selecting a session shows beacon poll markers aligned with configured sleep/jitter band
- Uploading a large file via `__DOWNLOAD__` visibly increases agent→server bytes in the chart
- SOCKS traffic appears in socks series when relay is active
- No measurable slowdown on beacon path (O(1) append per request)

---

## 2. Upload to file.io (archived)

**Status:** removed — superseded by [§10 rclone exfil](#10-rclone-exfil-agent-side).

---

## 3. Downloaded files browser (web UI)

**Status:** done  
**Surfaces:** Web UI (primary); REST API for listing; optional CLI `downloads list` later  
**Goal:** Operators can browse, open, and save files pulled from the agent (`__DOWNLOAD__`) directly in the web UI — not only via a one-off link buried in the shell transcript.

### Downloads browser — problem statement

When an operator queues `__DOWNLOAD__`, the agent uploads the file to the server (`RMM_logs/downloads/`) and a `file_upload` event appears in the transcript with a **Download file** link (`GET /api/v1/artifacts/downloads/{name}`). That works for the moment the event scrolls by, but:

- There is **no file list** for the selected session (or globally).
- Older downloads are hard to find without re-reading the full event history.
- Screenshots get inline preview; downloaded files do not (except the transcript link).
- No metadata at a glance: original remote path, size, received time.

Operators should manage exfil artifacts from the browser the same way they use the shell — without SSHing to the RMM host or digging in `RMM_logs/`.

### Downloads browser — UX (target) (or a dedicated tab beside the shell):

```text
┌─ Downloads (from agent) ───────────────────────────────┐
│  Remote path              Size      Received    Actions │
│  C:\Users\x\doc.pdf       1.2 MB    2m ago      [↓][👁] │
│  C:\Temp\dump.zip         48 MB     1h ago      [↓]     │
└────────────────────────────────────────────────────────┘
```

Per row:

- **Remote path** — from `file_upload` result / stored metadata (not only the staged filename).
- **Size** — bytes on disk.
- **Received** — when the server finished reassembly.
- **Actions** — **Download** (save via browser), **Preview** (optional v1: images, `.txt`/`.json` under a size cap inline; else download only).

Refresh when a new `file_upload` event arrives on the operator WebSocket (no manual reload).

Optional v2: global **All downloads** view across sessions (filter by hostname).

### Downloads browser — server `RMM_logs/downloads/{hashPrefix}_{originalName}`; served at `GET /api/v1/artifacts/downloads/{basename}` with bearer token (or `?token=` query param for `<a href>`).

**Index on ingest:** when `_save_file_upload` completes a file, append to `session.download_artifacts` (in-memory + optional JSON sidecar under `RMM_logs/` for survival across restart):

```json
{
  "artifact": "a1b2c3d4_report.pdf",
  "remote_path": "C:\\Users\\x\\report.pdf",
  "size": 1258291,
  "received_at": "2026-06-01T12:05:00",
  "artifact_url": "/api/v1/artifacts/downloads/a1b2c3d4_report.pdf"
}
```

**Backfill (v1):** on server start, scan `downloads/` and match `{hashPrefix}_*` to session IDs via existing hash prefix logic (same as artifact naming today).

### Downloads browser — operator API

```json
{
  "session_id": "…",
  "downloads": [
    {
      "artifact": "a1b2c3d4_report.pdf",
      "remote_path": "C:\\Users\\x\\report.pdf",
      "size": 1258291,
      "received_at": "2026-06-01T12:05:00",
      "artifact_url": "/api/v1/artifacts/downloads/a1b2c3d4_report.pdf"
    }
  ]
}
```

Existing `GET /api/v1/artifacts/downloads/{filename}` remains the download endpoint; list entries reference it.

Optional: `DELETE /api/v1/sessions/{id}/downloads/{artifact}` to remove server-side copy (operator cleanup).

### Downloads browser — web client

- On session select: fetch `GET …/downloads`, render table/list in tools panel.
- Reuse `artifactSrc()` for authenticated download/preview URLs.
- On WS `file_upload` event (or any event with `artifact_url` under `downloads/`), prepend row or refetch list.
- Keep transcript link behavior for backward compatibility; list is the durable entry point.

### CLI & MCP (optional, post-v1)

| Surface | Action |
|---------|--------|
| `rmm_cli.py` | `downloads list [session]` — table of staged files + paths |
| `rmm_tools.py` | `list_session_downloads(session_ref)` — shipped; see `docs/mcp-parity.md` |
| `mcp_rmm_server.py` | `list_session_downloads` tool — shipped |

Not required for first ship if web UI is the primary ask; REST list endpoint is enough for automation.

### Downloads browser — security notes

Artifact URLs continue to require token.

- List endpoint must only return artifacts whose stored `hashPrefix` maps to the requested session (no cross-session leakage).
- Preview only for allowlisted MIME/types and max size (e.g. 1 MB text, images) to avoid loading huge binaries into the DOM.

### Downloads browser — implementation order

1. Track `download_artifacts` on `file_upload` complete + `GET …/downloads`
2. Web UI list + download buttons wired to existing artifact endpoint
3. WS-driven refresh on new `file_upload`
4. Optional preview + delete; CLI/MCP list if needed
5. Document in `README.md` and parity table in `docs/progress.md`

### Downloads browser — acceptance criteria

- After `__DOWNLOAD__` completes, the file appears in the session Downloads panel without a full page reload
- Clicking **Download** saves the correct file via the browser
- List shows remote path and size, not only the hashed server filename
- Sessions cannot see another session’s downloads

---

## 4. Web UI — command completion

**Status:** done  
**Surfaces:** Web UI (primary)  
**Goal:** Make the web shell input feel closer to `rmm_cli.py` / a local terminal: tab completion and command history without leaving the browser.

### Command completion — problem

The web console (`web/app.js`) has a single-line shell input. Operators can **Enter** (exec + wait) or **Ctrl+Enter** (queue), but there is no:

- **Tab completion** for prior commands, common prefixes, or shell mode hints
- **Up/Down history** through commands already run in this session (browser tab)
- **Inline suggestions** while typing (optional v2)

`rmm_cli.py` already ships readline / prompt_toolkit completion for operator meta-commands (`list`, `use`, `download`, session id prefixes, local upload paths). The web UI only sends **remote** commands today, so completion should focus on remote shell ergonomics and session-local history—not duplicating the full CLI REPL.

### Command completion — UX (target)

Enhance `#shell-input` in the console panel:

1. **History (Up/Down)** — navigate commands echoed in the current session transcript (and optionally a small `sessionStorage` ring buffer per session id). Down restores draft line.
2. **Tab completion** — cycle or expand matches from:
   - Session command history (longest common prefix)
   - Static hints: `cmd:`, `PS:`, `powershell:`, `pwsh:` (agent dispatch prefixes)
   - Optional v2: paths/files on the agent via one-shot `dir`/`Get-ChildItem` completion (slow; only on explicit Tab+Shift or second Tab)
3. **Visual affordance** — ghost text or a small dropdown under the input for the top match (no build step; vanilla JS).

```text
┌─ Shell ────────────────────────────────────────────────┐
│  C:\Users\x> dir Doc█                                    │
│              dir Documents/   ← ghost / dropdown hint    │
│  [cmd ▾]  Enter = wait · Ctrl+Enter = queue · Tab hint │
└──────────────────────────────────────────────────────────┘
```

### Command completion — web client (`web/app.js`, `web/style.css`)

- Maintain `state.shellHistory[]` per `selectedId` (seed from operator echo lines when loading events).
- `keydown`: `ArrowUp` / `ArrowDown` walk history; `Tab` / `Shift+Tab` complete from history + static prefixes.
- Do not steal Tab from normal focus navigation when input is empty unless focused in shell.
- Optional: persist last N commands in `sessionStorage` keyed by session id.

### Command completion — server / API

**v1 needs no new endpoints** — history comes from existing `/events` and local echo state.

Optional later:

- Parse `RMM_CWD_SIG:` from result events into `session.work_dir` on the server so the web prompt can show `C:\…>` accurately (today `work_dir` is rarely populated).

### Command completion — out of scope (v1)

- Agent-side filesystem completion without an explicit slow round-trip
- MCP / CLI changes

### Command completion — implementation order

1. Up/Down history from session echoes + event load
2. Tab completion from history + `cmd:` / `PS:` / `powershell:` / `pwsh:` hints
3. Optional ghost-text dropdown
4. Server: update `work_dir` from `RMM_CWD_SIG` in results (supports §5 shell prompt)
5. Document in `README.md` and parity row in `docs/progress.md`

### Command completion — acceptance criteria

- Up/Down recalls prior commands for the selected session in order
- Tab completes the longest shared prefix from history or inserts a known dispatch prefix
- Completion works after page refresh when events are reloaded
- No regression to Enter / Ctrl+Enter behavior

---

## 5. Web UI — interactive shell mode (cmd / PowerShell)

**Status:** planned  
**Surfaces:** Web UI (primary); optional API field on exec/commands later  
**Goal:** Let operators work in a **session-oriented shell** (`cmd.exe`, Windows PowerShell, or `pwsh`) with a realistic prompt and cwd, instead of treating every line as a one-off remote command.

### Interactive shell — problem

The agent already supports multiple dispatch modes (`cmd.exe` default, `PS:`, `powershell:`, `pwsh:`, `cmd:`) and emits `RMM_CWD_SIG:` so cwd can follow across commands. The web UI does not expose this:

- Prompt is always `rmm:deadbeef>` (operator session id), not `C:\Users\x>` or `PS C:\…>`
- No **shell type** selector (cmd vs PowerShell vs pwsh)
- Each command is typed blind; cwd is not shown or updated in the UI
- Operators expect an **interactive shell**; the beacon model is **poll-based** (latency ≥ sleep + jitter), not a live PTY

This feature is **UX-interactive**: one command per beacon round-trip, not character-at-a-time streaming like SSH.

### Interactive shell — UX (target)

Add a **Shell mode** control above or beside `#shell-input`:

| Mode | Agent dispatch | Prompt example |
|------|----------------|----------------|
| **CMD** (default) | bare line → `cmd.exe` | `C:\Users\x>` |
| **PowerShell** | prefix `PS: …` | `PS C:\Users\x>` |
| **Pwsh** | prefix `pwsh: …` | `PS C:\Users\x>` |

Behavior:

1. **Mode selector** — dropdown or segmented control; persisted in `sessionStorage` per session (or global for the tab).
2. **Prompt line** — replace `#shell-prompt` with cwd-aware prompt when `work_dir` is known; fall back to `C:\>` / `PS>` until first result.
3. **Send path** — in PS modes, wrap user input: `PS: <line>` (or `pwsh:`) before `exec` / queue; CMD mode sends the line unchanged.
4. **Cwd tracking** — parse `RMM_CWD_SIG:` from result output (client-side v1) and/or server updates `session.work_dir` on `/result` (preferred v2); refresh prompt after each completed command.
5. **Shell mode banner** — subtle note: *“Beacon shell — one round-trip per command (~sleep interval).”* so expectations match the protocol.
6. **Optional: dedicated panel** — toggle “Interactive shell” layout: larger input, slimmer tools panel, prompt fixed like a terminal (same backend behavior).

```text
┌─ Interactive shell ──────────────────────────────────────┐
│  Mode: [ CMD ▾ ]   cwd: C:\Users\x\Desktop               │
│  C:\Users\x\Desktop> whoami                              │
│  desktop-win\labuser                                     │
│  C:\Users\x\Desktop> _                                   │
└──────────────────────────────────────────────────────────┘
```

### Beacon constraints (important)

- **Not** a reverse TCP shell or WebSocket PTY — see `docs/progress.md` control-plane decision.
- Long-running or full-screen tools (`vim`, `python` REPL, `cmd` nested interactives) will **not** work reliably; document as unsupported.
- For “closer to live,” operators can lower sleep via existing config PATCH (trade-off: more beacon noise).

### Server (`server_rmm.py`) — optional v2

- On `handle_result` for `output`, scan body for `RMM_CWD_SIG:(path)` and set `session.work_dir`.
- Expose `work_dir` in session detail (already in `to_dict()`); web reads it on refresh or WS session update.

No new internal agent commands required for v1 — reuse `Invoke-RmmUserCommand` dispatch rules in `client_rmm.ps1`.

### Interactive shell — web client

- `state.shellMode`: `cmd` | `powershell` | `pwsh`
- `applyShellMode(line)` wraps line before API call
- `updateShellPrompt()` uses mode + `work_dir` from session object or parsed events
- Integrate with §4 history/completion (mode-specific prefixes)

### Interactive shell — out of scope (v1)

- True PTY / streaming stdin (would need SOCKS tunnel or new agent channel)
- Persistent `persist` wrapper that keeps one `cmd.exe` process open across beacons (possible v3; complex on agent)
- cmd.exe vs PowerShell auto-detection from user input without explicit mode

### Interactive shell — implementation order

1. Shell mode selector + wrap lines for `PS:` / `pwsh:` / CMD
2. Client-side `RMM_CWD_SIG` parsing → update displayed cwd/prompt
3. Server `work_dir` sync from results + session refresh
4. §4 completion/history in shell mode
5. README + `docs/progress.md` parity table

### Interactive shell — acceptance criteria

- Selecting PowerShell mode sends `PS: …` to the agent; output appears in the transcript
- After `cd`, prompt updates to the new path when the agent emits `RMM_CWD_SIG`
- CMD mode behavior matches today’s bare-line dispatch
- UI states beacon latency limitation clearly
- Enter still exec-waits; Ctrl+Enter still queues

---

## 6. Web UI — queued command result placement (bug)

**Status:** done  

### Queued results — problem

Today the web console appends every incoming event to the **tail** of `#shell-output` (`appendShellOutput`, `appendShellLine`, artifact blocks). That works for **Enter** (exec + wait) because only one command is in flight and the user waits before typing again.

It breaks for **queued** work:

1. Operator queues `whoami` (Ctrl+Enter) — echo + “(queued — waiting for next beacon)” appear.
2. Operator queues `hostname` before the first result returns.
3. `whoami` output arrives on the next beacon but is rendered **at the tail**, below the second command echo — not under `whoami`.

The same ordering issue affects rapid tool actions (download, MEGA upload, screenshot) when multiple items are queued: results drift to the transcript bottom instead of staying paired with the command that produced them.

`exec` (wait) mode is largely unaffected; the bug is specific to **non-blocking queue** UX and multi-command sessions.

### Expected behavior

```text
rmm> whoami
(queued — waiting for next beacon)
desktop\labuser          ← result inserted here when it arrives

rmm> hostname
(queued — waiting for next beacon)
DESKTOP-LAB              ← under hostname, even if whoami was slower
```

Results must stay visually bound to their command even if:

- Another command was queued while waiting
- Events arrive out of strict FIFO visual order (slow command first in queue, fast second)
- The page reloads and events are replayed from `/events` (history mode should preserve the same pairing)

### Root cause (current code)

- `appendEvent()` always appends new DOM nodes to `#shell-output` end; there is no per-command anchor.
- `state.echoedCommands` only suppresses duplicate **operator** event lines for locally echoed commands; it does not reserve a slot for later **output** events.
- Server events already carry `ev.command` (from agent `rmm_cmd` JSON) for output results — the web UI does not use it for placement.

### UX / DOM model (target)

1. On local echo (`appendShellEcho` / queue path), create a **command block**:
   - echo line (`rmm> …`)
   - optional meta (“queued…”)
   - empty **result container** (placeholder or collapsed “waiting…”)
2. Store a map `pendingResults`: key = normalized command string + monotonic queue id (or server event id of operator action when available).
3. On `output` / `file_upload` / `cloud_upload` / `screenshot` events with `ev.command`, find the oldest **unfilled** block matching that command (FIFO among duplicates) and render into its result container.
4. If no match (e.g. command queued from CLI), append a standalone block at tail (fallback).
5. On session switch / history load, rebuild blocks from event pairs (operator `queued:` + subsequent output with same `command`).

Optional: show a subtle “waiting for beacon…” spinner in the result slot until filled.

### Server / API

**No protocol change required** — output events already include `command` when the agent sends `rmm_cmd` in JSON results. Verify operator `record_operator_action` events include enough detail to correlate queued tool actions (`download`, `exfil`, etc.) with later result types.

Optional later: explicit `command_id` on queue API responses for unambiguous pairing when the same command string is queued twice.

### Web client implementation notes

- Refactor `appendShellEcho` / `runCommand(false)` / tool queue helpers to register a block id.
- Replace tail-only `appendShellOutput` for matched results with `fillCommandResult(blockId, ev)`.
- Keep `scrollShellToBottom()` only when the filled block is near the bottom or user is already pinned to bottom (avoid jarring scroll when an older command completes).
- History replay (`appendEvent(..., { history: true })`) should use the same block builder so refresh matches live WS behavior.

### Queued results — out of scope (v1)

- Reordering events server-side
- CLI transcript changes (CLI already streams in arrival order; issue is web DOM placement)
- Persistent command (`persist`) streaming semantics

### Queued results — implementation order

1. Command block DOM + pending map for locally queued shell commands
2. Match `output` events via `ev.command` into the correct block
3. Extend to tool queues (download, MEGA upload, screenshot, upload)
4. History reload parity
5. Document in `README.md` / `docs/progress.md`; close known-issue row

### Acceptance criteria

- Queue two commands quickly; each result appears under its own echo, not at transcript tail
- Ctrl+Enter queue + later WS/poll delivery preserves pairing after tab refresh (events reload)
- Exec (Enter) behavior unchanged
- Unmatched results (CLI-queued) still appear at tail with command label

---

## 7. Server restart resets agent sleep/jitter (bug)

**Status:** done  

### Restart config — problem

After **server restart**, in-memory sessions are empty. The server writes `RMM_logs/sessions.json` on `save_session()` but **does not reload it on startup**, so a reconnecting agent with the same session ID is registered as a **new** in-memory `Session`:

```python
# Session.__init__ defaults (server_rmm.py)
self.sleep_seconds = 60
self.jitter_percent = 30
```

On every idle `/cmd` poll, `get_command()` returns:

```text
__CONFIG__ {session.sleep_seconds} {session.jitter_percent}
```

So the agent receives **`__CONFIG__ 60 30`** (server defaults) even when:

- The operator had previously set sleep/jitter via PATCH / web UI / CLI (lost with restart), or
- The agent was running script timing (e.g. `$baseSleepSeconds = 5`) adopted earlier in the session.

The client makes this worse on reconnect:

1. **`RmmRegisterConfigSynced`** is set `$true` after the first successful `/register` in a process and is **never cleared** on server outage.
2. Later registers (including `-Reconnect` after errors) **omit** `s=`, `j=`, and `sync=1`, so the restarted server never learns the agent’s script timing.
3. **`Update-Configuration`** applies server `__CONFIG__` when values differ, so the next idle beacon can **overwrite** the agent’s in-memory sleep/jitter with the wrong defaults.

```text
Agent (5s sleep, running) ──► server restart ──► register (no sync)
       ▲                                              │
       │         __CONFIG__ 60 30 on idle /cmd ◄──────┘
       └── client sleep/jitter changed unexpectedly
```

### Operator PATCH not honored (same reconnect window)

After server restart, operators often **PATCH** sleep/jitter (web UI **Apply config**, CLI `set_sleep`, `PATCH /api/v1/sessions/{id}/config`) to correct a reconnecting agent. The server session updates immediately and the UI shows the new values, but the **agent may keep beaconing at the old interval** — sometimes for a full extra cycle at the wrong sleep.

This is a separate symptom from the default `__CONFIG__` push, but shares the same reconnect/config pipeline.

#### What the operator sees

1. Server restarts; agent reconnects (same session ID).
2. Operator sets sleep to e.g. **5 s** via PATCH — server `to_dict()` / sidebar show `sleep 5s`.
3. Agent **`last_seen`** and command latency still reflect the **old** interval (e.g. ~60 s).
4. Optional: no `[config_ack]` in the transcript, or `config_ack` arrives but beacons stay slow.

#### Why PATCH does not reach the agent promptly

| Mechanism | Current behavior |
|-----------|------------------|
| **PATCH is server-only** | `update_session_config()` updates in-memory `session.sleep_seconds` / `jitter_percent` and persists to `sessions.json`. Nothing is queued; delivery is **passive** on the next idle `/cmd`. |
| **`get_command()` priority** | Persistent command → FIFO queue → **`__CONFIG__` last**. Queued or interactive work delays config delivery. |
| **Sleep-before-poll** | Main loop calls `Get-JitteredSleep` **before** `/cmd`. If the agent is mid-cycle at 60 s when the operator PATCHes to 5 s, it will not poll until that sleep finishes — up to ~78 s with 30 % jitter. |
| **`/cmd` before `/register`** | On reconnect, the agent polls `/cmd` **before** `Register-RmmSession`. If the session does not exist yet, `get_command()` returns `("", "none")` — **no `__CONFIG__` that cycle**. Register then creates a session with defaults; the agent sleeps again before the next poll. |
| **Wrong default applied first** | Idle `/cmd` may deliver `__CONFIG__ 60 30` before or after the operator PATCH. The agent applies 60 s locally, then the operator PATCHes the server to 5 s. The agent will not see `__CONFIG__ 5` until it completes the **60 s sleep** it just started — even though the server already shows 5 s. |
| **UI shows server truth, not agent truth** | Web sidebar and API report `session.sleep_seconds` from the server object, not the agent’s in-memory `$baseSleepSeconds`. Operators assume “Apply config” took effect when only the server record changed. |

```text
Operator PATCH sleep=5 ──► server session.sleep_seconds = 5 (UI updates)
                                    │
Agent loop: [sleep 60s] ────────────┼──► still sleeping; cannot poll /cmd yet
            poll /cmd ◄─────────────┘
            __CONFIG__ 5 30 ──► Update-Configuration ──► continue ──► [sleep ~5s]
```

`rmm_run_on_host.py` already warns about this class of delay (`Waiting up to {old_sleep + jitter + 25}s…`), but after restart the **server-reported** sleep may already be the new value while the **agent** is still on the old interval — so wait heuristics based on server fields underestimate the lag.

#### Expected behavior (operator PATCH)

- PATCH updates server **and** the agent applies the new sleep on the **next practical beacon**, without waiting through a full cycle at a stale/wrong interval when possible.
- UI distinguishes **server config** vs **agent-applied config** (e.g. pending until `config_ack`, or show agent-reported values from ack body).
- After reconnect + PATCH, operator can rely on `last_seen` updating at the new interval within one jittered period of the **target** sleep, not the pre-reconnect sleep.

#### Fix directions (operator PATCH)

1. **Eager config delivery** — on PATCH, set a `config_pending` flag or prepend `__CONFIG__` ahead of the queue (or dedicated high-priority slot) so the next `/cmd` returns config even when other work is queued.
2. **Interruptible sleep** — after PATCH, optionally wake the agent sooner (requires agent-side change: shorter poll during “config pending”, or server signal on register response).
3. **Register returns desired config** — `/register` response includes `sleep_seconds` / `jitter_percent` so the agent reconciles **before** the pre-poll sleep on the next loop iteration (or immediately after reconnect `continue`).
4. **Apply config before sleep on change** — agent tracks “config received this poll”; if values changed, skip or shorten the **next** sleep (or move sleep to after `/cmd` handling when only `__CONFIG__` was received).
5. **Reconnect register sends current agent timing** — `-Reconnect` with `sync=1` so server and agent agree before operator has to PATCH manually.

### Restart config — expected behavior

- **Server restart + same session ID:** restore operator-configured sleep/jitter from disk (or treat reconnect as config resync, not a blank session).
- **Agent reconnect:** either push client script values to the server again, or **do not** push `__CONFIG__` until server state matches agent state.
- **Operator PATCH before restart:** persisted values survive restart and are what idle `/cmd` advertises.

### Restart config — root cause (current code)

| Area | Behavior |
|------|----------|
| `save_session()` | Writes `sessions.json` with `sleep_seconds` / `jitter_percent` |
| Server startup | No loader for `sessions.json` into `self.sessions` |
| `register_session()` | New session → `Session()` defaults unless `sync=1` + `s`/`j` on that register |
| Client `Register-RmmSession` | `sync=1` only when `RmmRegisterConfigSynced` is false (once per process) |
| `get_command()` | Always emits `__CONFIG__` from server session fields when queue empty |
| PATCH `/sessions/{id}/config` | Updates server session only; no queue entry; agent learns on next idle `/cmd` |
| Agent main loop | `Get-JitteredSleep` runs **before** `/cmd`; config handled after poll, then `continue` → sleep again |

### Restart config — fix directions (pick one or combine)

1. **Load `sessions.json` on startup** — rehydrate active sessions (or at least a `{session_id → sleep, jitter}` map) before beacons arrive.
2. **Reconnect register** — client sends `s`, `j`, `sync=1` on `Register-RmmSession -Reconnect` (or whenever server was unreachable), so restarted server adopts agent script timing when no persisted operator config exists.
3. **Defer `__CONFIG__`** — after register, skip config push until server has explicit config (restored from disk, operator PATCH, or client sync); avoid broadcasting class defaults on first idle poll.
4. **Persist on PATCH only** — ensure `update_session_config` / PATCH is the source of truth in `sessions.json`; load wins over `Session()` defaults on reconnect.

### Restart config — server / API

No new endpoints required for v1. Optional: include `sleep_seconds` / `jitter_percent` in `/register` response so the client can reconcile without waiting for `/cmd`.

### Restart config — out of scope (v1)

- Cross-server replication of session state
- Changing agent script defaults in the field automatically

### Restart config — implementation order

1. Reproduce **default push:** start server, PATCH sleep/jitter or use 5 s client script → restart server → observe `__CONFIG__ 60 30` on agent
2. Reproduce **PATCH not honored:** restart server with live agent → operator PATCH sleep to 5 s → confirm agent `last_seen` still ~60 s and/or no timely `config_ack`
3. Load persisted session config on server startup (minimal: sleep/jitter by session id)
4. Client reconnect sync (`-Reconnect` sends `sync=1` or clear `RmmRegisterConfigSynced` after prolonged failure)
5. Guard `get_command()` so fresh post-restart sessions do not push defaults before sync
6. Eager or prioritized config delivery after PATCH; agent-side sleep/interrupt strategy
7. Document in `README.md` / `docs/progress.md`; close known-issue row

### Restart config — acceptance criteria

- Restart server with agent still running: agent sleep/jitter unchanged unless operator had persisted a different value before restart
- Operator PATCH to 120 s / 10 % survives server restart and is reflected in UI + agent after reconnect
- Operator PATCH to 5 s on a reconnecting agent that was wrongly at 60 s: agent beacons at ~5 s within **one target interval**, not after an additional full 60 s sleep
- New session (first register with `sync=1`) still adopts client script timing as today
- UI or events distinguish server-configured vs agent-acknowledged sleep when they differ

---

## 8. file.io upload API broken (removed)

**Status:** removed — replaced by [§10 rclone exfil](#10-rclone-exfil-agent-side).

---

## 9. Upload to MEGA (superseded)

**Status:** removed — superseded by [§10 rclone exfil](#10-rclone-exfil-agent-side).

---

## 10. rclone exfil (agent-side)

**Status:** done  
**Surfaces:** Server (`rmm_rclone.py`, `server_rmm.py`), agent (`client_rmm.ps1`), REST → CLI → MCP → Web UI  
**Goal:** Operator queues remote file or folder exfil; agent bootstraps rclone from server; upload runs locally with ephemeral profile config; link or path in events.

### Configuration

| Env | Purpose |
|-----|---------|
| `RMM_RCLONE_BIN` | Path to `rclone.exe` on server (default `tools/rclone/rclone.exe`) |
| `RMM_RCLONE_PROFILES` / `RMM_RCLONE_PROFILES_FILE` | Named remote profiles (MEGA, S3, …) |
| `RMM_RCLONE_DEFAULT_PROFILE` | Default profile when omitted (default `mega-lab`) |
| `RMM_RCLONE_MAX_BYTES` | Size cap (default 100 MB) |

### Flow

`exfil` API → `__EXFIL__` JSON command → agent caches `rclone.exe` → `rclone copyto` (file) or `rclone copy` (folder) + optional `link` → `cloud_upload` event.

See `docs/rclone-exfil.md` for API tables and operator commands.

---

## 11. Web UI — archived sessions missing entered commands (bug)

**Status:** planned (bug)  
**Surfaces:** Web UI (`web/app.js`), optional server (`record_operator_action` in `server_rmm.py`)  
**Goal:** When operators open an **archived session** from the history sidebar, the transcript must show the same **`rmm> …` command lines** they typed or queued during the live session — not only agent output or internal dispatch tokens.

### Archive commands — problem

Live sessions echo commands locally (`appendShellEcho` / command blocks). Archived transcripts replay events from `GET /api/v1/history/{id}/events` with `appendEvent(..., { history: true })`.

Today the archive view often **does not show what the operator entered**:

1. **Tool actions** — server logs operator events with internal payloads (`download: __DOWNLOAD__ C:\path`, `screenshot: __SCREENSHOT__`, `upload: __UPLOAD__ …`) while the web UI echoed user-facing text (`download C:\path`, `screenshot`, `upload file.txt → C:\dest`). History replay uses `operatorCommandFromBody()` and renders the **internal** string, or fails to match tool results to the label the operator saw.
2. **Output-only rows** — when no operator event precedes a result (CLI-only queue, older transcripts, or missing `record_operator_action`), `appendUnmatchedEvent()` shows a small `result » command` meta line instead of a full prompt echo (`rmm> …`).
3. **Exec (Enter) mode** — relies on an `exec: …` operator event in the archive; if absent, only stdout appears with no command line above it.
4. **§6 command blocks** — history replay builds blocks from `operator` events only (`createCommandBlock(opCmd, …)`). There is no fallback to synthesize an echo from `ev.command` on the next `output` / artifact event when the operator row is missing or uses a different string than the live echo.

```text
Live (web UI):
  rmm:abc123> whoami
  desktop\labuser

Archive (same session after kill):
  desktop\labuser                    ← missing "rmm> whoami"

Live (download tool):
  rmm:abc123> download C:\secret.txt
  C:\secret.txt → secret.txt

Archive:
  archive> __DOWNLOAD__ C:\secret.txt   ← wrong label; or output only
```

### Archive commands — expected behavior

- Archived transcript matches live session layout: **prompt + entered command**, optional queued meta, then result — for shell queue, exec, download, exfil, upload, and screenshot.
- Prompt in archive may read `archive>` (read-only) but the **command text** must be what the operator typed.
- Refreshing a live session (`/events?since=0`) and opening the same session from history must show the same command/result pairing.

### Archive commands — root cause (current code)

| Area | Behavior |
|------|----------|
| `record_operator_action()` | Stores `{action}: {command}` where `command` is often the agent dispatch line (`__DOWNLOAD__ …`), not the web/CLI display string |
| `appendEvent(..., { history: true })` | Operator branch calls `createCommandBlock(opCmd)` only when `opCmd` is truthy; no mapping from internal tokens to UI labels |
| `appendUnmatchedEvent()` | Fallback for unmatched results — meta `result » …`, not prompt echo |
| `historyOperatorKind` / meta | Covers download/exfil/upload/screenshot actions but not `exec`, `queued`, or display-label normalization |
| Disk history | `events.jsonl` has no separate `display_command` field |

### Archive commands — fix directions

1. **Display command on record** — extend `record_operator_action(session, command, action, display_command=None)`; web/API pass the user-facing string; persist in events and `events.jsonl`.
2. **History replay fallback** — on `output` / artifact events with `ev.command`, if no pending block matches, create a retroactive echo block from `ev.command` (or mapped display label) before filling results.
3. **Token → label map** — in `web/app.js`, map `__DOWNLOAD__ path` → `download path`, `__SCREENSHOT__` → `screenshot`, etc., when replaying legacy archives without `display_command`.
4. **CLI parity** — ensure embedded CLI and `rmm_cli.py` paths that queue work also record a display-friendly operator line where feasible.

### Archive commands — out of scope (v1)

- Re-writing old `events.jsonl` files on disk
- Changing agent protocol

### Archive commands — implementation order

1. Reproduce: kill session after web queue + tool actions; open history sidebar; confirm missing or wrong command lines
2. Add optional `display_command` to operator events (server + API callers)
3. History replay: prefer `display_command`, then mapped internal token, then `ev.command` fallback block
4. Document in `README.md` / `docs/progress.md`; close known-issue row

### Archive commands — acceptance criteria

- Archived session shows `whoami` (or `download C:\path`, etc.) above its result for web-queued and exec commands
- Tool actions show the same label the operator saw live, not `__DOWNLOAD__` / `__SCREENSHOT__` tokens
- Output-only legacy rows still show a sensible command line via `ev.command` fallback

---

## 12. Docker deployment (RMM server + Exegol MCP)

**Status:** planned — **deferred** (not in current delivery scope; spec only)  
**Surfaces:** `Dockerfile`, `docker-compose.yml`, `docs/docker-deploy.md`, `.env.example`, optional `Makefile` targets  
**Goal:** Package the **operator stack** (RMM server, web UI, AI/MCP, persisted logs and rclone config) for reproducible lab deployment without local Python venvs; wire **Exegol MCP** and **SOCKS** over Docker networking instead of ad hoc `127.0.0.1` / `host.docker.internal` setup.

**Rationale for deferral:** Docker touches networking (bind addresses, SOCKS, agent reachability), secrets, volumes, and cross-platform compose behaviour — too many moving parts for a quick “facilitate deploy” change. **Current workaround:** run RMM manually (host or inside an Exegol container) as today. Pick this up as a dedicated milestone when delivery bandwidth allows.

### Docker deploy — problem

Today deployment is manual:

- Python venv, `pip install -r requirements.txt`, export secrets (`RMM_API_TOKEN`, `RMM_BEACON_SECRET`)
- Typical lab start (e.g. inside Exegol):  
  `python server_rmm.py --rclone-profiles tools/rclone/profiles.json --rclone-max-bytes 0 --token … --beacon-secret … 8081`
- Default HTTP bind is `127.0.0.1` — fine on host, **insufficient inside a container** (agents and port publishing need `--bind 0.0.0.0`)
- Exegol MCP is optional HTTP (`RMM_EXEGOL_MCP_URL`, default `http://127.0.0.1:8000/mcp`) — works when RMM and Exegol MCP share the host, breaks easily when either runs in a container
- `rmm_cli.py` already hints at Docker (`host.docker.internal` for `RMM_SERVER_URL`) but there is **no** `Dockerfile` or compose file in the repo

The **Windows agent** (`client_rmm.ps1`) is deployed separately on managed hosts and is **not** containerized — Docker targets the operator/server side only.

### What Docker simplifies

| Area | Benefit |
|------|---------|
| RMM server + web UI | One `docker compose up`; pinned Python + MCP deps; no venv per machine |
| Secrets | `.env` / Docker secrets for API and beacon tokens |
| Persistence | Volumes for `RMM_logs/` (sessions, history, AI chat, downloads, screenshots, keylogs), rclone profiles + `rclone.exe` |
| Exegol MCP ↔ RMM AI | Shared Docker network; `RMM_EXEGOL_MCP_URL=http://exegol-mcp:8000/mcp` |
| Exegol operator tools | Attach existing Exegol container to compose network → `http://rmm:8081`, `socks5://rmm:1080` (cleaner than `host.docker.internal`) |
| Operator CLI on host | `RMM_SERVER_URL=http://127.0.0.1:8081` with published port |

### What Docker does not simplify

| Area | Why |
|------|-----|
| Windows agent deployment | Agent runs on target hosts; needs reachable server URL + `RMM_BEACON_SECRET` |
| Agent → server connectivity | Server in a container must bind `0.0.0.0` and agents must use **host LAN IP** or tunnel — not container `127.0.0.1` |
| Full Exegol environment | Exegol is a separate pentest stack; only **Exegol MCP** (HTTP) is in scope for compose wiring |
| Lab security | `--insecure` and `--bind 0.0.0.0` remain lab-only; Docker adds no isolation by itself |

### Target architecture

```text
┌─────────────────────────────────────────────────────────┐
│  docker compose — network rmm-lab (operator host)       │
│                                                         │
│  ┌──────────────┐     HTTP MCP      ┌──────────────┐   │
│  │  rmm-server  │◄─────────────────►│  exegol-mcp  │   │
│  │  :8081 /ui/  │  (optional profile)│  :8000/mcp   │   │
│  │  SOCKS :1080 │                   └──────────────┘   │
│  └──────┬───────┘                                       │
│         │ volumes: docker-data/RMM_logs, tools/rclone   │
└─────────┼───────────────────────────────────────────────┘
          │ HTTP beacon (/register, /cmd, /result)
          ▼
   ┌──────────────┐          ┌──────────────────────────┐
   │ Agent Windows│          │ Exegol (existing container)│
   │ (LAN)        │          │ docker network connect …   │
   └──────────────┘          │ API → rmm:8081, SOCKS → rmm:1080 │
                             └──────────────────────────┘
```

### Reference command (container equivalent)

Host command today:

```bash
python server_rmm.py \
  --rclone-profiles tools/rclone/profiles.json \
  --rclone-max-bytes 0 \
  --token test \
  --beacon-secret test \
  8081
```

Target container `CMD` (secrets via `.env`, not hard-coded):

```bash
python server_rmm.py 8081 \
  --bind 0.0.0.0 \
  --rclone-profiles /app/tools/rclone/profiles.json \
  --rclone-max-bytes 0
```

(`RMM_API_TOKEN` / `RMM_BEACON_SECRET` from env or `--token` / `--beacon-secret`.)

### Deployment options

| Option | Layout | When to use |
|--------|--------|-------------|
| **A — RMM only** (ship first) | Single `rmm` service; Exegol MCP on host | Minimal impact; fastest path to “no venv” |
| **B — Compose integrated** | `rmm` + `exegol-mcp` on shared network `rmm-lab` | AI panel + Exegol MCP without host port juggling |
| **C — Exegol operator (today)** | RMM inside or on host; CLI/tools in Exegol | Document `host.docker.internal`; migrate to A/B later |

**Migration from “RMM inside Exegol”:** stop in-container RMM → `docker compose up rmm` → `docker network connect <project>_rmm-lab <exegol-container>` → in Exegol set `RMM_SERVER_URL=http://rmm:8081` and `ALL_PROXY=socks5://rmm:1080` (or proxychains).

### Compose sketch (option B)

```yaml
services:
  rmm:
    build: .
    ports:
      - "${RMM_PORT:-8081}:${RMM_PORT:-8081}"
      - "1080:1080"   # SOCKS — host: socks5://127.0.0.1:1080
    env_file: .env
    environment:
      RMM_EXEGOL_MCP_URL: http://exegol-mcp:8000/mcp
    volumes:
      - ./docker-data/RMM_logs:/app/RMM_logs
      - ./tools/rclone/profiles.json:/app/tools/rclone/profiles.json:ro
      - ./tools/rclone/rclone.exe:/app/tools/rclone/rclone.exe:ro
    command:
      - python
      - server_rmm.py
      - "${RMM_PORT:-8081}"
      - --bind
      - "0.0.0.0"
      - --rclone-profiles
      - /app/tools/rclone/profiles.json
      - --rclone-max-bytes
      - "0"
    networks: [rmm-lab]
    # Linux: extra_hosts: ["host.docker.internal:host-gateway"] if agents use host IP helpers

  exegol-mcp:
    profiles: [exegol]
    # Image / command per Exegol MCP docs — placeholder until pinned
    ports:
      - "8000:8000"
    networks: [rmm-lab]

networks:
  rmm-lab:
```

Optional: mount `./ai-skills` → `/app/ai-skills` for skill edits without rebuild.

### Dockerfile (target)

- Base: `python:3.12-slim` (repo minimum 3.10+)
- Copy application tree; `pip install -r requirements.txt`
- `.dockerignore`: `RMM_logs/`, `.git`, `**/.venv`, `docker-data/`
- Expose `${RMM_PORT}` (default 8081) and document `1080` for SOCKS when mapped
- `WORKDIR /app`; default headless server with `--bind 0.0.0.0`
- `tools/rclone/` layout preserved; `rclone.exe` bind-mounted (served to Windows agents at `/tools/rclone.exe` even from a Linux image)

### Networking notes

| Consumer | URL / binding |
|----------|----------------|
| Web UI / CLI on host | `http://127.0.0.1:8081/ui/` (or `${RMM_PORT}`) |
| Windows agent (LAN) | `http://<host-lan-ip>:8081` + matching `RMM_BEACON_SECRET` |
| CLI inside Exegol (no shared network) | `RMM_SERVER_URL=http://host.docker.internal:8081` |
| Exegol on `rmm-lab` network | `http://rmm:8081`, `socks5://rmm:1080` |
| RMM AI → Exegol MCP (compose) | `RMM_EXEGOL_MCP_URL=http://exegol-mcp:8000/mcp` |
| SOCKS proxy (operator on host) | `socks5://127.0.0.1:1080` with port publish |

Server subprocess AI (`mcp_rmm_server.py` over stdio) runs inside the RMM container — no extra service required.

**SOCKS bind caveat:** `DEFAULT_BIND_HOST` is `127.0.0.1` (`rmm_socks.py`). Port publish works for the **host**; for **Exegol on `rmm-lab`**, validate `socks5://rmm:1080` in phase 2 — may require binding SOCKS on `0.0.0.0` (env or `bind_host` on `POST …/socks`) if inter-container connect fails.

### Secrets & volumes

| Item | Handling |
|------|----------|
| `RMM_API_TOKEN`, `RMM_BEACON_SECRET` | `.env.example` + compose `env_file`; never commit real values |
| `RMM_RCLONE_PROFILES_FILE` | Bind-mount `tools/rclone/profiles.json` (gitignored secrets) |
| `RMM_RCLONE_BIN` | Default `tools/rclone/rclone.exe`; bind-mount binary |
| `RMM_logs/` | Bind-mount `./docker-data/RMM_logs` — includes `sessions.json`, `history/` (events + AI chat), artifacts |
| `RMM_RCLONE_MAX_BYTES` | `0` = unlimited (lab); set in command or env |

### Cross-platform (Windows / Mac / Linux)

| Topic | Windows | Mac | Linux |
|-------|---------|-----|-------|
| Runtime | Docker Desktop (WSL2) | Docker Desktop | Docker Engine + compose plugin |
| Volume paths | Relative `./docker-data/...` | Same | Same; watch uid/gid on `RMM_logs` writes |
| `host.docker.internal` | Native (Desktop) | Native | Add `extra_hosts: host-gateway` on services that need it |
| `rclone.exe` in Linux image | OK — HTTP serve to Windows agents | Same | Same |
| Agent → RMM | Host LAN IP, not `localhost` | Same | Same |

### Docker deploy — out of scope (v1)

- Containerizing the Windows agent
- Production hardening (TLS termination, non-root user, image signing) beyond lab README warnings
- Bundling the full Exegol pentest image — only MCP sidecar or external Exegol docs
- Kubernetes / Helm
- Cloudflared in compose (document port mapping only)

### Implementation order (phased — reduce delivery risk)

**Phase 1 — RMM only (MVP):** `Dockerfile`, `docker-compose.yml` (single service), `.env.example`, `docs/docker-deploy.md`, volumes for `RMM_logs` + rclone; smoke test UI + health.

**Phase 2 — Exegol + SOCKS:** explicit `rmm-lab` network; doc `docker network connect`; validate SOCKS from Exegol; patch SOCKS bind if needed.

**Phase 3 — Exegol MCP:** compose profile `exegol` + `exegol-mcp` service; pin image in docs; test web AI merged tools.

**Phase 4 — Polish:** `Makefile` targets (`docker-build`, `docker-up`), README link, optional CI `docker build`.

### Docker deploy — acceptance criteria

- `docker compose up` starts RMM server; web UI loads at mapped port with API token auth
- `RMM_logs/` and rclone profiles survive container restart via volumes
- Windows agent on LAN registers when `$env:RMM_BASE_URL` points at host IP and beacon secret matches
- SOCKS: `socks5://127.0.0.1:1080` on host relays through mapped container port
- From Exegol on `rmm-lab`: API and SOCKS reach `rmm` by service name
- AI chat works in container (MCP stdio spawn); with Exegol MCP profile, merged tools visible when enabled
- Documented clearly that agents stay outside Docker; Win / Mac / Linux notes included

### Interim deploy (until Docker ships)

No repo change required — operators continue with:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export RMM_API_TOKEN=… RMM_BEACON_SECRET=…
python server_rmm.py --rclone-profiles tools/rclone/profiles.json --rclone-max-bytes 0 8081
```

Or the same command inside an Exegol container (current workflow).

---

## Backlog (referenced elsewhere)

Items tracked in `docs/progress.md` **Up Next** but not spec’d here yet:

- Web UI SOCKS controls + global relay list
- Web shell meta commands — `socks`, `persist`/`stop`, `upload` (browser limitation); see `docs/web-shell-completion.md`
- Chunked upload (symmetry with chunked download)
- CLI subcommands for screenshot / SOCKS
- Automated tests
- `docs/prd.md`

Docker operator deployment is spec’d in [§12](#12-docker-deployment-rmm-server--exegol-mcp).

Operator surface alignment (REST / MCP / web shell) is enforced by **`make check-parity`** — see `docs/mcp-parity.md`.

When picking up a backlog item, add a numbered section here before implementation.

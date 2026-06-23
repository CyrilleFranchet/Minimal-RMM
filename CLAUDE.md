# Project Overview

**Minimal-RMM** is a minimal remote monitoring and management (RMM) proof of concept for **authorized lab use only**. A Python HTTP server coordinates Windows PowerShell agents over a beacon protocol; operators control hosts via REST API, CLI, web UI, or MCP tools.

## Architecture

```text
Operator (CLI / Web / MCP / curl)
        │
        ▼
server_rmm.py  ── REST /api/v1/*  +  beacon /register /cmd /result /socks
        │
        ├── rmm_socks.py   SOCKS5 listeners (per session) on 127.0.0.1:N
        ├── rmm_ws.py      stdlib WebSocket (operator events + agent SOCKS channel)
        └── RMM_logs/      downloads, screenshots, keylogs

client_rmm.ps1 (Windows agent)
        ├── Main loop: register → poll /cmd → execute → POST /result
        └── SOCKS worker (optional): WebSocket or HTTP poll on /socks when socks_active
```

| File | Role |
|------|------|
| `server_rmm.py` | Threaded HTTP server, sessions, command queue, results, operator API |
| `client_rmm.ps1` | Windows beacon + SOCKS relay worker (separate runspace) |
| `rmm_cli.py` | Operator CLI (`RmmApiClient` → `/api/v1`) |
| `rmm_socks.py` | SOCKS5 bridge: listener on server, TCP relayed through agent |
| `rmm_tools.py` | Shared tool implementations for MCP and web AI |
| `mcp_rmm_server.py` | FastMCP server exposing operator actions |
| `web/` | Static operator UI at `/ui/` |

## Security defaults

- `RMM_API_TOKEN` — operator API (`/api/v1/*`)
- `RMM_BEACON_SECRET` — beacon endpoints (`/register`, `/cmd`, `/result`, `/socks`)
- Server binds `127.0.0.1` by default; `--insecure` is lab-only

## SOCKS (important)

- Start: operator `socks [port]` → server sets `socks_active` on `/cmd` → agent opens WS to `GET /socks`
- Use proxy at **`socks5://127.0.0.1:1080` on the machine running the server** (traffic exits the agent host)
- List relays: `GET /api/v1/socks`, CLI `socks list`, MCP `list_socks`
- Agent logs: `[+] SOCKS WebSocket channel active`, `SOCKS outbound TCP host:port`

## Working on this repo

1. Read `README.md` for API tables and protocol details.
2. Read `docs/progress.md` for recent work, decisions, and known issues.
3. Match existing style: small focused diffs, American English comments/docs.
4. Operator features should be exposed consistently: **REST API → `rmm_cli.py` → MCP** (`rmm_tools.py` + `mcp_rmm_server.py`).
5. Do not break the beacon path (`/cmd` latency, register sync, beacon secret).
6. SOCKS changes touch `rmm_socks.py`, `server_rmm.py`, and `client_rmm.ps1` together; test through tunnels if relevant.

## Project Instructions

- Document every new feature in the `docs/` folder.
- Document every new class in the `docs/` folder.
- Document every new function in the `docs/` folder.
- Write all code comments in American English.
- Write all documentation in American English.

## Key Documents

- Progress Log: `docs/progress.md` — living doc of completed work, open items, blockers
- README: `README.md` — setup, API reference, SOCKS troubleshooting
- PRD: `docs/prd.md` — *(not yet created)*
- Tech Plan: `docs/tech-plan.md` — planned features (traffic/beacon charts, MEGA upload, web shell completion, …)

When starting a new task, read the relevant sections of these docs before writing code.

## Context Management

- When compacting, always preserve: the list of modified files, current task context, and any failing test output
- If context is getting long, proactively suggest a `/clear` and offer to update `docs/progress.md` first
- Use subagents for research or investigation tasks to keep the main context clean

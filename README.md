# minimal_rmm

A minimal **remote monitoring and management (RMM)** proof of concept: a Python HTTP server with an interactive CLI, and a Windows PowerShell client that **polls (beacons)** for work, executes commands, and posts results back.

**Use only on systems you own or are explicitly authorized to manage.** Misuse may violate law and policy.

---

## How it works

1. **Server** listens on HTTP (`0.0.0.0` and a configurable port). A **thread** serves requests; the **main thread** runs the interactive RMM console.
2. **Client** registers once (`GET /register`), then loops: sleep (base interval ± jitter) → **`GET /cmd`** → run work → **`POST /result`** when needed.
3. **Commands** you type in the server CLI are **queued per session** (FIFO). Between queued jobs, the server may return a **`__CONFIG__`** pseudo-command so the client updates sleep/jitter.
4. **Output** from the client is printed in the server console; file pulls, screenshots, and keylog dumps are stored under `RMM_logs/`.

There is no interactive TCP shell: latency is at least one beacon interval (plus jitter).

---

## HTTP API

Base URL is whatever you expose (e.g. Cloudflare Tunnel HTTPS URL). All paths use query parameters as below.

| Method | Path | Query | Response / body |
|--------|------|--------|------------------|
| `GET` | `/register` | `id` (session GUID), `h` (hostname), `u` (username) | `200` body `REGISTERED` (new) or `UPDATED` (existing). **`403` `TERMINATED`** if that session id was **killed** (client should exit). |
| `GET` | `/cmd` | `id` | `200` JSON: `{"command": "<string>", "type": "<type>"}` — see [Command channel](#command-channel). |
| `GET` | `/ping` | `id` | `200` `PONG` (simple liveness; optional use). |
| `POST` | `/result` | `id`, `type` (optional, default `output`) | Body: text, raw base64 (screenshot), JSON for file upload, etc. — see [Result types](#result-types). |

`Access-Control-Allow-Origin: *` is set on responses (browser-friendly; not a security boundary).

---

## Command channel

`GET /cmd?id=<session_id>` returns JSON:

| `type` | Meaning |
|--------|---------|
| `execute` | One-shot command string (user command or internal token). |
| `persistent` | Same command is returned on every poll until cleared with `__STOP__`. |
| `config` | Server uses this for `__CONFIG__ …` idle traffic; client applies sleep/jitter. |
| `none` | Empty command (e.g. unknown session id with no kill flag). |

### Server → client: special `command` values

| `command` | Purpose |
|-----------|---------|
| `__CONFIG__ <sleep> <jitter>` | Push beacon interval (seconds) and jitter percent (0–100). |
| `__EXIT__` | Session was **killed**; client exits cleanly. |
| `__STOP__` | Clear **persistent** command (also used when you run `stop` in the CLI). |
| `__DOWNLOAD__ <path>` | Client reads **remote** file, posts it as `type=file_upload`. |
| `__UPLOAD__ <path>` + newline + JSON | JSON has `content` (base64); client writes **remote** file. |
| `__SCREENSHOT__` | Client captures screen; posts PNG as `type=screenshot` (body base64). |
| `__KEYLOG__ start` \| `stop` \| `dump` | Keylogger control on the client. |
| `__INSTALL_PERSIST__` / `__REMOVE_PERSIST__` | Client installs or removes its persistence hooks (see client script). |

Any other string is treated as a **user command** (CMD by default; `PS:`, `powershell:`, `pwsh:`, `cmd:` prefixes are interpreted on the client — see **Remote command prefixes** below).

---

## Result types (`POST /result`)

| `type` | Body |
|--------|------|
| `output` (default) | Prefer JSON `{"rmm_cmd": "…", "rmm_output": "…"}` so the server can label output; plain text also accepted. |
| `file_upload` | JSON `{"filename": "…", "content": "<base64>"}` — server writes under `RMM_logs/downloads/`. |
| `screenshot` | Raw base64-encoded PNG. |
| `keylog` | Text blob — saved under `RMM_logs/keylogs/`. |
| `config_ack` | Acknowledgment string for config changes (logged). |

---

## Server CLI reference

Select a session with `use <id>` (full GUID or **prefix**, minimum **4 characters** if multiple matches would be ambiguous). Most actions require a selected session.

### Aliases

| Alias | Maps to |
|-------|---------|
| `sessions` | `list` |
| `help` | `?` |
| `exit` | `quit` |
| `ls` | `dir` (treated as a normal remote command when a session is selected) |

### Session management

| Command | Description |
|---------|-------------|
| `list` | List active sessions (id prefix, user, host, sleep, jitter, last seen). |
| `use <session_id>` | Select session (prefix allowed). |
| `background` | Clear current session (global view). |
| `info` | Show metadata for the current session. |
| `kill <session_id>` | Marks session id as terminated, removes it from the active map, and on the **next** `/cmd` returns `__EXIT__` so the client exits. That id stays **blocked** for re-registration until the **server process restarts** (in-memory set). |

### Beacon configuration (current session)

| Command | Description |
|---------|-------------|
| `set_sleep <seconds>` | 1–3600; applied on next idle `__CONFIG__`. |
| `set_jitter <percent>` | 0–100; same. |
| `show_config` | Show sleep, jitter, and effective delay range. |

### Command execution (current session)

| Command | Description |
|---------|-------------|
| `<anything not matching a builtin>` | **Whole line** is queued as a one-shot command (e.g. `dir`, `whoami`). |
| `shell <command>` / `exec <command>` | Same as above with explicit verb. |
| `persist <command>` | **Persistent** command: repeated every beacon until `stop`. |
| `stop` | Sends `__STOP__` to clear persistent command. |

**Windows CMD tips (from in-app help):** use double quotes for paths with spaces; for `NET GROUP`, put the group name before `/domain`.

### File and recon (current session)

| Command | Description |
|---------|-------------|
| `download <remote_file>` | Queue `__DOWNLOAD__`; artifact under `RMM_logs/downloads/`. |
| `upload <local_file> <remote_file>` | Reads local file on **server** machine, queues `__UPLOAD__` for **client** path. |
| `screenshot` | Queue `__SCREENSHOT__`. |
| `keylog` / `keylogger` `start` \| `stop` \| `dump` | Queue `__KEYLOG__ …`. |

### Persistence (current session)

| Command | Description |
|---------|-------------|
| `install_persist` | Queue `__INSTALL_PERSIST__` (client copies script to Startup, registry run key — **lab use only**). |
| `remove_persist` | Queue `__REMOVE_PERSIST__`. |

### Other

| Command | Description |
|---------|-------------|
| `help` / `?` | Print full help in the console. |
| `clear` | Clear terminal (`cls` / `clear`). |
| `quit` | Shut down the HTTP server and exit. |

**Ctrl+C** cancels the current input line (does **not** exit the server). **Ctrl+D** (EOF) exits when using prompt_toolkit. Prefer `quit` to shut down cleanly.

The prompt shows **date and time** (local) plus `RMM` or `RMM [<session id prefix>]` when a session is selected.

---

## Client (`client_rmm.ps1`) behavior

- **URL:** set `RMM_BASE_URL` to the server base URL (no trailing slash), or edit `$u` in the script. If the value still contains the placeholder `REPLACE-WITH-YOUR-CLOUDFLARED-URL`, the script exits.
- **Session id:** generated as a new GUID each run (unless you change the script).
- **Registration:** retries with backoff; **403** during register means the session was killed on the server — exit.
- **Remote command prefixes** (handled on the client): `PS:` / `powershell:`, `pwsh:`, `cmd:` — otherwise the command runs via the default CMD-style path used in the script.

---

## Features summary

- Multi-session CLI, tab completion and history (with **prompt_toolkit**; else readline + `input()`).
- Dynamic sleep/jitter; FIFO queue + optional persistent command.
- File up/download, screenshot, keylog helpers, persistence install/remove.
- **`kill`** stops the remote client on the next beacon and blocks that session id until server restart.

---

## Requirements

- **Server:** Python 3.8+; optional `pip install -r requirements.txt` for `prompt_toolkit`.
- **Client:** Windows PowerShell.

---

## Quick start

### Server

```bash
cd minimal_rmm
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server_rmm.py          # default port 8080
python server_rmm.py 9000     # custom port
```

### Expose with a tunnel (example)

```bash
cloudflared tunnel --url http://localhost:8080
```

Use the printed HTTPS URL as the client base URL (no trailing slash).

### Client

```powershell
$env:RMM_BASE_URL = "https://your-tunnel-or-host.example.com"
powershell -ExecutionPolicy Bypass -File .\client_rmm.ps1
```

---

## Files and directories

| Path | Role |
|------|------|
| `server_rmm.py` | HTTP server + interactive console |
| `client_rmm.ps1` | Windows beacon client |
| `requirements.txt` | Optional: `prompt_toolkit` |
| `RMM_logs/` | Created at runtime: `sessions.json`, `downloads/`, `screenshots/`, `keylogs/` |
| `~/.RMM_history` | CLI command history (readline / prompt_toolkit) |

---

## License

Add a `LICENSE` file if you want standard terms published on GitHub; this repo does not ship one by default.

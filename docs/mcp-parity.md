# MCP operator parity

Every **operator REST capability** exposed by `server_rmm.py` under `/api/v1/*` must be reachable through **`mcp_rmm_server.py`** (and shared `rmm_tools.py` / `OPENAI_TOOLS` for the web AI panel).

## Requirement

When adding or changing an operator feature:

1. Implement or extend the **REST API** (`server_rmm.py`).
2. Add a **`RmmApiClient` method** in `rmm_cli.py` when the endpoint is new.
3. Add a **`tool_*` handler** in `rmm_tools.py` (`TOOL_HANDLERS` + `OPENAI_TOOLS`).
4. Expose a **`@mcp.tool()`** in `mcp_rmm_server.py` with the same semantics.
5. Update the parity table in this doc and `docs/progress.md`.
6. Update **`scripts/check_operator_parity.py`** (`REQUIRED_MCP_TOOLS`, `TOOL_CLIENT_METHOD`, `WEB_SHELL_META` when adding tools or shell meta commands).
7. Run **`make check-parity`** (also included in **`make check`**).

CLI interactive commands and web-only UX (sidebar, file picker, WebSocket live updates) do not need duplicate MCP tools if the underlying API is covered.

## Automated check

```bash
make check-parity   # MCP ↔ rmm_tools ↔ RmmApiClient ↔ web shell meta commands
make check          # test + check-parity + lint
```

The script `scripts/check_operator_parity.py` is the **machine-readable registry** of required MCP tool names. When you add a REST operator endpoint or web shell meta command, extend that file and the tables below in the same PR.

### Web shell meta commands

| Shell verb | MCP tool | REST |
|------------|----------|------|
| `download` | `queue_download` | `POST …/download` |
| `exfil` | `queue_exfil` | `POST …/exfil` |
| `screenshot` | `queue_screenshot` | `POST …/screenshot` |

`upload` is intentionally **not** a shell meta command (browser file picker only). See intentional exceptions.

## Implementation map

| REST / capability | MCP tool | Notes |
|-------------------|----------|-------|
| `GET /health` | `health` | |
| `GET /sessions` | `list_sessions` | |
| `GET /sessions/{id}` | `get_session` | `session_ref` = prefix, UUID, or hostname |
| `DELETE /sessions/{id}` | `kill_session` | |
| `POST …/commands` | `queue_command`, `queue_persistent` | |
| `POST …/exec` | `exec_command` | |
| `PATCH …/config` | `patch_config` | |
| `GET …/events` | `get_events` | Live session transcript |
| `POST …/download` | `queue_download` | Same as shell `download` / CLI |
| `POST …/exfil` | `queue_exfil` | Same as shell `exfil` / CLI |
| `GET /rclone/config` | `get_rclone_config` | |
| `POST …/screenshot` | `queue_screenshot` | Same as shell `screenshot` |
| `POST …/upload` | `queue_upload` | Local path on MCP host |
| `GET /socks`, `POST …/socks` | `list_socks`, `start_socks`, `stop_socks` | |
| `GET …/downloads` | `list_session_downloads` | Completed agent→server files |
| `GET /history` | `list_history` | Archived sessions |
| `GET /history/{id}` | `get_history_session` | |
| `GET /history/{id}/events` | `get_history_events` | |
| `DELETE /history/{id}` | `delete_history` | |
| `GET /agent/script` | `get_agent_script` | Full `client_rmm.ps1` text |
| Agent `__KEYLOG__` | `queue_keylog` | Wrapper over `queue_command` |
| Agent `__INSTALL_PERSIST__` / `__REMOVE_PERSIST__` | `install_persistence`, `remove_persistence` | Lab only |
| Agent `__STOP__` | `stop_persistent` | |

## Intentional exceptions

| Surface | Why not MCP |
|---------|-------------|
| `POST /api/v1/ai/chat` | Uses MCP internally; not an operator action |
| WebSocket `/api/v1/ws` | Push transport; use `get_events` / poll |
| `exfil_progress`, `download_progress` | Ephemeral WS events; not in REST history |
| `GET /artifacts/…` binary download | Use `artifact_url` from `list_session_downloads` or `get_events`; fetch with bearer token outside MCP if needed |
| Web UI file-picker upload | Browser-only; MCP uses `queue_upload` with local path |

## Files

| File | Role |
|------|------|
| `rmm_tools.py` | Shared tool implementations + `OPENAI_TOOLS` |
| `mcp_rmm_server.py` | FastMCP `@mcp.tool()` wrappers |
| `rmm_cli.py` | `RmmApiClient` HTTP client |
| `mcp.example.json` | Cursor MCP config template |

## Checklist for reviewers

- [ ] New REST route has matching `RmmApiClient` method
- [ ] `rmm_tools.TOOL_HANDLERS` and `OPENAI_TOOLS` updated
- [ ] `mcp_rmm_server.py` exposes the tool
- [ ] Parity tables updated (`docs/progress.md`, this file)
- [ ] README MCP table updated when user-facing

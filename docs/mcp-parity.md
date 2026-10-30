# MCP operator parity

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the operator and transport
boundaries.

Every **operator REST capability** exposed by `server_rmm.py` under `/api/v1/*` must be reachable through **`mcp_rmm_server.py`** (and shared `rmm_tools.py` / `OPENAI_TOOLS` for the web AI panel).

## Service configuration

`GET /api/v1/settings` maps to `rmm_cli.py settings show` and MCP
`settings(action="show")`. `PATCH /api/v1/settings` maps to CLI
`settings set`, `unset`, and `import`, or MCP
`settings(action="update", settings={...})`. Credentials are write-only;
saved service changes require restart. See [service settings](service-settings.md).

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
| `help` / `?` | — | Browser-local command reference |
| `download` | `queue_download` | `POST …/download` |
| `exfil` | `queue_exfil` | `POST …/exfil` |
| `screenshot` | `queue_screenshot` | `POST …/screenshot` |
| `pe` | `queue_pe_load` | `POST …/pe` |

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
| `GET/POST/DELETE /embeddings/profiles/*`, `/embeddings/results/*`, `POST /embeddings/run` | `embedding_profiles`, `embedding_results`, `run_embeddings` | [Saved profiles/results](document-embeddings.md); workspace operations use the local service |
| `GET /plugins`, `POST …/pe` | `list_plugins`, `queue_pe_load` | Isolated child-process PE plugin execution on the Go agent (`__PE_LOAD__` or plugin-owned `__PLUGIN_COMMAND__`) |
| `GET /plugin-releases`, `POST /plugin-releases/install` | `list_plugin_release`, `install_plugin_release` | [Browse and install compiled GitHub releases](plugin-releases-operator.md) in the active server/workspace |
| `GET /plugin-sources`, `POST/DELETE /plugin-sources/{id}` | `list_plugin_sources`, `save_plugin_source`, `remove_plugin_source` | Persist repository/branch configurations and service-side credential references |
| `GET/POST/PATCH/DELETE /assets/*` | `list_assets`, `add_asset`, `set_asset_enabled`, `remove_asset` | URL-managed agent plugins, AI skills, and rclone profile files |
| `GET/POST /profile` | `profile_status`, `start_profile`, `stop_profile` | Generic RMM-Profile connector and generated beacon URL |
| `POST …/upload` | `queue_upload` | Local path on MCP host |
| `GET /socks`, `POST …/socks` | `list_socks`, `start_socks`, `stop_socks` | |
| `GET /tun`, `POST …/tun` | `list_tun`, `start_tun`, `stop_tun` | TUN device ownership follows storage mode; traffic exits through the Go agent's authenticated TUN transport; `routes[]` supports multiple CIDRs |
| `GET …/downloads` | `list_session_downloads` | Completed agent→server files |
| `GET /artifacts?kind=downloads\|screenshots` | `list_artifacts` | Global artifact inventory with agent identity and session status |
| `GET /history` | `list_history` | Archived sessions |
| `GET /history/{id}` | `get_history_session` | |
| `GET /history/{id}/events` | `get_history_events` | |
| `DELETE /history/{id}` | `delete_history` | |
| `DELETE /history` | `clear_history` | All ended archives |
| `GET /agent/script` | `get_agent_script` | Full `client_rmm.ps1` text |
| `POST /agent/deploy/script` | `deploy_agent_script` | Render a configured `client_rmm.ps1` server-side; returns the artifact path, never the secret |
| `POST /agent/go/build` | `build_go_agent` | Build the Go agent with the active connection embedded (`use_server_config`) |
| Agent `__KEYLOG__` | `queue_keylog` | Wrapper over `queue_command` |
| Agent `__INSTALL_PERSIST__` / `__REMOVE_PERSIST__` | `install_persistence`, `remove_persistence` | Lab only |
| Agent `__STOP__` | `stop_persistent` | |

## Intentional exceptions

| Surface | Why not MCP |
|---------|-------------|
| `POST /api/v1/ai/chat` | Uses MCP internally; not an operator action |
| WebSocket `/api/v1/ws` | Push transport; use `get_events` / poll |
| `exfil_progress`, `download_progress` | Ephemeral WS events; not in REST history |
| `GET /artifacts/…` binary download | Use `artifact_url` from `list_session_downloads`, `list_artifacts`, or `get_events`; fetch with bearer token outside MCP if needed |
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

`queue_upload.local_path` accepts a local filesystem path or the `artifact_ref`
returned by deployment/build tools. Artifact references are fetched through the
configured server's authenticated API, so MCP does not need a shared filesystem.

Upload completion semantics are shared across CLI, MCP, and web AI: successful
queuing is not delivery. Both CLI upload modes accept `artifact_ref` values.
The shared AI upload response includes `delivery_state: queued` and instructions
to wait for a matching new agent result through `get_events`. MCP and web AI
schemas require recording the cursor before queuing. The WebUI upload form
matches results by command and destination, never by the upload marker alone.
Run `node tests/test_upload_matching.cjs` to check result correlation.

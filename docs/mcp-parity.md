# MCP operator parity

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the operator and transport
boundaries.

Every **operator REST capability** exposed by `server_rmm.py` under `/api/v1/*` must be reachable through **`mcp_rmm_server.py`** (and shared `rmm_tools.py` / `OPENAI_TOOLS` for the web AI panel).

## Workspace routing and scope

Operator CLI and external MCP clients default to the workspace URL on port
8787; an explicit URL remains authoritative. Central administration requires
explicit central connection settings. The operator WebUI connects directly to
the workspace and uses its local token.

The shared `RmmApiClient` uses `RejectApiRedirects` for API calls and generated
artifact downloads. Its `redirect_request` hook rejects redirects instead of
forwarding bearer/workspace credentials or mutation bodies to another endpoint.
Configure the final operator URL; a redirect response is surfaced as an HTTP
failure. This policy also applies to MCP tools using the shared client.

Web AI runs centrally, but workspace-owned JSON operations must reach that
same workspace. The internal MCP client marks these requests with
`X-RMM-Operator-Relay`; the server requires authenticated workspace scope and
relays only allowlisted method/path combinations. Direct workspace calls do
not need this marker. The marker is not an authentication credential.

The relay covers local settings/UI state, assets, embeddings, plugin sources
and releases, history, artifact listings, rclone configuration, skills,
session events/download indexes/exfil, and local SOCKS/TUN operations. Binary
artifact bodies and arbitrary destination URLs are outside this bounded JSON
relay. Request and response payloads are limited to 1 MiB.

`local_operator_path` defines the allowlist; `BloodHoundBroker.submit_operator`
queues and waits for the owning workspace; `RMMHandler._relay_local_operator`
enforces scope; `execute_workspace_request` journals execution intent and
completion; the callback in `WorkspaceService.bloodhound_sync_once` invokes
the existing local route handlers. A 20-second response timeout returns HTTP
504 with a workspace request ID, not proof of failure. Retrieve that request
with `bloodhound action=result`; do not resubmit a mutation whose outcome is
unknown. Queue expiration and central restarts retain the uncertainty rules
described in the [workspace workflow](bloodhound-workflow.md).
Successful synchronous replies are removed from central memory after delivery;
workspace journals retain execution results. Oversized replies produce a bounded
error before journaling. Timed-out requests retain their recovery IDs.

Execution replies label central event IDs with `event_id_scope="central"`.
Workspace transcript pagination uses local IDs instead; CLI `record_exec_cursor`
does not advance its local cursor using a central execution ID. This avoids
skipping history after migration or a central restart.

The parity registry checks 55 shared MCP/AI tool names and their API-client
mapping. It does not prove that all 55 have dedicated manual browser controls.
The sixteen BloodHound actions have explicit manual CLI and WebUI controls;
other workflows use their documented UI controls or shared AI tools.

`rmm_deployment.py prepare/run` and `rmm_workspace_migration.py` are offline
deployment/administration utilities. They are intentionally not operator API,
WebUI, or MCP actions: they create process configuration and migrate stopped
service storage before those APIs are running.

## BloodHound CE workflow

`POST /api/v1/bloodhound` maps to `RmmApiClient.bloodhound`, standalone CLI
`bloodhound ACTION --run-id ID --options-json JSON`, shared `tool_bloodhound`,
and MCP/Web AI `bloodhound(action, run_id, options)`. Collection/import return
asynchronous acceptance; `status` inspects the persisted run. Requests remain
at the storage owner; workspace-scoped central calls return a relay request ID
retrieved with `result`, without executing at the central server. Both the
standalone interactive CLI and WebUI shell intercept `/bloodhound` locally,
including invalid syntax; it never falls through to the agent. See the
[workflow contract](bloodhound-workflow.md).

The WebUI shell exposes the same sixteen actions; the main page does not expose
a raw JSON operations form. Use CLI or MCP for advanced actions without selecting
an agent session. **Open BloodHound** waits for readiness and opens the proxy link;
the connection status panel shows runtime readiness and diagnostics. A
**Retry BloodHound startup** button appears next to **Open BloodHound** only
while the runtime is `uncertain` (a previous launch's reply was lost); it
calls `reset` and then re-attempts the open flow. See
[BloodHound CE service startup](bloodhound-workflow.md#bloodhound-ce-service-startup).

`dispatch_interactive_bloodhound` reuses the CLI parser and dispatch function
while keeping parse failures inside the interactive shell. The WebUI
`dispatchShellMetaCommand` branch validates structured options and displays
escaped responses; the form submit handler validates JSON and renders with
`textContent`. `BLOODHOUND_ACTIONS` supplies both browser entry points.
`check_operator_parity.py` checks their action set against the CLI, shared tool
schema, and workspace broker; transport tests exercise actual dispatch.

This establishes parity for the BloodHound workflow. It does not imply that
every other operator capability has a dedicated manual WebUI control; the
shared AI/MCP tool registry and the manual UI are separate surfaces.

The same surfaces expose `library_search` (paginated metadata) and `library_get`
(one original query by GUID). These read-only actions require no run or Exegol
startup and do not add upstream Cypher to the execution allowlist.

## Service configuration endpoints

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

## Session identity and transcript retrieval

Each command targets an explicit session ID. AI and MCP tools resolve an
unambiguous `session_ref` before dispatch; switching the browser's selected
session does not retarget an accepted command. `exec_command` results include
the resolved `session_id` on success, timeout, and HTTP errors. Event IDs are
local to a session, so correlate results by both session ID and event ID.
Ambiguous live or archived references must be replaced with a full session ID.

| Surface | Forward transcript retrieval |
|---------|------------------------------|
| REST | `GET /sessions/{id}/events?since=0&limit=500&forward=1` (or `/history/{id}/events`) |
| MCP / web AI | `get_events(session_ref, since=0, limit=500, forward=True)` or `get_history_events` |
| CLI | `events --session ID --since 0 --limit 500 --forward`; add `--history` for archived sessions |
| WebUI | Live and archived shells automatically traverse retained forward pages |

For explicit pagination, advance `since` to the last returned event ID and
repeat until fewer than `limit` events remain. Omitting `forward` retains the
central API's latest-events behavior. Workspace storage already returns
ascending pages. The CLI background poller uses forward pages to avoid dropping
bursts larger than its page size. Selecting a CLI session intentionally skips
old activity; use `/events` in the interactive shell or the explicit command
above to inspect retained history. Interactive `events [since]` reads the next
50 events in forward order.

Execution returns HTTP 409 when a session already has an execution waiter,
410 when that session is killed while waiting, and 503 when the server stops.
MCP and web AI preserve those statuses in tool errors. Killing a session also
interrupts its associated background AI job; remote shutdown remains
cooperative at the next server contact.

`tests/test_transcript_parity.py` exercises CLI request construction, shared
AI/MCP handlers, result identity across session switches, and ambiguous
archive rejection. The parity checker also verifies `forward` exists in the
MCP signatures, AI schemas, and API client methods.

## Automated check

```bash
make check-parity   # MCP ↔ rmm_tools ↔ RmmApiClient ↔ web shell meta commands
make check          # test + check-parity + lint
```

The script `scripts/check_operator_parity.py` is the **machine-readable registry** of required MCP tool names. When you add a REST operator endpoint or web shell meta command, extend that file and the tables below in the same PR.

### Web shell meta commands

The leading `/` is interactive syntax only. REST and MCP contracts are unchanged.
Both interactive surfaces show suggestions on `/` and filter them while typing.
Unknown slash commands never reach the remote shell.

| Shell verb | MCP tool | REST |
|------------|----------|------|
| `/help` / `/?` | — | Browser-local command reference |
| `/download` | `queue_download` | `POST …/download` |
| `/exfil` | `queue_exfil` | `POST …/exfil` |
| `/screenshot` | `queue_screenshot` | `POST …/screenshot` |
| `/pe` | `queue_pe_load` | `POST …/pe` |
| `/spawn` | `spawn_session` | `POST …/spawn` |
| `/make_token` | `make_token` | `POST …/make_token` |
| `/rev2self` | `rev2self` | `POST …/rev2self` |
| `/lateral` | `lateral_move` | `POST …/lateral` |
| `/uid` | `session_uid` | `POST …/uid` |
| `/embeddings` | `run_embeddings` | `POST /embeddings/run` |
| `/bloodhound` | `bloodhound` | `POST /bloodhound` |

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
| `POST …/download` | `queue_download` | Same as shell `/download` / CLI |
| `POST …/exfil` | `queue_exfil` | Same as shell `exfil` / CLI |
| `GET /rclone/config` | `get_rclone_config` | |
| `POST …/screenshot` | `queue_screenshot` | Same as shell `/screenshot` |
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
| `GET /agent/go/stage/{staging_id}` | `stage_status` | Stage1 launcher status for a staged build (last status line, fetch state, expiry) |
| Agent `__KEYLOG__` | `queue_keylog` | Wrapper over `queue_command` |
| Agent `__INSTALL_PERSIST__` / `__REMOVE_PERSIST__` | `install_persistence`, `remove_persistence` | Lab only |
| Agent `__SPAWN__` (`POST …/spawn`) | `spawn_session` | Detached second Go agent with a fresh session ID; registration and responsiveness verification, Go sessions only |
| Agent `__MAKE_TOKEN__` / `__REV2SELF__` (`POST …/make_token`, `…/rev2self`) | `make_token`, `rev2self` | `runas /netonly` style network identity for spawned commands, Go (Windows) sessions only; operator events record the identity label, never the password |
| Agent `__LATERAL__` (`POST …/lateral`) | `lateral_move` | Start a Go agent on another host (psexec/wmiexec/winrm) with a fresh session ID; registration and responsiveness verification, Go (Windows) sessions only |
| Agent `__UID__` (`POST …/uid`) | `session_uid` | Identity report for make_token / rev2self verification, Go sessions only |
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

### Persistent AI job inspection

| REST | CLI | MCP / web AI |
| --- | --- | --- |
| `GET /ai/jobs?session_id=...` | `ai jobs [--session-id ID]` | `list_ai_jobs(session_id="")` |
| `GET /ai/jobs/{id}` | `ai job ID` | `get_ai_job(job_id)` |

`RmmApiClient.list_ai_jobs` encodes the optional full session ID as a query;
`get_ai_job` encodes the job ID as a path component. `cmd_ai` prints the API
response as JSON. The `tool_list_ai_jobs` and `tool_get_ai_job` wrappers expose
these read-only operations through MCP and web AI with consistent HTTP status
handling. None of these inspection calls starts, retries, or resumes execution.
An interrupted job may already have performed actions; inspect its evidence
before submitting more work.

Session discovery delegates authentication to the API, supporting both bearer
and workspace credentials. `_resolve_session_id` uses only that API's visible
session list: exact IDs take precedence, then exact case-insensitive hostnames,
then ID/hostname prefixes. Multiple matches return `ambiguous_session_ref` with
visible candidates; tools stop without executing until a full ID is supplied.

CLI authentication precedence is: explicit `--token`, then the configured
`RMM_WORKSPACE_ID`/`RMM_WORKSPACE_TOKEN` pair, then `RMM_API_TOKEN`. Direct
central-server workspace access sends only workspace headers, even when an
operator token is inherited. Incomplete or rejected workspace credentials never
fall back to the operator token. For a workspace-local URL protected by a local
bearer token, pass that token explicitly with `--token`.

### Deployment confirmation and enrollment

Configured Windows agents inherit the authenticated workspace on every surface.
Administrator CLI builds can pass `--workspace-id`; MCP/AI deployment tools accept
`workspace_id`; WebUI generators expose an owner field. The API rejects workspace
scope mismatches. Only Windows build targets are supported.

Spawn/lateral responses reserve the expected session ID before dispatch and
report `ok: true` only after that agent registers under the source workspace.
`verified: true` also requires exact fresh command-probe outputs. Skipping checks
reports request acceptance with `pending: true`, `ok: false`, and `registered:
null`; HTTP 200 or a process acknowledgment is not successful deployment.

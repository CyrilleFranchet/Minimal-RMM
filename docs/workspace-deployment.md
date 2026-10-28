# Multi-operator workspace deployment

See the [documentation wiki](README.md) and the [architecture overview](architecture.md)
for the system-wide component map and storage boundaries.

Minimal-RMM supports a central control plane with one local operator workspace
per Exegol container. The central server still receives agent commands and
results so it can route the beacon and enforce workspace ownership. The local
workspace service owns the durable operator view: transcripts, AI chat, files,
screenshots, keylogs, the local SOCKS listener, and the local TUN device.

This mode is enabled with `RMM_STORAGE_MODE=workspace`. The default
`RMM_STORAGE_MODE=server` keeps the existing single-server behavior.

## Architecture

```text
Windows or Go agent
        │ authenticated beacon + SOCKS/TUN plugin channel
        ▼
Dockerized RMM control plane
  ├── session registry and command queues
  ├── workspace claims
  ├── bounded per-workspace event queue
  ├── transport-only SOCKS/TUN bridges
  ├── central runtime plugin cache
  └── RMM-Profile process and beacon listener
        │ outbound HTTPS requests from each workspace
        ▼
Exegol workspace service
  ├── local WebUI at 127.0.0.1:8787
  ├── SQLite transcript and AI chat store
  ├── local artifact directories
  ├── local SOCKS5 listener
  └── local TUN device and routes
        │
        ▼
Exegol tools use native TCP/UDP sockets through the local TUN device.
```

The control plane does not need an inbound connection to an Exegol container.
The workspace makes outbound authenticated HTTP requests for session sync,
event delivery, and pivot task exchange. This works when the workspace is a
Compose service or an existing Exegol container attached to the Compose
network.

The central server can observe command and result data while relaying it. The
storage boundary is different: operator data is persisted in the workspace
container, not in the control-plane volume.

## Storage ownership

In workspace mode the central server keeps only control-plane state:

- active session metadata and workspace ownership in `RMM_logs/sessions.json`;
- command queues and pivot transport state in memory;
- a bounded transient event queue per workspace;
- the persistent central plugin volume at `/app/plugins`, which contains
  externally built agent plugin artifacts;
- the persistent profile volume at `/app/profiles`, which contains trusted
  RMM-Profile bundles and runs the selected external transport;
- server build artifacts such as `rmm-tun` in the configured image or tools
  directory.

Rclone profiles, default profile selection, and the exfiltration size limit are
workspace configuration. They are not loaded from the central control-plane
environment in workspace mode. The central process sees the selected job while
it is queued for the agent transport.

The central server skips durable writes for:

- session event transcripts;
- downloaded files, screenshots, and keylogs;
- AI chat history;
- ended-session history.

URL-installed plugins, AI skills, and rclone files live in the workspace asset
store under `RMM_WORKSPACE_DATA/assets`. Enabled plugin bytes are synchronized
to the central runtime cache so the beacon listener can serve them. The
workspace republishes this cache on startup and removes disabled or deleted
plugin copies. See [operator-managed assets](operator-assets.md).

The workspace service stores its data under `RMM_WORKSPACE_DATA`:

```text
/workspace/data/
├── workspace.db                 # SQLite sessions, events, downloads, AI chat
├── artifacts/
│   ├── downloads/
│   ├── screenshots/
│   └── keylogs/
└── parts/                       # interrupted upload chunks
```

The workspace database and artifact directories are created with owner-only
permissions where the host filesystem supports them. The workspace service
uses a local SQLite database with WAL mode, so the data survives a workspace
service restart.

## Configuration

Central server variables:

| Variable | Description |
| --- | --- |
| `RMM_STORAGE_MODE` | `server` (default) or `workspace` |
| `RMM_WORKSPACES` | JSON object such as `{"alice":"token-a","bob":"token-b"}` |
| `RMM_WORKSPACE_EVENT_LIMIT` | Central transient queue size per workspace; default `10000` |
| `RMM_MAX_WORKSPACE_PAYLOAD_BYTES` | Maximum one transient event payload; defaults to `RMM_MAX_BODY_BYTES` |
| `RMM_API_TOKEN` | Global operator or administrator token |
| `RMM_BEACON_SECRET` | Agent beacon secret |
| `RMM_ADMIN_PORT` | Central administration listener; default `8080` |
| `RMM_BEACON_PORT` | Central beacon listener; default `8081` |
| `RMM_PROFILES_DIR` | Trusted profile bundles; default `/app/profiles` in Compose |
| `RMM_PROFILE_NAME` | Profile selected for automatic startup; default `cloudflared` |
| `RMM_PROFILE_AUTOSTART` | Start the selected installed profile; default `1` |
| `RMM_ASSET_MAX_BYTES` | URL asset download limit; default 64 MiB |
| `RMM_ASSETS_DIR` | Server-mode asset store; default `RMM_logs/assets` |
| `RMM_UI_STATE_DB` | Server-mode WebUI state database; default `RMM_logs/ui-state.db` |

Workspace service variables:

| Variable | Description |
| --- | --- |
| `RMM_C2_URL` | Central URL, for example `http://rmm-control:8080` |
| `RMM_WORKSPACE_ID` | Workspace ID registered on the central server |
| `RMM_WORKSPACE_TOKEN` | Token matching the central workspace registry |
| `RMM_WORKSPACE_DATA` | Local persistence directory; default `~/.minimal-rmm` |
| `RMM_WORKSPACE_BIND` | Local HTTP bind; use `127.0.0.1` by default |
| `RMM_WORKSPACE_PORT` | Local HTTP port; default `8787` |
| `RMM_WORKSPACE_LOCAL_TOKEN` | Token for the local WebUI and CLI when the service is published; optional only when it is bound to a private loopback |
| `RMM_WORKSPACE_TUN_HELPER` | Local `rmm-tun` path; default `tools/rmm-tun` |
| `RMM_WORKSPACE_AUTO_CLAIM` | Claim every unassigned session when set to `1` |
| `RMM_WORKSPACE_POLL_INTERVAL` | Session/event sync interval in seconds; default `1` |
| `RMM_RCLONE_PROFILES_FILE` | Workspace-local JSON profile file |
| `RMM_RCLONE_DEFAULT_PROFILE` | Workspace-local default profile; default `mega-lab` |
| `RMM_RCLONE_MAX_BYTES` | Workspace-local size cap; `0` means unlimited |

Use different workspace tokens for different operators. A workspace token is
accepted only with both `X-RMM-Workspace` and `X-RMM-Workspace-Token` headers.
The central API token remains available for administrative access.

## Session ownership

Agents register normally and start unassigned. A workspace can claim a live
session with:

```bash
curl -s -X POST \
  -H "X-RMM-Workspace: alice" \
  -H "X-RMM-Workspace-Token: $ALICE_WORKSPACE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"SESSION_ID"}' \
  http://127.0.0.1:8080/api/v1/workspace/claims
```

The workspace service normally uses `RMM_WORKSPACE_AUTO_CLAIM=1` for a simple
single-operator lab. In a shared deployment, leave it disabled and claim
sessions explicitly. A claimed session is visible only to its workspace token
or the global operator token. A second workspace receives `409 session_claimed`.

Workspace discovery endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/workspace/candidates` | List unassigned active sessions |
| `POST` | `/api/v1/workspace/claims` | Claim one full ID or unique prefix |
| `GET` | `/api/v1/workspace/sessions` | List sessions owned by the workspace |
| `GET` | `/api/v1/workspace/events?since=N` | Read the bounded event queue |

The workspace service persists the cursor returned by the events endpoint. If
the central queue has already discarded older events, the response sets
`reset_required: true`; the service logs that local history is incomplete and
does not pretend that the missing events were delivered.

## Communication and data flow

Normal commands use the existing REST API, with the workspace headers added by
the local service. Result events are published to the owning workspace queue.
File uploads are sent as transient payloads until the final chunk; the local
service reassembles them under its own `artifacts/downloads` directory.
Screenshots and keylogs follow the same payload path. The server keeps these
payloads in memory only in workspace mode.

The local service proxies operator actions such as command execution, uploads,
downloads, screenshots, plugin builds, and beacon configuration. It serves
sessions, events, history, downloads, and artifacts from its SQLite/filesystem
store. The existing WebUI uses its polling fallback in workspace mode because
the local service intentionally does not proxy the central WebSocket byte
stream.

It also handles `/api/v1/assets/*` locally. This keeps URL-installed plugins,
AI skills, and rclone files in the operator's workspace. `GET /api/v1/plugins`
is proxied to the central server so the operator sees both seeded runtime
artifacts and workspace-synchronized plugin copies.

The local service exposes the same `/api/v1/` paths as the central server, so
the existing `rmm_cli.py` works when `RMM_SERVER_URL` points at the local
workspace service. It stores WebUI configuration in the `ui_state` table in
`RMM_WORKSPACE_DATA/workspace.db`, including AI preferences and the local
configuration cache. Set `RMM_WORKSPACE_LOCAL_TOKEN` whenever the local bind
is exposed beyond loopback; the Compose example requires it.

### AI tool scope

The local AI request is proxied to the central `/api/v1/ai/chat` endpoint with
the workspace headers. The central handler passes those headers to both the
direct `rmm_tools` client and the stdio `mcp_rmm_server.py` child. The child
therefore lists and controls only sessions claimed by the current workspace.
The provider API key is stored in the workspace's owner-controlled SQLite UI
state after validation and is sent only to the selected provider request. Keep
the workspace data directory private.

### Rclone profile scope

The workspace service serves `GET /api/v1/rclone/config` from its own
`RMM_RCLONE_PROFILES_FILE`, `RMM_RCLONE_DEFAULT_PROFILE`, and
`RMM_RCLONE_MAX_BYTES` settings. The central broker exposes only
`GET /api/v1/rclone/capabilities`, which reports whether the Go plugin and the
optional central binary bootstrap artifact are available. In workspace mode,
the central `/api/v1/rclone/config` endpoint refuses to return profiles.

When an operator queues exfiltration, the workspace builds the selected plugin
or binary job from its local profile and sends that job through the
authenticated central request. The central server queues it for the matching
agent but does not load or persist the workspace profile file. Binary mode
still requires `rclone.exe` on the central control plane because the agent
downloads it from the beacon endpoint; Go agents should use the in-process
plugin path when available.

## SOCKS and TUN placement

In workspace mode, the central server creates a transport-only bridge. It does
not bind the operator-facing SOCKS listener or create the TUN device. The
workspace service starts those components locally and exchanges the existing
`connect`, `send`, `close`, `udp_send`, and `udp_close` task protocol with the
central bridge.

### SOCKS

Start SOCKS through the local WebUI or CLI. The returned URL points to the
workspace namespace, for example `socks5://127.0.0.1:1080`. Tools running in
that Exegol container use the local listener. The central agent plugin still
polls the authenticated `/socks` endpoint, so the target agent remains
privilege-free.

### TUN

Start TUN through the local workspace WebUI or CLI. The local service starts
`rmm-tun`, creates the device, assigns the address, and installs routes in the
workspace network namespace. The central server only keeps the agent-side TUN
transport active. The local container therefore needs:

- Linux `/dev/net/tun`;
- `CAP_NET_ADMIN`;
- the `rmm-tun` helper;
- a network namespace shared with the tools that should use the routes.

Example local checks:

```bash
ls -l /dev/net/tun
ip addr show rmm0
ip route get 10.10.20.15
```

The helper carries TCP and UDP socket flows through the existing agent TUN
plugin. It waits for the agent-side TCP `CONNECT` before completing the local
handshake, so TCP port results reflect the target connection and include beacon
latency. Raw packet and ICMP semantics remain outside this implementation, so
`ping` and other target-side raw-packet tools still need a later transport.

Do not add `NET_ADMIN` or `/dev/net/tun` to the central control-plane service.
Only the per-operator workspace that owns the local interface receives them.

## Docker Compose

Copy `.env.example` to `.env`, replace every placeholder, and start the control
plane:

```bash
docker compose up --build -d rmm-control
```

The control plane listens on the published port and has no TUN device or
network administration capability. To run the example workspace sidecar on a
Linux Docker host:

```bash
docker compose --profile workspace up --build -d
```

Create the profile file on the operator host before starting the workspace
service:

```bash
cp tools/rclone/profiles.example.json tools/rclone/profiles.json
$EDITOR tools/rclone/profiles.json
```

Compose mounts `tools/rclone/` into the workspace service at
`/workspace/rclone/`. The central `rmm-control` service does not receive that
mount or the workspace rclone environment variables.

Install the trusted Cloudflared profile bundle in the control-plane
`rmm-profile-store` volume and make the `cloudflared` executable available in
the control-plane image. The profile forwards the public quick-tunnel URL to
the central beacon port; the workspace WebUI uses the returned URL when
generating agents.

The example sidecar is a workspace service image. An existing Exegol
container can run the same `workspace_service.py` directly instead:

```bash
RMM_C2_URL=http://rmm-control:8080 \
RMM_WORKSPACE_ID=alice \
RMM_WORKSPACE_TOKEN="$ALICE_WORKSPACE_TOKEN" \
RMM_WORKSPACE_DATA=/workspace/.minimal-rmm \
RMM_RCLONE_PROFILES_FILE=/workspace/Minimal-RMM/tools/rclone/profiles.json \
RMM_RCLONE_DEFAULT_PROFILE=mega-lab \
RMM_RCLONE_MAX_BYTES=0 \
python3 workspace_service.py
```

Attach that Exegol container to the Compose network if it needs to resolve
`rmm-control` by service name. On Docker Desktop for macOS, a real Linux VM or
Linux Exegol host is required for a usable `/dev/net/tun`; container device
behavior is not equivalent to a native Linux host.

The Dockerfile builds only the checked-in TUN helper into the image. The
control plane mounts `/app/plugins` from the persistent plugin volume; install
the Windows amd64 pivot DLLs and PowerShell scripts from the external plugin
repository before launching a pivot. Outside Docker, run `make tun-build`
explicitly on the workspace host before using TUN. Set `RMM_WORKSPACE_TUN_HELPER`
when the helper is installed at a custom path. Startup never compiles it.
New plugin commands stay in their plugin artifact and
use the agent's generic isolated PE loader; the workspace service does not need
a copy of the plugin code.

## Startup order and failure behavior

1. Start the central control plane with valid API and beacon secrets.
2. Start each workspace service with its unique workspace ID and token.
3. Claim sessions or enable auto-claim for a controlled lab.
4. Open the local WebUI and start SOCKS/TUN from that workspace.
5. Verify the local route or SOCKS URL from inside the same Exegol namespace.

If a workspace service stops, central event delivery remains bounded in memory
and pivot tasks are not durable. Restart the workspace service promptly. If the
queue overflows, the service reports `reset_required` and the operator should
re-run the affected commands or establish a new transcript baseline. A local
workspace restart restores its SQLite data and attempts to recreate active
central pivots.

## Function and class reference

Every new public implementation symbol is covered here so the deployment
contract stays close to the code.

### `rmm_workspace.py`

| Symbol | Role |
| --- | --- |
| `valid_workspace_id` | Validate workspace IDs used in headers and control metadata |
| `WorkspaceRegistry` | Parse workspace token configuration and authenticate scoped requests |
| `WorkspaceRegistry.from_environment` | Load `RMM_WORKSPACES` and singular workspace variables |
| `WorkspaceRegistry.authenticate` | Constant-time workspace token comparison |
| `WorkspaceRegistry.contains` / `ids` | Inspect configured workspace IDs without exposing tokens |
| `WorkspaceEventBroker` | Bounded, thread-safe central event queues |
| `WorkspaceEventBroker.publish` | Add a session event and return its workspace cursor |
| `WorkspaceEventBroker.read` | Read events after a cursor and report queue loss |
| `WorkspaceEventBroker.clear` | Explicitly discard one workspace queue |

### `workspace_service.py`

| Symbol | Role |
| --- | --- |
| `WorkspaceStore` | SQLite and filesystem persistence inside one Exegol workspace |
| `WorkspaceStore.sync_sessions` | Upsert active session metadata and archive missing rows locally |
| `WorkspaceStore.apply_event` | Store a transcript event and materialize its artifact payload |
| `WorkspaceStore.get_events` / `list_history` | Serve live and archived local transcripts |
| `WorkspaceStore.list_downloads` / `list_artifacts` / `artifact_path` | Serve per-session and global artifact inventory plus bytes |
| `WorkspaceStore.get_ai_chat` / `save_ai_chat` / `clear_ai_chat` | Keep AI history in the workspace database |
| `WorkspaceStore.delete_history` / `clear_history` | Remove local ended sessions and artifacts |
| `WorkspaceUpstream` | Add workspace headers and call central REST endpoints |
| `WorkspaceService` | Coordinate HTTP, synchronization, persistence, and local pivots |
| `WorkspaceService._rclone_config` | Combine local profile status with central execution capabilities |
| `WorkspaceService._workspace_exfil_job` | Build a selected plugin or binary job from local settings |
| `WorkspaceStore.asset_store` / `asset_list` | Persist URL-installed plugins, skills, and rclone files |
| `WorkspaceService._sync_workspace_plugins` | Republish the workspace plugin cache to the central server |
| `WorkspaceService.sync_once` | Claim optional sessions, sync metadata, and apply event envelopes |
| `WorkspaceService._submit_pivot_tasks` / `_fetch_pivot_responses` | Exchange local data-plane batches with central |
| `WorkspaceService.start` / `serve_forever` / `stop` | Manage the local service lifecycle |
| `WorkspaceRequestHandler` | Serve static UI and the local `/api/v1/` proxy |

### `workspace_pivot.py`

| Symbol | Role |
| --- | --- |
| `WorkspacePivotManager` | Own local SOCKS/TUN managers in the workspace namespace |
| `start_socks` / `stop_socks` / `list_socks_status` | Manage the local operator SOCKS listener |
| `start_tun` / `stop_tun` / `list_tun_status` | Manage the local helper, interface, and routes |
| `sync_once` | Move local task batches to central and agent responses back locally |
| `stop_all` | Release local listeners, routes, helper processes, and bridge threads |

`WorkspacePivotManager.sync_once` requeues a drained task batch when the central
handoff fails. Its restart reconciliation stops local listeners whose central
relay disappeared and recreates listeners whose central relay is still active.

### Central server additions

`RMMServer.resolve_workspace_session`, `workspace_sessions`,
`workspace_candidates`, `claim_workspace_session`, `workspace_submit_tasks`,
`workspace_drain_responses`, and `workspace_publish_event` enforce ownership
and connect the central registry to the local service. `RMMHandler` accepts
workspace headers, scopes operator routes, and exposes the workspace endpoints.
`RMMServer.queue_workspace_exfil` accepts only a job assembled by the owning
workspace, while `GET /api/v1/rclone/capabilities` reports central plugin and
binary availability without returning workspace profiles.
`Session.workspace_id` is control-plane metadata and is persisted only in the
central session registry.

## Complete implementation symbol reference

The deployment reference above describes the runtime behavior. This section
maps every function added for workspace mode to its implementation role,
including private helpers that enforce the local storage boundary.

### rmm_workspace.py

| Symbol | Role |
| --- | --- |
| valid_workspace_id | Validate workspace IDs used in headers and storage keys |
| _workspace_event_limit | Read and clamp the central transient queue limit |
| WorkspaceRegistry.__init__ | Keep validated workspace token mappings in memory |
| WorkspaceRegistry.from_environment | Parse RMM_WORKSPACES and singular workspace variables |
| WorkspaceRegistry.authenticate | Compare a workspace token in constant time |
| WorkspaceRegistry.contains / ids | Inspect configured IDs without exposing tokens |
| WorkspaceEventBroker.__init__ | Create the cursor and per-workspace bounded queues |
| WorkspaceEventBroker.publish | Copy an event into a workspace queue and allocate a cursor |
| WorkspaceEventBroker.read | Return events after a cursor and report queue loss |
| WorkspaceEventBroker.clear | Discard one workspace queue during an explicit reset |

### workspace_service.py

#### Local path helpers

| Symbol | Role |
| --- | --- |
| _safe_name | Reduce an agent supplied basename to a bounded filesystem name |
| _safe_path | Resolve a child path and reject directory traversal |
| _session_prefix | Derive a stable per-session artifact prefix |

#### WorkspaceStore

| Symbol | Role |
| --- | --- |
| WorkspaceStore.__init__ | Create owner-only directories, open SQLite, and initialize the schema |
| WorkspaceStore._init_schema | Create metadata, sessions, events, downloads, and AI tables |
| WorkspaceStore.close | Close the SQLite connection |
| WorkspaceStore.get_cursor / set_cursor | Read or atomically persist the central event cursor |
| WorkspaceStore.sync_sessions | Upsert current sessions and mark missing rows ended locally |
| WorkspaceStore._decode_session | Decode one SQLite session row defensively |
| WorkspaceStore.list_sessions | Return active sessions in local database order |
| WorkspaceStore.resolve_session | Resolve a full ID or unique prefix, optionally active-only |
| WorkspaceStore.apply_event | Persist a transcript event and materialize its workspace payload |
| WorkspaceStore._apply_file_upload | Reassemble chunked or single file uploads |
| WorkspaceStore._apply_screenshot | Decode and write a PNG screenshot |
| WorkspaceStore._apply_keylog | Write keylog text to the local artifact directory |
| WorkspaceStore._index_download | Record completed downloads in the SQLite index |
| WorkspaceStore.get_events | Read live or archived transcript events with pagination |
| WorkspaceStore.list_history | List ended local sessions |
| WorkspaceStore.history_meta | Return metadata for one live or ended session |
| WorkspaceStore.list_downloads | List indexed downloads for one session |
| WorkspaceStore.artifact_path | Resolve a safe local artifact path for HTTP delivery |
| WorkspaceStore.get_ai_chat / save_ai_chat / clear_ai_chat | Read, normalize, or remove per-session AI messages |
| WorkspaceStore.delete_history | Delete one ended session and its session-prefixed artifacts |
| WorkspaceStore.clear_history | Delete every ended local session |

#### WorkspaceUpstream

| Symbol | Role |
| --- | --- |
| WorkspaceUpstream.__init__ | Store the central URL and workspace credentials |
| WorkspaceUpstream.request | Send authenticated JSON requests and decode JSON or binary responses |

#### WorkspaceService

| Symbol | Role |
| --- | --- |
| WorkspaceService.__init__ | Wire local persistence, upstream transport, and pivot callbacks |
| WorkspaceService.log | Emit local lifecycle messages without sending them to the agent |
| WorkspaceService._submit_pivot_tasks | Forward one local SOCKS/TUN task batch to central |
| WorkspaceService._fetch_pivot_responses | Pull raw agent responses for one local pivot channel |
| WorkspaceService._resolve_local_session_id | Resolve active local session references |
| WorkspaceService.sync_once | Claim optional sessions, sync metadata/events, and reconcile pivots |
| WorkspaceService._sync_pivot_state | Restore local listeners and stop stale local relays after restart |
| WorkspaceService._sync_loop | Run periodic synchronization until shutdown |
| WorkspaceService.start | Bind the local HTTP server and start synchronization |
| WorkspaceService.serve_forever | Run the local HTTP server until interruption |
| WorkspaceService.stop | Shut down HTTP, sync, pivots, and SQLite |
| WorkspaceService._local_authorized | Authenticate the optional local browser/CLI token |
| WorkspaceService._proxy | Forward an API request to the central broker |
| WorkspaceService._local_session | Resolve one active local session for a handler |
| WorkspaceService._rclone_config | Combine local profiles with central plugin and binary capabilities |
| WorkspaceService._workspace_exfil_job | Build the selected exfil job from workspace-local settings |
| WorkspaceService.get_json | Serve local GET routes or return None for proxying |
| WorkspaceService.post_json | Serve local pivot/AI POST routes or return None for proxying |
| WorkspaceService.delete_json | Serve local history/AI DELETE routes or return None for proxying |

#### WorkspaceRequestHandler

| Symbol | Role |
| --- | --- |
| WorkspaceRequestHandler.log_message | Suppress default access logs in the workspace terminal |
| WorkspaceRequestHandler._json / _bytes | Write JSON, binary, or static HTTP responses |
| WorkspaceRequestHandler._request_body | Read and bound a JSON request body |
| WorkspaceRequestHandler._authorized | Check the local workspace token |
| WorkspaceRequestHandler._static | Serve the existing WebUI and workspace configuration script |
| WorkspaceRequestHandler._handle | Dispatch local HTTP requests and proxy the rest upstream |
| WorkspaceRequestHandler.do_GET / do_POST / do_PATCH / do_DELETE | Map HTTP verbs to _handle |
| main | Parse environment/CLI settings and run one workspace service |

### workspace_pivot.py

| Symbol | Role |
| --- | --- |
| WorkspacePivotManager.__init__ | Create local SOCKS/TUN managers and the bridge thread |
| WorkspacePivotManager.log | Forward safe pivot lifecycle messages to the workspace logger |
| WorkspacePivotManager.start_socks / stop_socks | Create or stop the local operator SOCKS listener |
| WorkspacePivotManager.socks_active / list_socks_status | Inspect local SOCKS state |
| WorkspacePivotManager.start_tun / stop_tun | Start or stop the local helper, interface, and routes |
| WorkspacePivotManager.tun_active / tun_config / list_tun_status | Inspect local TUN state |
| WorkspacePivotManager.sync_once | Move local task batches to central and agent responses back locally |
| WorkspacePivotManager._sync_loop | Keep pivot task exchange independent of HTTP handlers |
| WorkspacePivotManager.stop_all | Release listeners, routes, helper processes, and bridge threads |

### Existing module extensions

| Symbol | Role |
| --- | --- |
| configured_storage_mode | Select server or workspace mode with a safe default |
| Session.workspace_id | Store the exclusive workspace claim beside session metadata |
| RMMServer.resolve_workspace_session | Resolve only a session owned by the requested workspace |
| RMMServer.workspace_sessions | List active sessions owned by one workspace |
| RMMServer.workspace_candidates | List unclaimed active sessions |
| RMMServer.claim_workspace_session | Atomically claim a session and persist ownership |
| RMMServer.workspace_submit_tasks | Validate ownership and enqueue local pivot tasks |
| RMMServer.workspace_drain_responses | Validate ownership and drain raw agent responses |
| RMMServer.workspace_publish_event | Publish a result event to the bounded workspace queue |
| RMMServer._publish_workspace_chunk | Publish a non-transcript upload chunk to the workspace |
| RMMServer.sessions_to_json | Optionally filter the session inventory by workspace |
| RMMHandler._workspace_scope | Return the workspace established by request authentication |
| RMMHandler._api_session | Resolve API sessions while enforcing workspace ownership |
| SessionSocksBridge.start_external | Start a task-only bridge without binding a central listener |
| SessionSocksBridge.submit_external_tasks | Queue tasks received from a workspace-local listener |
| SessionSocksBridge.requeue_tasks | Restore a batch when the central handoff fails |
| SessionSocksBridge._submit_external_responses | Retain raw agent responses for a workspace |
| SessionSocksBridge.drain_external_responses | Return and remove raw responses for local delivery |
| SocksManager.start_external | Register an external bridge in the central manager |
| SocksManager.submit_external_tasks | Submit workspace task batches by session |
| SocksManager.requeue_tasks | Requeue failed workspace task batches |
| SocksManager.drain_external_responses | Drain workspace response batches by session |

The existing result, history, AI, SOCKS, and TUN methods branch on
RMMServer.storage_mode. In workspace mode they publish transient events, keep
only central session/control metadata, and create transport-only pivot
bridges. In server mode their legacy filesystem and listener behavior remains
unchanged. The existing AI client functions also accept the scoped headers so
direct and child MCP tool calls stay inside the claimed workspace.

## Validation

Run the standard checks from the repository root:

```bash
make check
python3 -m unittest discover -s tests
make go-test
node --check web/app.js
```

For a real Linux TUN test, run the workspace service inside the namespace that
owns `/dev/net/tun`, start a narrow route, and verify a TCP connection to a
known authorized target before trying larger tools.

## Document embedding configuration

The local workspace owns embedding profiles and explicitly saved result
collections in `workspace.db`. Central mode refuses these persistence routes;
CLI and standalone MCP must point at the local workspace API. The autonomous
DLL requires larger body limits for base64 upload/synchronization; see
[document embeddings](document-embeddings.md) for installation, commands,
authentication preferences, and retention.

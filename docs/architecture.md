# Minimal-RMM architecture

This is the canonical description of the current runtime architecture. It
covers both the single-workspace deployment and the multi-operator deployment
with one workspace service per Exegol container. Procedures and detailed
protocol notes remain in the linked feature pages.

## System map

The [BloodHound CE workflow](bloodhound-workflow.md) executes at the storage
owner: the workspace service inside Exegol.
Every AI run requires RMM and Exegol MCP; the workspace checks readiness and
the central AI worker resolves owner-controlled connection settings through
the authenticated relay. There is no RMM-only AI fallback. The central control
plane still manages agents independently of AI/Exegol availability in both
single-workspace and multi-workspace deployments.
An owner-local runtime checks MCP/container identity and starts or reuses CE
at service startup. Reconnecting browsers read health; CE failures leave the
workspace and agent-management paths available.
It persists run checkpoints and artifacts, invokes an installed CE collector,
and uses a configured CE API. It never moves collection onto the Windows agent
or forwards raw collection files to the central control plane. Bounded tool
responses can become centrally stored AI evidence; they are separate from the
workspace-owned raw collection artifacts.

Minimal-RMM has four boundaries:

1. **Operator surface** — WebUI, CLI, MCP, and the WebUI AI loop send actions
   through the operator API.
2. **Control plane** — `server_rmm.py` authenticates agents and operators,
   owns session state and command queues, and relays agent traffic.
3. **Workspace data plane** — one
   `workspace_service.py` instance per operator owns durable operator data and
   local network pivots.
4. **Agent runtime** — the PowerShell or Go agent runs on the managed host.
   The Go agent loads optional PE plugins in child processes; the target agent
   does not create a TUN device.

```mermaid
flowchart LR
    operator["Operator"] --> surfaces["WebUI / CLI / MCP\n/ WebUI AI"]
    surfaces --> api["Operator API\n/api/v1/*"]

    subgraph control["RMM control plane"]
        server["server_rmm.py\nHTTP server"]
        state["Session registry\ncommand queues\nbeacon authentication"]
        artifacts[("Workspace delivery cache\n&lt;root&gt;/central/plugin-cache/workspaces")]
        server --- state
        server --- artifacts
    end

    api --> local
    server -->|"/register /cmd /result"| agent["PowerShell agent\nor Go agent"]
    server -->|"/tools/plugins/<name>"| agent
    agent --> target["Managed host\nand target network"]

    subgraph workspace["Per-operator workspace"]
        local["workspace_service.py\nlocal API and persistence"]
        db[("SQLite + artifacts\nRMM_WORKSPACE_DATA")]
        pivot["Local SOCKS listener\n+ TUN device/helper"]
        local --- db
        local --- pivot
    end

    surfaces --> local
    local -->|"authenticated sync\nclaims, events, tasks"| server
    tools["Exegol tools"] --> pivot
```

The workspace service owns durable operator data and local pivots in every
deployment. A single-workspace installation uses the same central broker and
workspace services as a multi-workspace installation. Legacy server-owned
operator storage requires explicit migration.

See [workspace deployment](workspace-deployment.md) for container setup and
[deployment guide](deployment.md) for native and Docker startup paths.

## Administration, beacon, and profile boundaries

The control plane binds the operator API and agent beacon to separate listener
roles. An RMM-Profile connects only to the beacon listener and publishes the
URL that generated agents should use:

```mermaid
flowchart LR
    operator["Operator WebUI / CLI / MCP"] -->|"workspace :8787"| workspace["Workspace service"]
    workspace -->|"scoped API :8080"| admin["Administration listener\n/api/v1/* and /ui/"]
    administrator["Central administrator"] --> admin
    admin --> server["server_rmm.py\nshared session registry"]
    profile["RMM-Profile process\nCloudflared, DNS, ngrok, ..."] -->|"local origin :8081"| beacon["Beacon listener\n/register /cmd /result /socks /tun"]
    profile -->|"ready event with public_url"| server
    agent["PowerShell or Go agent"] -->|"public beacon URL"| profile
    beacon --> server
```

The server does not parse Cloudflared, DNS, or ngrok output. It starts the
selected profile and consumes the generic `minimal-rmm-profile-v1` ready and
error events. Profile bundles are trusted server-side executables mounted from
`RMM_PROFILES_DIR`; they are maintained in the separate
[`Minimal-RMM-profiles`](https://github.com/CyrilleFranchet/Minimal-RMM-profiles)
repository. See [RMM-Profiles](rmm-profiles.md).

## Deployment topologies

Use the [wrapper deployment guide](deployment.md) for single and distributed
same-host startup. Cross-host overlay deployment is future work. All mutable
state follows the owner-root convention in the [configuration reference](service-settings.md).

### Single workspace

```mermaid
flowchart LR
    tools["Operator WebUI / CLI / MCP"] --> workspace["workspace_service.py inside Exegol"]
    workspace --> data[("Workspace SQLite and artifacts")]
    workspace --> pivots["Local SOCKS and TUN"]
    workspace --> server["server_rmm.py control plane"]
    server <--> agent["Managed-host agent"]
```

The single preset supervises the two services as separate processes. It provides
one registered workspace; adding workspaces changes configuration, not the
execution architecture. Network administration capability belongs to the
workspace that creates the TUN device.

### Central control plane plus workspaces

```mermaid
flowchart LR
    subgraph central["Docker control plane"]
        c2["rmm-control\nserver_rmm.py"]
        plugins[("Scoped plugin cache\ncentral state volume")]
        c2 --- plugins
    end

    subgraph alice["Alice's Exegol container"]
        ws1["workspace_service.py"]
        data1[("workspace.db\nartifacts")]
        tun1["rmm-tun + local TUN\nSOCKS listener"]
        ws1 --- data1
        ws1 --- tun1
    end

    subgraph bob["Bob's Exegol container"]
        ws2["workspace_service.py"]
        data2[("workspace.db\nartifacts")]
        tun2["rmm-tun + local TUN\nSOCKS listener"]
        ws2 --- data2
        ws2 --- tun2
    end

    ws1 <-->|"workspace token\nclaims, events, pivot tasks"| c2
    ws2 <-->|"workspace token\nclaims, events, pivot tasks"| c2
    c2 <-->|"agent beacon and\nplugin transport"| agent["Managed-host agent"]
    tun1 --> tools1["Alice's tools"]
    tun2 --> tools2["Bob's tools"]
    agent --> target["Target network"]
```

Only distributed central uses the dedicated RMM Docker image, generated by
the deployment wrapper. It receives neither `/dev/net/tun` nor `CAP_NET_ADMIN`.
A workspace runs inside its existing Exegol environment; when it owns a TUN
device, that Exegol environment receives those permissions and must share its network namespace with
the tools that use the routes. A workspace can claim a session only through
its workspace credentials; a second workspace cannot use a claimed session.

## Control and data channels

| Channel | Direction | Purpose | Owner |
| --- | --- | --- | --- |
| `/register`, `/cmd`, `/result`, `/ping` | Agent ↔ control plane | Beacon registration, command polling, results, and health | `server_rmm.py` |
| `/tools/plugins/<name>` | Agent → control plane | Authenticated plugin artifact download | Runtime plugin store on the control plane |
| `/api/v1/*` | Operator ↔ server or workspace | Session control, profiles, assets, plugins, pivots, artifacts, AI, and history | Central server or local workspace facade |
| `/socks` | Agent ↔ pivot owner | Public SOCKS transport for TCP/UDP tasks | `rmm_socks.py` or workspace pivot bridge |
| `/tun` | Agent ↔ pivot owner | Dedicated TUN transport for TCP/UDP tasks | `rmm_tun.py` or workspace pivot bridge |
| Workspace endpoints | Workspace ↔ control plane | Claims, bounded events, and pivot task handoff | `workspace_service.py` + `server_rmm.py` |

The WebSocket endpoint remains available for compatible SOCKS clients, but the
current Go SOCKS and TUN plugins use authenticated HTTP polling. The normal
beacon continues polling while a persistent pivot child runs.

## Command and plugin flow

```mermaid
sequenceDiagram
    participant O as Operator surface
    participant S as server_rmm.py
    participant A as Go agent beacon
    participant P as Plugin child
    participant N as Target network

    O->>S: Queue PE load or plugin command
    S->>A: /cmd returns __PE_LOAD__ or __PLUGIN_COMMAND__
    A->>S: GET /tools/plugins/<name>
    S-->>A: DLL bytes over beacon-authenticated request
    A->>P: Start child and frame DLL + input over stdin
    P->>P: Map DLL and call export or Run envelope
    P->>N: Plugin-specific operation
    P-->>A: Output over the child pipe
    A->>S: POST /result
    S-->>O: Event through REST, WebSocket, CLI, MCP, or WebUI
```

The main Go agent contains the generic fetch, child-process, and PE-loader
path. Plugin-specific command names, input validation, and output formats live
in the plugin artifact. The command namespace form sends a JSON envelope to the
plugin's `Run` export. See [dynamic plugin architecture](plugin-architecture.md)
and [Go agent plugins](go-agent-plugins.md).

The loader starts one child per one-shot command. The child exits after the
operation, so plugin state does not become state in the main beacon. SOCKS and
TUN are persistent exceptions: each has its own Go plugin package, DLL, child
process, lifecycle flag, and HTTP endpoint.

## Pivot data flow

```mermaid
flowchart LR
    app["Exegol tool\nnormal TCP/UDP socket"] --> device["TUN device\nowned by server or workspace"]
    device --> helper["rmm-tun\nuser-space IP/TCP/UDP stack"]
    helper --> bridge["Private per-session\nrelay bridge"]
    bridge -->|"authenticated /tun tasks"| tunchild["tun.dll\nGo child on agent"]
    tunchild -->|"ordinary TCP/UDP sockets"| remote["Target network"]

    proxy["SOCKS client"] --> listener["SOCKS listener\nowned by server or workspace"]
    listener --> socksbridge["Per-session SOCKS bridge"]
    socksbridge -->|"authenticated /socks tasks"| sockchild["socks.dll or socks.ps1\nagent plugin"]
    sockchild --> remote
```

The TUN device is a local operator-side interface. It is never created on the
managed host. The target-side plugin opens ordinary outbound TCP and UDP
sockets, which keeps the agent privilege-free. The helper completes the
agent-side TCP `CONNECT` before acknowledging the local SYN, so TCP port status
comes from the target connection result. The transport does not carry target
raw packets or ICMP.

## Storage ownership

| Data | Canonical owner/location |
| --- | --- |
| Active sessions and ownership | `central/sessions.json` and central memory |
| Command queues and transient agent transport | Central memory |
| Authoritative plugins and plugin sources | `workspaces/<id>/assets/` |
| Disposable plugin delivery copies | `central/plugin-cache/workspaces/` |
| RMM-Profile bundles | `central/profiles/` |
| Transcripts, AI chat, operator UI, embeddings | `workspaces/<id>/workspace.db` |
| Downloads, screenshots, keylogs | `workspaces/<id>/artifacts/` |
| Reconnectable AI jobs and evidence | `central/ai-jobs.sqlite3` and `central/ai-context/`, scoped to owner/session |
| Central administration UI | `central/ui-state.db` |
| Rclone profiles | Registered enabled workspace `assets/rclone/` files; no loose-file/example fallback |
| SOCKS listeners and TUN devices/routes | Workspace/Exegol namespace |

These locations are relative to the deployment root selected by the wrapper.
Server and workspace roots need not share a filesystem. The legacy server-owned
operator storage mode is rejected; see [migration](workspace-migration.md).

## Privilege boundaries

| Process | Runs where | Required privileges |
| --- | --- | --- |
| `server_rmm.py` | Control-plane host or `rmm-control` | HTTP bind and filesystem access; no TUN capability in Compose |
| `workspace_service.py` | Operator's Exegol container | Local storage; `CAP_NET_ADMIN` and `/dev/net/tun` when TUN is used |
| `rmm-tun` | Same namespace as the TUN device | Permission to open/configure the device and routes |
| PowerShell agent | Managed Windows host | Ordinary agent account for normal beacon and pivots |
| Go agent | Managed host | Ordinary agent account; no target-side TUN device |
| Go plugin child | Managed host | Same account as the Go agent; isolated lifetime |

The control plane never needs to join the operator's network namespace. The
workspace service makes outbound authenticated requests to the control plane,
which avoids an inbound connection from the central server into Exegol.

## What requires a rebuild or restart?

| Change | Server image rebuild | Server restart | Agent rebuild | Operational action |
| --- | --- | --- | --- | --- |
| Add a one-shot plugin DLL | No | No | No | Install the artifact in the runtime plugin store and queue it through `pe` |
| Add a plugin-owned command | No | No | No | Publish the updated artifact; invoke it with `--command` or an export |
| Replace a one-shot plugin | No | No | No | Atomically replace the artifact; new commands use the new bytes |
| Replace a running SOCKS/TUN artifact | No for the volume | No | No | Stop and restart that pivot so the child loads the replacement |
| Change `server-tun` or workspace helper code | Yes for Docker | Restart the process that owns the helper | No | Rebuild `rmm-tun`, update the image or local helper, then restart the owner |
| Add a new persistent lifecycle type | Usually | Yes after deployment | Usually | Extend the beacon lifecycle and operator surfaces together |
| Add a WebUI-only panel | No | Usually for server route changes | No | Update the static WebUI and any required API endpoint |

The generic plugin path is the extension point for new one-shot agent
capabilities. Persistent plugins remain deliberately explicit because they
need lifecycle state, cleanup, transport endpoints, and operator controls.

The external plugin repository builds and releases agent plugins. Minimal-RMM
builds only server-side helpers and serves the plugin artifacts installed in
the runtime store. See the [external plugin repository guide](plugin-repository.md).

## Repository map

| Area | Responsibility | Documentation |
| --- | --- | --- |
| `server_rmm.py` | HTTP control plane, operator API, plugin delivery, session state | [Root README](../README.md), [operator parity](mcp-parity.md) |
| `rmm_profile.py` | Generic profile discovery, process lifecycle, ready URL, and shutdown | [RMM-Profiles](rmm-profiles.md) |
| `rmm_asset_store.py` | Atomic URL asset downloads and enablement state | [Operator-managed assets](operator-assets.md) |
| `rmm_socks.py` | SOCKS5 listener and task bridge | [WebUI pivots](web-socks.md) |
| `rmm_tun.py` | TUN lifecycle, route setup, and private relay | [TUN pivot](tun-pivot.md) |
| `server-tun/` | Go helper embedding the user-space IP stack | [TUN pivot](tun-pivot.md) |
| `workspace_service.py` | Per-operator API facade, persistence, upstream sync | [Workspace deployment](workspace-deployment.md) |
| `workspace_pivot.py` | Workspace-local SOCKS/TUN ownership and transport handoff | [Workspace deployment](workspace-deployment.md) |
| `agent-go/` | Beacon, command execution, generic plugin child host | [Go agent](go-agent.md), [Go agent plugins](go-agent-plugins.md) |
| `Minimal-RMM-plugins` | Go and PowerShell plugin sources, tests, Windows builds, manifests, and release bundles | [External plugin repository](plugin-repository.md) |
| `agent-powershell/` | PowerShell beacon and script plugin host | [PowerShell generator](web-agent-generator.md), [pivot plugins](pivot-plugins.md) |
| `web/`, `rmm_cli.py`, `mcp_rmm_server.py` | Operator surfaces | [Operator parity](mcp-parity.md) |
| `Dockerfile`, `rmm_deployment.py` | Wrapper-managed distributed central image | [Workspace deployment](workspace-deployment.md) |

## Known limits

- The current TUN transport supports TCP and UDP socket flows. Raw packets and
  ICMP are outside the transport.
- The shipped TUN Go plugin targets Windows amd64 Go agents. Other agent types
  do not advertise that capability yet.
- Generic one-shot plugins are hot loaded from the runtime store. Arbitrary
  persistent plugin lifecycles are not self-describing yet.
- The plugin ABI is native PE/DLL. Managed .NET assemblies are outside the
  current loader scope.
- The central server remains able to observe transient operator and agent data;
  workspace mode changes durable storage and local pivot ownership.

## Saved embedding configuration and results

Embedding profiles and explicitly selected output collections belong to the
workspace storage owner:
`RMM_WORKSPACE_DATA/workspace.db` in workspace mode. The central broker refuses
workspace embedding persistence; only the resolved plugin execution request
uses the existing central command transport. See the
[embedding workflow](document-embeddings.md) for CLI, WebUI, MCP, and boundaries.

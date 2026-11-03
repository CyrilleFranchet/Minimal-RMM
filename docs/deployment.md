# Deployment guide

Use this page to choose and start a deployment. The other pages are references,
not additional steps to run on every machine. Work on branch **v2**;
Minimal-RMM `main` remains v1. The separate plugin repository uses `main`.

## Configuration before startup

Use [persistent service settings](service-settings.md) for new installations.
Initialize authentication locally with `rmm_settings.py`, then manage operational
settings through Config, CLI, or MCP. Restart after saving. The environment-file
recipes below remain a compatible migration path; nonempty environment values
override saved settings until removed.

## Choose one topology

| Setup | Use it for | Processes |
| --- | --- | --- |
| **Ubuntu central server + one Exegol workspace per operator** | Your multi-operator setup | `server_rmm.py` on Ubuntu; `workspace_service.py` inside each Exegol environment |
| Compose central server + optional workspace sidecar | Container deployment on a Linux Docker host | Same processes, packaged in the shared image |
| Single-workspace preset | One operator or a shared lab | The same central server plus one workspace service |

For the recommended first setup, follow steps 1–5 below. Do not also start the
Compose workspace sidecar for the same operator. Each operator needs a unique
workspace ID/token and its own persistent directory. Users sharing a workspace
also share its plugins and settings.

```mermaid
flowchart LR
    browser["Browser / CLI / MCP"] -->|"Workspace API :8787"| workspace["Exegol workspace\nsettings, plugins, history, local pivots"]
    workspace -->|"Central admin API :8080"| central["Ubuntu central server\nclaims, command queue, transport, plugin cache"]
    agent["Managed agent"] -->|"Beacon :8081 or transport URL"| central
```

| Address or secret | Used by | Purpose |
| --- | --- | --- |
| Central administration URL, usually `:8080` | Workspace service and central administrator | Broker API; not the normal operator UI in workspace mode |
| Beacon URL, direct `:8081` or profile URL | Managed agents | Registration, polling, results, and plugin downloads |
| Workspace URL, usually `:8787` | Browser, CLI, standalone MCP | Operator's own API and UI |
| `RMM_API_TOKEN` on the central process | Central administrator | Global access |
| `RMM_BEACON_SECRET` | Central server and agents | Beacon authentication |
| `RMM_WORKSPACE_TOKEN` | Workspace service | Must match its ID in central `RMM_WORKSPACES` |
| `RMM_WORKSPACE_LOCAL_TOKEN` | Browser, CLI, standalone MCP | Authenticate to that workspace |

## 1. Prepare both checkouts

Use the current `v2` checkout on Ubuntu and inside Exegol. On each machine:

```bash
git switch v2
git pull --ff-only
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

These examples use a POSIX shell and run from the repository root. They assume
Python 3.11+ and the OS's venv support are installed. No Go compiler is needed
for ordinary Python startup. See the optional-feature table below before using
TUN or generating a Go agent.

If Git reports divergent history, stop here and inspect it. Do not resolve a
rewritten history with an arbitrary merge. Keep a backup branch and reconcile
local work before updating. Do not delete `.env` or runtime directories.

## 2. Start the central server on Ubuntu

Copy the native configuration template to a private location:

```bash
mkdir -p "$HOME/.config/minimal-rmm"
cp deploy/central.env.example "$HOME/.config/minimal-rmm/central.env"
chmod 600 "$HOME/.config/minimal-rmm/central.env"
```

Edit that file before starting. Replace all secrets; put one entry per operator
in `RMM_WORKSPACES`. The template's address `10.254.50.203` is an example private
Ubuntu address: replace it with an address reachable by your workspaces and
agents. Its direct beacon URL must be reachable by the agents too.

```bash
set -a
. "$HOME/.config/minimal-rmm/central.env"
set +a
python3 server_rmm.py --bind 10.254.50.203 --beacon-bind 10.254.50.203
```

Replace both bind addresses with the actual interface address on Ubuntu.
This initial recipe assumes a trusted private network/VPN. Restrict the admin
port to operators/workspaces; use HTTPS termination for untrusted networks.
Python serves HTTP directly. Do not put the administration API behind the
agent-only transport profile.

Workspace storage is the default for native and Docker deployments. Explicit
`RMM_STORAGE_MODE=server` is rejected to prevent silently stranding legacy data.
There is no `--storage-mode` argument.

Neither Python entry point loads `.env` automatically. The two `deploy/*.env.example`
files are shell-compatible templates. The root `.env.example` is for Compose;
its JSON syntax must not be assumed safe to load with shell `source`.

For systemd or another process manager, keep `WorkingDirectory` set to the
repository root, use the venv Python executable, and load the role-specific
environment file. Restart the central service after changing its workspace
registry or environment. The removed `run_server.py` wrapper is not needed.

## 3. Start one workspace inside Exegol

Inside the operator's Exegol container, using its own checkout:

```bash
mkdir -p "$HOME/.config/minimal-rmm"
cp deploy/workspace.env.example "$HOME/.config/minimal-rmm/workspace.env"
chmod 600 "$HOME/.config/minimal-rmm/workspace.env"
```

Edit the file: set `RMM_C2_URL` to Ubuntu's **admin** URL, match the workspace ID
and token to the central registry, and choose a separate local token. Set
`RMM_WORKSPACE_DATA` to a directory on an actual persistent mount, not merely
a writable container layer. Set a read-only GitHub plugin token if needed.

```bash
set -a
. "$HOME/.config/minimal-rmm/workspace.env"
set +a
python3 workspace_service.py
```

The workspace initiates its connections to Ubuntu; Ubuntu does not initiate a
connection to the workspace. For a central server on another machine, use its
reachable IP/DNS name. `rmm-control` is a Compose service name available only to
containers on that Docker network.

Open `http://127.0.0.1:8787/ui/` **from the workspace's network namespace** and
enter its local token. A browser on your host can use that address only when
it shares the namespace or a port has been forwarded. For a bridge-networked
Exegol container, configure localhost-only host port publication/forwarding to
container port 8787 and set `RMM_WORKSPACE_BIND=0.0.0.0` inside the container.
Retain the local token. Port publication belongs to the container launcher;
changing the service bind alone does not publish a host port.

For CLI or standalone MCP running inside that Exegol environment:

```bash
export RMM_SERVER_URL=http://127.0.0.1:8787
export RMM_API_TOKEN="$RMM_WORKSPACE_LOCAL_TOKEN"
python3 rmm_cli.py list
# For a stdio MCP client, use this executable command with the same environment:
# .venv/bin/python mcp_rmm_server.py
```

Here `RMM_API_TOKEN` is the client's token for the **local workspace**, not the
central administrator token. Do not share the administrator environment file
with operators. Setting up the standalone MCP endpoint is separate from the
WebUI AI loop; see [MCP parity](mcp-parity.md).

Exegol is required for the operator workspace's AI and BloodHound workflow.
Configure `RMM_EXEGOL_MCP_URL`, its credential `RMM_EXEGOL_MCP_TOKEN` when
required, in owner-local settings. Container selection is automatic through MCP
discovery and workspace identity verification; `RMM_EXEGOL_CONTAINER` is an
optional override. The workspace service must run in the selected container's
filesystem and network namespace.
It verifies ownership through MCP and ensures CE is running at startup, without
an AI request. The WebUI shows readiness; reconnects only check health.

The WebUI AI loop runs centrally. The configured Exegol MCP URL must be reachable
from both the workspace runtime and central server: `127.0.0.1:8000` refers to each caller's network
namespace, not the operator's Exegol container. Configure an authenticated
endpoint reachable over the trusted network when those services are separate.
For a bridge container calling MCP on its Docker host, use a host address
reachable from that container. A host MCP listener bound only to `127.0.0.1`
cannot accept requests to the bridge gateway address. Bind MCP to an appropriate
host interface and permit the container network through the host firewall.
Publishing RMM ports does not configure this outbound connection.
`host.docker.internal` must resolve inside the container before it can be used;
the deployment launcher does not create that alias or discover the host endpoint.
Test from inside the workspace container using the actual MCP URL. An HTTP error
response proves reachability, but only successful MCP initialization and tool
discovery establish protocol readiness.

Runtime diagnostics distinguish `exegol_mcp_connection_failed`,
`exegol_mcp_authentication_failed`, `exegol_mcp_http_error`,
`exegol_mcp_dependency_missing`, and `exegol_mcp_timeout`.
`runtime_failure_code` classifies bounded nested exception chains without exposing
exception text or credentials. Unclassified failures retain `exegol_runtime_failed`;
failures after startup dispatch retain the uncertainty state to prevent replay.

Do not select a different MCP endpoint in an individual AI request. See the
[BloodHound runtime and proxy guide](bloodhound-workflow.md) for readiness,
uncertain startup handling, and browser-facing proxy settings.

## 4. Claim sessions and install plugins

An empty workspace can mean that no sessions have been claimed. With the local
workspace environment loaded, list candidates and claim a chosen session:

```bash
curl --fail-with-body -H "Authorization: Bearer $RMM_WORKSPACE_LOCAL_TOKEN" \
  http://127.0.0.1:8787/api/v1/workspace/candidates
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $RMM_WORKSPACE_LOCAL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"REPLACE_WITH_SESSION_ID"}' \
  http://127.0.0.1:8787/api/v1/workspace/claims
```

Wait for the next workspace sync, then refresh the session list. Auto-claim is
**off by default**; enable it only when one workspace should claim all
unassigned sessions. A session already owned by another workspace is rejected.

In the **workspace UI**, open **Config → Plugins**.
Save the operator's repository/branch source, select **Load plugins**, check the
required files, and select **Install selected**. Or use `plugin sources`, `plugin releases`, and `plugin install`
through the local CLI. See [plugin source and release controls](plugin-releases-operator.md).
The workspace stores the binaries and synchronizes a workspace-specific cache
on Ubuntu. Manual copying into the shared central plugin directory is not
required for this flow.

Do not confuse saved plugin sources with beacon transport profiles. Sources
select compiled plugin releases; an RMM-Profile is a central transport process
such as Cloudflared. Embedding profiles are another separate concept: saved
model/classification settings in the workspace.

## 5. Add optional features deliberately

| Feature | Where to prepare it | Prerequisite |
| --- | --- | --- |
| Public beacon via Cloudflared or another RMM-Profile | Central Ubuntu server/container | Install the trusted profile bundle and provider executable; then set `RMM_PROFILE_AUTOSTART=1` and the selected profile |
| SOCKS | Workspace and managed agent | Install the matching SOCKS plugin through the workspace; tools use the workspace listener |
| TUN | Exegol workspace and managed agent | Install `tun.dll`; run `make tun-build` on the workspace host; Linux needs `ip`, `/dev/net/tun`, and `CAP_NET_ADMIN` |
| Go agent generation through API/WebUI | Central server | Go toolchain available centrally; the shipped runtime Docker image does not include Go |
| Document embeddings | Workspace and agent | Install the autonomous DLL, then configure/save the model profile; no ONNX compiler on either Python service |
| Rclone profiles | Workspace | Configure/upload profiles locally; the central service relays selected jobs |

The central broker needs no TUN privileges in workspace mode. TUN routes and
SOCKS listeners must share the namespace of the tools using them. The optional
workspace sidecar is not automatically in an existing Exegol tool namespace.

For a public beacon, follow [RMM-Profiles](rmm-profiles.md), then check
`python3 rmm_cli.py profile status` and use the returned beacon URL in agents.
Keep `RMM_C2_URL` pointed at the central admin API. The initial native template
disables profile autostart so missing optional bundles do not obscure setup.

## Compose alternative

Use the root `.env.example` only for Compose bootstrap and optional legacy
credential overrides. Operational settings live in each persistent data volume.
Do not overwrite an existing `.env` on updates. Initialize configuration before
first startup, for example:

```bash
docker compose build rmm-control
docker compose run --rm -T rmm-control python3 rmm_settings.py --scope server --config RMM_logs/settings.json import - < /private/path/central-settings.json
docker compose up -d rmm-control
```

The private import file is a JSON object containing the central API/beacon
secrets and workspace registry. For the sidecar, initialize its settings in the
workspace volume too, including `RMM_C2_URL=http://rmm-control:8080`, its workspace
identity/token, and a distinct local token:

```bash
docker compose run --rm -T workspace python3 rmm_settings.py --scope workspace --config /workspace/data/settings.json import - < /private/path/workspace-settings.json
```

Choose **one** workspace placement:

- Existing Exegol container: follow step 3, using a reachable central URL.
- Example sidecar on the Docker host: `docker compose --profile workspace up --build -d`.
  This is a workspace service, not a full Exegol tools image.

The shipped Compose file publishes ports on host interfaces; restrict access
with host/cloud firewall rules or adjust the mappings for your network.
The sidecar binds inside its container and publishes host port 8787. Its TUN
configuration requires a Linux host with `/dev/net/tun`; the central service
has no such requirement. Container port numbers remain 8080/8081/8787 even if
you change the published host ports in `.env`.

The image includes Python dependencies, the native helper, and Linux `ip`.
It does not include the Go compiler, Cloudflared, or private profile/plugin
bundles. Install optional providers in a derived image when needed. Both
services use the same image, so an image build compiles the helper once;
ordinary process startup compiles nothing.

The example defines one workspace and one data volume. For multiple operators,
use separate workspace services/projects with distinct IDs, tokens, volumes,
and published ports. Do not scale copies sharing the same identity and volume.
See [workspace reference](workspace-deployment.md) for the protocol and volume
layout. Rclone configuration is optional; create the local profile file only
when using that feature.

## Single-workspace and multi-workspace presets

Standalone now means one workspace, not server-owned operator storage. Prepare
protected configuration files without printing credentials:

```bash
python3 rmm_deployment.py prepare --preset single --output /path/to/deployment
python3 rmm_deployment.py run --root /path/to/deployment --component single
```

The single launcher supervises separate control and workspace processes on the
same host. Run it inside Exegol when using AI, collection, or local pivots.
When `--central-url` is omitted, preparation derives the workspace connection
URL from the saved central administration bind and port; wildcard binds become
loopback addresses. Supply an explicit externally reachable URL for remote
workspaces or a TLS reverse proxy.
Connect operators to the workspace WebUI, normally port 8787; the central
administration listener remains on port 8080. Configure the workspace MCP URL
before using AI/BloodHound. Container selection is automatic.

For multiple workspaces, prepare named identities and a central URL reachable
from each Exegol environment:

```bash
python3 rmm_deployment.py prepare --preset multi --output /path/to/deployment --workspace-id alice --workspace-id bob --central-url https://rmm.example
python3 rmm_deployment.py run --root /path/to/deployment --component control
python3 rmm_deployment.py run --root /path/to/deployment --component workspace --workspace-id alice
```

Distribute each workspace's configuration only to its owner; do not give operators
the central registry or another workspace's credentials. The preset creates
`central/settings.json`, `workspaces/ID/settings.json`, and deployment metadata.
It does not create Exegol containers, configure a VPN, or install external tools.
Separate-host Tailscale/WireGuard deployment remains planned work.

`rmm_deployment.write_private` writes protected configuration files;
`prepare` generates preset credentials and settings; `commands` resolves the
selected component commands and checks the workspace identity against deployment
metadata; `child_environment` filters known settings to each service's scope;
`run_services` supervises child processes and
their shutdown; `main` validates the CLI. The central and workspace settings
stores remain separate even when the single launcher starts both locally.
Nonempty environment overrides valid for that service still take precedence
over saved settings; remove old overrides before relying on generated configuration.
The launcher does not forward central-only credentials to the workspace. It
also removes legacy singleton workspace credentials from the central environment,
which uses the prepared registry (or an explicit `RMM_WORKSPACES` override).

The WebUI `configureWorkspaceAccess` helper uses the authenticated health
response to expose AI/BloodHound controls only on a workspace connection.
Central administration shows an operator-routing notice and does not poll
local BloodHound readiness. `renderDeploymentMode` labels workspace identity,
central administration, and legacy deployments distinctly.

A sole registered workspace receives new sessions automatically. With multiple
registered workspaces, use explicit claims and preserve existing ownership.

Legacy data must be migrated before switching an existing installation. See
[workspace migration](workspace-migration.md); the migration copies data to a
fresh destination and never deletes the source.
When preparing an upgrade, pass `--legacy-settings /old/RMM_logs/settings.json`
to `prepare` so saved beacon/API secrets, public URL, and profile configuration
are retained. Existing workspace registrations require explicit reconciliation;
the importer will not silently replace them. Preserve environment-only beacon
secrets through the environment or import them into protected settings before
switching, otherwise existing agents will no longer authenticate.

## Verify, update, and preserve data

| Symptom/check | What to inspect |
| --- | --- |
| Workspace `/ui/` loads, API returns 401 | Enter the workspace local token; do not use the central admin/workspace transport token |
| Workspace has no sessions | Correct central admin URL, matching ID/token, connectivity, and explicit claims |
| Plugin source reports missing credentials | Save the referenced credential in workspace service settings, remove conflicting environment overrides, and restart |
| Plugin install succeeds locally but reports sync failure | Central reachability/token and its 128 MiB `RMM_MAX_BODY_BYTES` setting; retry install |
| Missing TUN helper or `ip` | Install on the pivot owner's host/namespace; ordinary startup does not build it |
| Cloudflared unavailable | Install provider and trusted bundle centrally, or keep profile autostart disabled |
| Go agent generation fails | Install Go on the central build host or use a suitable derived image |
| Permission errors in runtime directories | Preserve volume ownership; do not recursively chmod/chown just to make Git inspect runtime data |

Back up each workspace data directory, the central `RMM_logs` control state,
profile bundles, persistent plugin caches, and the external environment files.
Use a consistent SQLite backup or stop the workspace while copying its database
and WAL files. A persistent directory survives a service restart only if its
underlying volume/mount survives container replacement.

For native deployments, update `v2`, update dependencies when they change, and
restart the affected Python process. For Compose, rebuild/recreate the affected
service after code changes, retaining named volumes. Never use `down -v` or
`git clean` as an update step. Plugin upgrades happen separately through the
workspace release controls; RMM updates do not rebuild plugin DLLs.

### Login regression coverage

`tests/test_workspace_login.py` uses `WorkspaceLoginTests` to verify the public
static-login boundary against authenticated operator APIs. `setUp` creates a
temporary workspace and loopback HTTP server; `tearDown` closes both and removes
the temporary store. `request` sends exact paths and an optional bearer token.
`test_login_assets_load_before_api_authentication` checks public UI assets and
protected API access. `test_public_static_routing_does_not_allow_traversal_or_other_methods`
checks traversal rejection and authentication for non-GET methods.

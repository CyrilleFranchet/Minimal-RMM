# Deployment guide

`rmm_deployment.py` is the supported service launcher on branch **v2**.
Use it for preparation, native single mode, the distributed central container,
and the workspace process inside Exegol. Direct `server_rmm.py`,
`workspace_service.py`, old environment-file recipes, and a generic workspace
Docker sidecar are implementation/compatibility details, not deployment paths.

## Three infrastructure models

| Model | Status | Placement |
| --- | --- | --- |
| Single | Supported; local testing | Central and one workspace are separate Python processes together inside the existing Exegol environment. One wrapper supervises both; no dedicated RMM Docker container. |
| Distributed on one host | Supported | Central runs in a dedicated Docker container. Each workspace runs inside its existing Exegol container/environment on the same host. Only central uses the RMM Docker image. |
| Distributed across hosts with an overlay | Future work | Central and Exegol workspaces on different machines, connected through an overlay such as Tailscale. There is no supported overlay deployment recipe yet. |

Single describes one operator deployment, not a combined service or shared
credentials. Both supported models use identical control/workspace ownership.
A workspace and its Exegol tools must share a filesystem and network namespace;
the central service can be elsewhere and must not need that filesystem.

## One storage convention, separate owners

Keep the deployment root outside the source checkout, on a persistent mount:

```text
/workspace/rmm-deployment/
├── deployment.json                  # schema, topology preset, workspace IDs; no secrets
├── central/
│   ├── settings.json                # admin/beacon secrets, workspace registry, service settings
│   ├── sessions.json                # live session metadata and ownership
│   ├── profiles/                    # installed RMM-Profile transport bundles
│   ├── plugin-cache/                # disposable workspace-scoped delivery copies
│   ├── assets/                      # central-managed assets, including optional skills
│   ├── ui-state.db                  # central administration UI state
│   └── ...                          # central jobs, build artifacts, diagnostics
└── workspaces/
    └── default/
        ├── settings.json            # upstream/local tokens, Exegol, AI defaults, feature settings
        ├── workspace.db             # sessions, events, UI settings, AI chat, embeddings
        ├── assets/
        │   ├── metadata.json        # installed files and explicit enablement
        │   ├── plugin-sources.json  # saved source repositories and credential references
        │   ├── plugins/
        │   ├── skills/
        │   └── rclone/              # registered profile files, commonly profiles.json
        ├── artifacts/               # downloads, screenshots, keylogs
        ├── parts/                   # interrupted transfer staging
        └── bloodhound/              # owner-local workflows, evidence, lifecycle state
```

There is one directory convention, not one shared settings file or database.
Each service's authoritative configuration and state belong to its owner.
`settings.json` contains credentials: back up privately, retain owner-only
permissions, and keep it out of Git and Docker build contexts. The deployment
root is never inferred from the working directory or a shell's old variables.

In distributed mode, copy/mount only `deployment.json` plus `central/` for the
central service. Give Exegol only `deployment.json` plus its own
`workspaces/default/` subtree. These may have different absolute paths on each
machine/container. Neither role needs the other role's settings or database.

## Prepare the source environment

Inside the environment where a native component will run:

```bash
git switch v2
git pull --ff-only
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Use an existing configured Python environment if appropriate. Before TUN use,
build the workspace helper with `make tun-build` and install the external
`tun.dll` plugin through workspace assets/releases. Exegol needs Linux
`/dev/net/tun` and `NET_ADMIN`; central does not. Service startup never builds
TUN or installs plugins. See [TUN](tun-pivot.md) and [plugin releases](plugin-releases-operator.md).

## Single: start everything together

Prepare once, inside Exegol, using a fresh destination:

```bash
python3 rmm_deployment.py prepare --preset single \
  --output /workspace/rmm-deployment
```

Preparation generates distinct admin, beacon, workspace, and local operator
secrets. It refuses to overwrite an existing deployment. Configure optional
features in the two generated `settings.json` files, using Config, offline
`rmm_settings.py`, or authenticated settings APIs; see the
[configuration reference](service-settings.md). Never put workspace settings
in the central file or copy central admin credentials into Exegol settings.

Start both components:

```bash
python3 rmm_deployment.py run --root /workspace/rmm-deployment --component single
```

The wrapper supervises the two processes and shuts down the peer if one exits.
Ctrl+C stops both. Open `http://127.0.0.1:8787/ui/` from the workspace namespace
and use the workspace's local operator token. Central admin is `:8080`; agents
use beacon `:8081` or the configured transport URL. Published Exegol ports or
host networking are container-launcher responsibilities.

Loopback binds are the safe defaults. If a browser outside Exegol needs the
workspace UI, or remote agents need the beacon listener, save the necessary
binds before startup:

```bash
python3 rmm_settings.py --scope workspace \
  --config /workspace/rmm-deployment/workspaces/default/settings.json \
  set RMM_WORKSPACE_BIND 0.0.0.0
python3 rmm_settings.py --scope server \
  --config /workspace/rmm-deployment/central/settings.json \
  set RMM_BEACON_BIND 0.0.0.0
```

Publish workspace port 8787 and beacon port 8081 through the existing Exegol
launcher as needed. Keep central administration loopback-only when both
processes share Exegol. Configure the agent-facing beacon URL separately;
changing a bind does not create a public transport URL.

## Distributed on one host

Prepare outside the checkout on the Docker host:

```bash
python3 rmm_deployment.py prepare --preset distributed \
  --output /srv/rmm-deployment \
  --central-url http://192.0.2.10:8080
```

Replace `192.0.2.10` with the host address reachable from Exegol. Repeat
`--workspace-id NAME` for multiple workspaces. With one registered workspace,
new sessions are assigned to it; multiple workspaces use explicit claims.
The distributed preset saves `0.0.0.0` container listener binds. The external
URL must match the reachable administration endpoint; if choosing different
ports, save the corresponding listener settings too.

Start the dedicated central container through the wrapper:

```bash
python3 rmm_deployment.py run --root /srv/rmm-deployment --component control \
  --docker --admin-publish-bind 192.0.2.10
```

The wrapper derives `compose.generated.json` from saved listener ports and local
paths, then runs Docker Compose in the foreground. That file is generated output,
not a second operational configuration source. Re-run the wrapper after changing
settings to regenerate it. Remove redundant absolute host storage overrides
before container startup, or use owner-relative values such as `profiles` for
`RMM_PROFILES_DIR`; the container mounts central at `/deployment/central`. The defaults publish admin on host loopback and beacon
on all interfaces; set the admin bind to an appropriate host address when a
bridge-networked Exegol container must reach it. Do not use the beacon URL as
the workspace's `RMM_C2_URL`. Ctrl+C stops the container through Compose.

Central mounts only its own subtree plus read-only `deployment.json`; it gets
no workspace database, rclone credentials, TUN device, or `NET_ADMIN`. The image
contains Python and the Go toolchain for agent generation; workspace TUN helpers
belong in Exegol. The retired root Compose sidecar recipe is not supported.

Place the matching metadata and selected workspace subtree on Exegol's
persistent volume, then run inside Exegol:

```bash
python3 rmm_deployment.py run --root /workspace/rmm-deployment \
  --component workspace --workspace-id default
```

The workspace initiates authenticated connections to central. Keep its local
WebUI/CLI token distinct from the workspace-to-central token. Shared volumes
between central and Exegol are not required. Overlay connectivity, TLS/routing
automation across machines, and Tailscale integration remain future work.

## Find the active files

```bash
python3 rmm_deployment.py paths --root /workspace/rmm-deployment --workspace-id default
python3 rmm_deployment.py paths --root /srv/rmm-deployment --component control
```

These print paths and ownership only, never credential values. Normal wrapper
startup uses saved service settings; inherited service environment variables
are ignored. `--allow-env-overrides` is an explicit compatibility escape hatch
for native startup, not the recommended configuration method. Wrapper-owned
storage paths stay pinned and a workspace identity override cannot select a
different owner's data. See [settings and variable inventory](service-settings.md).

## Rclone profiles: registration is required

Copying `profiles.json` into `workspaces/default/assets/rclone/` **does not
register or activate it**. Assets are selected through `assets/metadata.json`.
Use the workspace WebUI's **Config → Runtime assets → rclone profiles → Upload
profiles.json**, the connected asset API/CLI, or this offline import into a
stopped workspace with a fresh destination:

```bash
python3 rmm_deployment.py import-rclone --root /workspace/rmm-deployment \
  --workspace-id default --file /private/path/profiles.json
```

The import registers the file and refuses to replace existing content. If the
file was already copied manually, use WebUI upload to replace/register it
explicitly; do not hand-edit inventory metadata. Invalid enabled assets are
reported and prevent transfer job construction. Missing, empty, or disabled
profiles do not activate repository examples or legacy environment profiles.
Check `get_rclone_config` via MCP or `rclone-config` via CLI against the workspace,
not central. See [rclone configuration](rclone-exfil.md).

## Existing installations

Back up the existing owner roots before moving anything. Never prepare over an
existing deployment or regenerate tokens merely to fix a profile lookup.
Existing `single` and `multi` metadata remain readable. Use the generated root
and its existing credentials. Move old external profile bundles and mutable
assets into their corresponding owner subtree, remove redundant absolute
storage-path overrides from settings, then use `paths` to verify the result.
Relative executable/helper paths in saved settings resolve against the owner
directory; installed bare executable names remain PATH-based where supported.

`prepare --legacy-settings FILE` imports a standalone central settings source
into a fresh deployment and preserves its existing admin/beacon secrets. It
refuses an existing workspace registry that needs explicit reconciliation.
[Workspace migration](workspace-migration.md) covers old durable server data.
Do not delete users' old settings, environment files, or data automatically.

## Wrapper implementation

| Symbol | Responsibility |
| --- | --- |
| `write_private` | Create a new owner-only JSON file without replacement. |
| `prepare` | Validate topology and prepare isolated credentials/settings and asset directories. |
| `commands` | Validate metadata/identity and resolve native component commands. |
| `owner_paths` | Derive owner-root storage and resolve relative helper paths; reject conflicting saved storage locations. |
| `child_environment` | Filter inherited service settings by default; support explicit scoped compatibility overrides. |
| `run_services` | Supervise child processes and forward shutdown with bounded cleanup. |
| `deployment_paths` | Report one owner's config/data locations without secrets. |
| `import_rclone` | Validate and register an explicit profile asset without replacing another file. |
| `central_compose` | Generate a distributed central-only container definition from saved settings. |
| `main` | Dispatch preparation, startup, paths, and offline profile import. |

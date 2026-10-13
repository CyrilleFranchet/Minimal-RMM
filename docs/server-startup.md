# Server startup wrapper

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the control-plane and workspace
topology.

Use `run_server.py` when starting a development or lab server:

```bash
python3 run_server.py
```

The wrapper performs these steps from the repository root:

1. Runs `make tun-build`, which writes the server-side helper to
   `tools/rmm-tun`.
2. Reports the deployed artifacts found in `RMM_PLUGINS_DIR`.
3. Reports the installed RMM-Profile bundles found in `RMM_PROFILES_DIR`.
4. Replaces the wrapper process with `server_rmm.py`, passing through all
   server arguments.

The wrapper does not build agent plugins. Build and test those plugins in the
private [external plugin repository](plugin-repository.md), then install its
release artifacts into `RMM_PLUGINS_DIR`.

It also does not fetch profile executables. Install trusted bundles from the
private [RMM-Profile repository](rmm-profiles.md) into `RMM_PROFILES_DIR`.

The server can still be started directly with:

```bash
python3 server_rmm.py
```

That direct path skips the TUN helper build and is useful when the helper and
plugin artifacts are already present.

## Listener ports and automatic profile startup

The default listeners are:

```text
administration: 127.0.0.1:8080
beacon:         127.0.0.1:8081
```

Set `RMM_ADMIN_PORT` and `RMM_BEACON_PORT`, or pass `--admin-port` and
`--beacon-port`. The administration listener serves the WebUI and operator
API. The beacon listener serves agent and pivot endpoints. `RMM_PROFILE_NAME`
selects the installed connector and `RMM_PROFILE_AUTOSTART=1` starts it after
the listeners bind. See [RMM-Profiles](rmm-profiles.md) for the Cloudflared
bundle and its `trycloudflare.com` URL handshake.

The server publishes the active agent URL through:

```bash
python rmm_cli.py profile status
curl -H "Authorization: Bearer $RMM_API_TOKEN" \
  http://127.0.0.1:8080/api/v1/transport
```

Use that beacon URL in the PowerShell or Go agent generator. Keep the
administration URL for CLI, MCP, and WebUI access.

## Docker plugin store

The Compose control plane mounts `/app/plugins` from the persistent
`rmm-plugin-store` volume. The image contains only the server-side `rmm-tun`
helper. Install the external plugin repository's release artifacts into this
volume before starting pivots.

For direct host-side deployment, set this in `.env`:

```text
RMM_PLUGIN_STORE=./plugin-store
```

Then copy the release bundle's `*.dll` and `*.ps1` artifacts into
`plugin-store/`. The next `GET /api/v1/plugins` request sees them, and the Go
agent can load them through `pe load` without rebuilding or restarting the
server. Keep the server-side TUN helper in `tools/rmm-tun`; it is not an agent
plugin artifact.

Compose also mounts `/app/profiles` from the persistent `rmm-profile-store`
volume. Install the trusted profile bundle from `Minimal-RMM-profiles` under
that volume. The control-plane image must contain the provider executable,
such as `cloudflared`, in `PATH` or at the configured profile path.

For a host bind mount during development:

```bash
git clone https://github.com/CyrilleFranchet/Minimal-RMM-profiles.git profiles
mkdir -p profile-store
cp -R profiles/cloudflared profile-store/
RMM_PROFILE_STORE=./profile-store docker compose up --build -d rmm-control
```

## Multi-operator workspace deployment

For several operators, run the central server as a control-plane broker and run
one `workspace_service.py` instance in each operator's Exegol container. Set
`RMM_STORAGE_MODE=workspace` and configure `RMM_WORKSPACES` with a unique token
for every workspace. The central process keeps session ownership, command
queues, agent transport, transient event queues, and build artifacts. Each
workspace process owns its SQLite database, transcript, artifacts, AI chat,
SOCKS listener, and TUN interface.

The repository includes a Compose deployment:

```bash
cp .env.example .env
# Replace every example secret in .env.
docker compose up --build rmm-control
docker compose --profile workspace up --build
```

The example workspace UI is published at `http://127.0.0.1:8787/ui/`. The
control-plane service intentionally has no `/dev/net/tun` device and no
`CAP_NET_ADMIN`. The workspace service receives both because it owns the local
routes. See [`workspace-deployment.md`](workspace-deployment.md) for the
container layout, claims, API headers, and Exegol attachment procedure.

An existing Exegol container can run the local service directly after joining
the Compose network:

```bash
RMM_C2_URL=http://rmm-control:8080 \
RMM_WORKSPACE_ID=alice \
RMM_WORKSPACE_TOKEN="$ALICE_WORKSPACE_TOKEN" \
RMM_WORKSPACE_DATA=/workspace/.minimal-rmm \
python3 workspace_service.py
```

Point `RMM_SERVER_URL` at the local workspace service when using
`rmm_cli.py`. If `RMM_WORKSPACE_LOCAL_TOKEN` is set, pass that value as the
CLI's `RMM_API_TOKEN`; the local service forwards its own workspace credentials
to the central server.

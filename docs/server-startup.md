# Server startup wrapper

Use `run_server.py` when starting a development or lab server:

```bash
python3 run_server.py
```

The wrapper performs these steps from the repository root:

1. Finds every checked-in Go plugin under `agent-plugins/`.
2. Builds each plugin for Windows amd64 with the same compiler path and build
   settings as `POST /api/v1/plugins/build`.
3. Writes the plugin DLLs to `RMM_logs/plugins/`.
4. Runs `make tun-build`, which writes the server-side helper to
   `tools/rmm-tun`.
5. Replaces the wrapper process with `server_rmm.py`, passing through all
   server arguments.

The wrapper currently builds `rclone-exfil`, `screenshot`, `socks`, and `tun`.
It fails before starting the server when Go, the configured mingw-w64
compiler, or the TUN helper build is unavailable. Set `RMM_PLUGIN_CC` to use a
different Windows cross compiler and `RMM_PLUGIN_BUILD_TIMEOUT` to change the
per-plugin build timeout.

The server can still be started directly with:

```bash
python3 server_rmm.py
```

That direct path skips the startup builds and is useful when the artifacts are
already present.

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

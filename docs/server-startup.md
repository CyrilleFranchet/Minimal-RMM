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

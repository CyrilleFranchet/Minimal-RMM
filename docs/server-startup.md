# Server startup reference

For a complete setup, follow the [deployment guide](deployment.md). This page
is the argument and helper reference. Use [workspace deployment](workspace-deployment.md)
for workspace claims, storage, and protocol details.

## Entry points

| Process | Entry point | Configuration owner |
| --- | --- | --- |
| Central broker or standalone server | `python3 server_rmm.py` | Central/server administrator |
| Operator workspace | `python3 workspace_service.py` | Individual operator |
| CLI | `python3 rmm_cli.py` | URL/token for the selected server or workspace |
| Standalone MCP | `python3 mcp_rmm_server.py` | Same API URL/token as the CLI |

Run native commands from the repository root, using the configured venv.
Startup compiles neither agent plugins nor the native TUN helper. Docker and
Compose use the same direct Python entry points.

Neither entry point loads `.env` automatically. Use the role-specific native
[central](../deploy/central.env.example) and [workspace](../deploy/workspace.env.example)
templates with the explicit loading commands in the deployment guide. The
root `.env.example` is the Compose input, not a shell startup script.

## Central server arguments

| Argument | Meaning |
| --- | --- |
| `--admin-port N` | Operator/workspace API port; `RMM_ADMIN_PORT`, default 8080 |
| `--beacon-port N` | Agent listener port; `RMM_BEACON_PORT`, otherwise admin port + 1 |
| `--bind ADDRESS` | Administration bind; default 127.0.0.1 |
| `--beacon-bind ADDRESS` | Beacon bind; set explicitly for remote agents (initial default is loopback unless `RMM_BEACON_BIND` is set) |
| `--token TOKEN` | Central administrator credential; prefer environment `RMM_API_TOKEN` |
| `--beacon-secret SECRET` | Agent shared secret; prefer `RMM_BEACON_SECRET` |
| `--profile NAME` | Installed beacon transport profile; `RMM_PROFILE_NAME`, default cloudflared |
| `--cli` | Legacy embedded console; default is headless API mode |
| `--rclone-profiles PATH` | Profile file for server-owned operation; workspace profiles belong to the workspace instead |
| `--rclone-max-bytes N` | Server-owned transfer limit; zero means unlimited |
| `--insecure` | Lab-only bypass of required secrets |
| Positional `port` | Legacy alias for the admin port |

`RMM_STORAGE_MODE` selects `server` or `workspace` through the environment;
there is no corresponding CLI flag. Direct Python defaults to `server`.
The image and shipped Compose configuration default to `workspace`.
`RMM_WORKSPACES` maps workspace IDs to their transport tokens. It does not
replace the administrator token or the agent beacon secret.

## Transport profiles

The admin listener serves the operator API. The beacon listener serves agents.
An RMM-Profile forwards traffic to the beacon listener only. Install the trusted
bundle in `RMM_PROFILES_DIR` and its provider executable centrally before
setting `RMM_PROFILE_AUTOSTART=1`. Set it to `0` for direct beacon operation.
`RMM_BEACON_PUBLIC_URL` supplies the externally reachable URL for direct or
externally managed transport. Check the actual active URL with:

```bash
python3 rmm_cli.py profile status
```

Configure that CLI with either the central admin URL/token or the workspace
URL/local token; the latter proxies transport status. Use the returned beacon
URL in agents and keep workspaces pointed at the central admin API.
See [RMM-Profiles](rmm-profiles.md) for bundle/provider installation.

## Prepare TUN only where needed

Outside Docker, run the explicit setup command on the host that owns the pivot:

```bash
make tun-build
```

This requires Go and writes `tools/rmm-tun`. Repeat after helper source,
toolchain, or platform changes. The Docker image builds this executable and
includes Linux `ip`; the central broker still does not need TUN privileges
when operating in workspace mode.

Select an existing custom executable with `RMM_TUN_HELPER` in server mode or
`RMM_WORKSPACE_TUN_HELPER` in workspace mode. These variables do not compile or
install it. Linux TUN also needs `/dev/net/tun` and `CAP_NET_ADMIN` in the pivot
owner's namespace. Requesting TUN without its helper returns the expected path;
server mode also reports `make tun-build`. Ordinary startup remains available.

## Plugin and profile stores

In workspace mode, install plugins from the workspace's [release controls](plugin-releases-operator.md).
The workspace owns sources and files; the central server serves its synchronized
workspace-specific runtime cache. The central `/app/plugins` volume can also
hold administrator-seeded shared plugins, but it is not the operator's normal
installation path. Server mode installs into the server-owned store instead.

Compose's `RMM_PLUGIN_STORE` and `RMM_PROFILE_STORE` select host bind mounts or
named volumes. Python's `RMM_PLUGINS_DIR` and `RMM_PROFILES_DIR` select runtime
paths. These names are not interchangeable. Profile bundles stay centrally
owned in both storage modes.

## Migrate existing launch commands

The former `run_server.py` wrapper has been removed. Replace it with
`server_rmm.py` in shell commands, systemd units, and process-manager settings,
keeping server arguments. Set the working directory to the repository root:
the wrapper no longer changes it for you. Remove `--rebuild-tun` and
`RMM_TUN_REBUILD`; use `make tun-build` during setup when needed.
Preserve existing environment files, secrets, directories, and volume ownership.
For installed inventory, use `plugin list` and `profile status` or the UI.

# Dynamic agent plugins

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the complete runtime topology.
The source split is implemented in the private
[external plugin repository](plugin-repository.md).

Minimal-RMM keeps the server-side plugin delivery mechanism in the Docker
image, while plugin artifacts that are added or replaced after deployment live
in the persistent plugin volume. The server does not need to be rebuilt or
restarted when an artifact is added to that volume.

## Runtime boundary

```text
Docker image
  ├── Python control plane
  ├── server-only helpers such as rmm-tun
  └── no agent plugin source or build toolchain

Persistent plugin volume (/app/plugins)
  ├── socks.dll
  ├── tun.dll
  ├── screenshot.dll
  └── operator-installed plugins

Go agent
  └── generic PE loader + child process host
```

The `rmm-control` service mounts the plugin volume at `/app/plugins`. The
container entrypoint creates the directory but does not build or seed agent
plugins. This keeps plugin release cadence independent from control-plane image
updates.

Set `RMM_PLUGIN_STORE=./plugin-store` in `.env` when a host directory is more
convenient than a named Docker volume. The directory is mounted at
`/app/plugins`, so copying a plugin into it makes the artifact visible to the
server immediately:

```bash
mkdir -p plugin-store
cp my-command.dll plugin-store/
```

Operators can also install `.dll` and `.ps1` artifacts from the WebUI, CLI, or
MCP with the URL asset API. In workspace mode, the source copy persists in the
operator's Exegol data directory and the central volume receives only the
runtime copy required by the beacon endpoint. See
[operator-managed assets](operator-assets.md).

For a named volume, use a dedicated short-lived container or a plugin sync
service to copy the release bundle into the volume. Do not copy plugin source
code into the control-plane container at runtime.

For the default Compose project, the short-lived copy can be performed with:

```bash
docker run --rm \
  -v minimal-rmm_rmm-plugin-store:/plugins \
  -v "$PWD/dist:/bundle:ro" \
  alpine:3.20 sh -c 'cp /bundle/*.dll /bundle/*.ps1 /plugins/'
```

Use the actual Compose project volume name when it differs from
`minimal-rmm_rmm-plugin-store`.

## Plugin-owned commands

The agent core has one generic plugin operation. It does not contain a case or
handler for every plugin command. The operator queues a plugin artifact and an
export through the existing PE endpoint:

```text
pe load my-command.dll --export Scan --session <id> '{"target":"10.20.0.0/16"}'
```

The agent performs the following steps:

1. Fetch `my-command.dll` from the authenticated server endpoint.
2. Start the isolated plugin child process.
3. Map the DLL in memory.
4. Invoke the requested export, such as `Scan`.
5. Return the export output as a normal agent result event.

The command name, argument schema, validation, and implementation belong to
the plugin. The server only validates generic plugin, export, and command
tokens and forwards the opaque input. Adding a new command therefore does not
require a new branch in `agent-go/main.go` or a new server command
implementation.

For a plugin-owned command namespace, use the optional `command` field on the
same endpoint:

```bash
rmm_cli.py pe load network.dll --command scan --session <id> \
  '{"target":"10.20.0.0/16"}'
```

This queues `__PLUGIN_COMMAND__`. The Go agent always invokes `Run` and passes
the command name and opaque input as a JSON envelope. The server records the
safe command name for lifecycle auditing but does not implement it.

A Go plugin can also expose only `Run` and dispatch a command envelope inside
the DLL. That is useful when the plugin wants one stable export and several
subcommands:

```json
{
  "command": "scan",
  "args": {"target": "10.20.0.0/16"}
}
```

The current loader starts one child per one-shot command. The DLL is not left
loaded in the beacon process, and plugin-specific state does not become part
of the main agent. Persistent services such as SOCKS and TUN remain separate
long-lived pivot plugins with their existing lifecycle controls.

## Publishing artifacts

The recommended external plugin repository publishes compiled artifacts rather
than source bundles consumed by the RMM server. A release should contain:

- the plugin artifact, such as `my-command.dll`;
- a manifest with its ABI, platform, version, exports, and SHA-256;
- a signature or trusted release checksum when the deployment requires supply
  chain verification.

Installation should copy the artifact into the plugin volume atomically: write
to a temporary name, flush and close it, then rename it into place. The server
will observe the new file on the next inventory request and serve it to agents
without a restart.

The server has no agent-plugin build endpoint. Production and development
deployments install release artifacts into the volume and keep the server image
focused on the control plane.

## Scope of hot loading

Hot loading covers one-shot plugin commands immediately. A running persistent
SOCKS or TUN child keeps the version it started with; stop and restart that
relay to use a replacement artifact. Making arbitrary persistent plugins
self-describing and lifecycle-managed is a separate extension to the beacon
protocol.

## Functions and files

| Item | Role |
| --- | --- |
| `docker-entrypoint.sh` | Create the persistent plugin volume directory |
| `server_rmm.py:plugin_directory` | Resolve the runtime plugin store |
| `server_rmm.py:list_plugin_files` | Inventory artifacts from the runtime store |
| `server_rmm.py:RMMHandler._serve_plugin_tool` | Serve an authenticated artifact to an agent |
| `agent-go/main.go:loadPluginCommand` | Parse a generic plugin load or command request |
| `agent-go/main.go:pluginCommandEnvelope` | Pass a plugin-owned command to `Run` |
| `agent-go/pe_loader_windows.go:loadPluginPE` | Fetch, isolate, map, and invoke the plugin |
| `rmm_cli.py:RmmApiClient.queue_pe_load` | Queue a plugin command from the operator CLI |

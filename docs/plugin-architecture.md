# Dynamic agent plugins

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the complete runtime topology.
The source split is implemented in the private
[external plugin repository](plugin-repository.md).

Minimal-RMM keeps plugin delivery in the control plane, while each workspace
owns its installed artifacts and release configuration. Installing a plugin
does not require rebuilding or restarting the server.

## Runtime boundary

```text
Workspace data/assets/plugins/  (authoritative operator-owned artifacts)
        │ authenticated workspace publication
        ▼
Central data/plugin-cache/workspaces/<workspace-id>/  (delivery copies only)
        │ session-scoped beacon download
        ▼
Go or PowerShell agent
```

Install release artifacts through the workspace WebUI, CLI, or MCP release
controls or URL asset API. The workspace persists source files and reconciles
them with central at startup and every 30 seconds after each reconciliation ends.
Only missing or changed enabled bytes are transferred; disabled and orphaned
cache entries are revoked. Central serves only the cache belonging to the
requesting agent's workspace; there is no shared global plugin fallback.
Corrupt local inventory metadata aborts reconciliation without deleting cached
publications; repair the workspace metadata before synchronization can resume.
See [operator-managed assets](operator-assets.md).

The cache defaults to `RMM_LOG_DIR/plugin-cache`, configurable with
`RMM_PLUGIN_CACHE_DIR`. Compose keeps it in the existing central state volume;
there is no separate plugin-store mount. Do not install files directly into
the cache or share it as an operator asset store. Profile bundles remain
centrally owned under `RMM_PROFILES_DIR`.

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

Install through the workspace's release or asset API. Publication writes the
central delivery copy atomically; subsequent agent downloads use the new file.
The server has no agent-plugin build endpoint or global installation store.

## Scope of hot loading

Hot loading covers one-shot plugin commands immediately. A running persistent
SOCKS or TUN child keeps the version it started with; stop and restart that
relay to use a replacement artifact. Making arbitrary persistent plugins
self-describing and lifecycle-managed is a separate extension to the beacon
protocol.

## Functions and files

| Item | Role |
| --- | --- |
| `docker-entrypoint.sh` | Start services and prepare the central profile directory |
| `server_rmm.py:plugin_directory` | Resolve a workspace-scoped delivery cache |
| `server_rmm.py:list_plugin_files` | Inventory artifacts for one workspace |
| `server_rmm.py:plugin_path_for_session` | Resolve only the registered session owner's cache; no global fallback |
| `server_rmm.py:RMMHandler._serve_plugin_tool` | Serve an authenticated artifact to an agent |
| `agent-go/main.go:loadPluginCommand` | Parse a generic plugin load or command request |
| `agent-go/main.go:pluginCommandEnvelope` | Pass a plugin-owned command to `Run` |
| `agent-go/pe_loader_windows.go:loadPluginPE` | Fetch, isolate, map, and invoke the plugin |
| `rmm_cli.py:RmmApiClient.queue_pe_load` | Queue a plugin command from the operator CLI |

`PluginDirectoryTests` and `PluginServeTests` verify scoped inventory and delivery;
the `_plugin_file` test helper creates artifacts under a workspace cache. Route
regressions reject central installation and verify that publishing or disabling
Alice's plugin cannot change Bob's copy.

The settings loader accepts the retired `RMM_PLUGINS_DIR` key only to keep older
saved configurations readable. Runtime code ignores it; remove it after migration.

Plugin deletion disables the workspace asset before requesting central revocation.
If synchronization fails, the API returns 502 and retains the disabled record;
retry deletion after restoring connectivity. Background synchronization
also retries revocation of disabled records. Until central
acknowledges revocation, its existing copy may still be downloadable.
`WorkspacePluginRevocationTests` covers this failure and retry sequence.

`serialize_plugin_mutation` serializes local plugin installation, enable/disable,
and deletion with reconciliation. `_sync_plugin_asset` rereads authoritative local
metadata rather than publishing a stale caller snapshot. `_publish_plugin_asset`
sends that locked snapshot; `_sync_workspace_plugins` compares the authenticated
remote inventory with local assets, and `_plugin_sync_loop` retries independently
of session/event polling. Shutdown waits for this worker before closing storage.
Use one workspace process per workspace identity and data directory; this lock
does not coordinate multiple processes sharing the same store.

`WorkspacePluginSyncTests` covers recovery after an unavailable central server,
cache loss, unchanged-content suppression, orphan/disabled revocation, stale
metadata, corrupt inventories, kind aliases, and concurrent publication/deletion. The fixture
`request` emulates the authenticated central inventory and publication API.

Workspace partitioning is not per-agent cryptographic authentication. Beacon
downloads still use the deployment's shared beacon secret and a registered session
ID. They resolve that session's current owner; this does not defend against a
caller possessing the shared secret and impersonating another known session.

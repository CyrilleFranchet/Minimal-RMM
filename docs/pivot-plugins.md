# Pivot plugins

See the [documentation wiki](README.md) and the [architecture overview](architecture.md)
for the deployment topology and privilege boundary. This page focuses on the
agent-side plugin packages and their task protocol.

SOCKS5 is the first pivot type exposed by the WebUI. The server keeps the
operator-side listener in `rmm_socks.py`; each agent-side relay is loaded only
while the server reports its corresponding `socks_active` or `tun_active` flag.

## Plugin packages

| Agent | Plugin | Delivery | Relay path |
| --- | --- | --- | --- |
| Windows PowerShell | `socks.ps1` from the external plugin bundle | Authenticated artifact fetch from `/tools/plugins/socks.ps1` | HTTP `GET/POST /socks` |
| Windows amd64 Go | `socks.dll` from the external plugin bundle | Deployed PE artifact from `/tools/plugins/socks.dll` | HTTP `GET/POST /socks` |
| Windows amd64 Go | `tun.dll` from the external plugin bundle | Deployed PE artifact from `/tools/plugins/tun.dll` | HTTP `GET/POST /tun` |

The normal `/register`, `/cmd`, and `/result` beacon remains the lifecycle
control path. The PowerShell plugin runs in its own runspace. The Go plugin
runs in a cancellable child process. Stopping the operator relay clears the
server state; the plugin observes the inactive poll response and closes its
TCP connections.

## Go plugin input

The Go agent passes the plugin this JSON object over the child stdin frame:

```json
{
  "base_url": "https://rmm.example",
  "beacon_secret": "beacon-secret",
  "session_id": "session-id",
  "http_proxy": ""
}
```

The plugin keeps these values in memory. It does not write them to disk or
include them in output. The plugin export is the persistent `Run` ABI defined
in `docs/go-agent-plugins.md`.

## PowerShell plugin host functions

`agent-powershell/client_rmm.ps1` keeps only the host lifecycle functions:

| Function | Role |
| --- | --- |
| `Get-RmmPluginScript` | Fetch a validated `.ps1` plugin over the beacon channel |
| `Ensure-RmmSocksPlugin` | Load the checked-in SOCKS plugin once and verify its entrypoints |
| `Sync-RmmSocksChannelFromServer` | Start or stop the plugin from the `/cmd` lifecycle flag |
| `Stop-RmmSocksChannelWorker` | Stop the active plugin worker during relay or session shutdown |

The fetched plugin owns the relay functions:
`Invoke-RmmSocksHttpRelay`, `Invoke-RmmSocksCycle`,
`Connect-RmmSocksTcpClient`, `Read-RmmSocksTcpChunk`,
`Submit-RmmSocksResponses`, `Flush-RmmSocksPendingResponses`,
`Close-RmmSocksConnection`,
`Start-RmmSocksChannelPlugin`, and `Stop-RmmSocksChannelPlugin`. It imports the
existing authenticated HTTP helpers into the worker runspace so proxy and
tunnel routing behavior stays consistent with the main beacon.

The remaining PowerShell plugin functions support those entrypoints:
`Get-RmmSocksPollActive`, `Get-RmmSocksTasksFromPoll`,
`Get-RmmSocksWorkerFunctionNames`, `Import-RmmFunctionsIntoRunspace`,
`Install-RmmSocksRunspaceHostLog`, `New-RmmSocksRunspace`, and
`Set-RmmSocksRunspaceVariables`.

Both Go plugins export `Run`. Their implementation functions are
`validateConfig`, `runRelay`, `runRelayWithClient`, `newHTTPClient`, `getPoll`,
`processTasks`, `processUDPTasks`, `readConnectionData`, `readUDPData`,
`snapshotConnections`, `snapshotUDPConnections`, `closeConnection`,
`closeUDPConnection`, `writeAll`, `closeConnections`, `closeUDPConnections`,
`postResponses`, `request`, `isPermanentHTTPError`, and `writeOutput`; together
they validate the child input, poll their separate server endpoint, relay TCP
and UDP data, and return the plugin status through the PE ABI. Their source
packages are maintained in the external plugin repository.

The pivot task protocol uses `connect`, `send`, and `close` for TCP plus
`udp_send` and `udp_close` for datagrams. UDP sends are delivered once and
bounded per poll; UDP close tasks are retried until the plugin returns
`udp_closed`.

## RMM-owned TUN relay

The TUN frontend uses the dedicated Go `tun` plugin as its authenticated
transport. In server mode, the server creates the TUN device and runs the
user-space IP stack. In workspace mode, the workspace service creates the
device and runs the stack locally while the central server keeps only the
agent-side transport. The target-side Go plugin opens ordinary TCP and UDP
sockets without creating a device or requiring administrator privileges. It is
controlled through `GET /api/v1/tun` and `POST /api/v1/sessions/{id}/tun`, with
matching CLI and MCP tools. See `docs/tun-pivot.md` and
`docs/workspace-deployment.md` for setup, routing, and current protocol limits.

SOCKS and TUN are separate Go plugin packages, DLLs, lifecycle flags, child
processes, and beacon endpoints. The TUN plugin reuses the same task framing
and relay behavior, but it is not loaded when only the public SOCKS pivot is
active.

## Operator setup

Install the matching artifacts from the external plugin repository before
starting a pivot for a Windows amd64 Go session:

```text
plugin-store/socks.ps1
plugin-store/socks.dll
plugin-store/tun.dll
```

The default local runtime store is `RMM_logs/plugins/`; Docker deployments
mount the persistent `/app/plugins` volume. A public SOCKS relay requires the Go
`socks-plugin` capability and `socks.dll`; a TUN relay requires `tun-plugin`
and `tun.dll`. Existing `/socks` REST, CLI, MCP, and WebUI controls remain
stable; the WebUI exposes TUN routing in the Pivots tab of the agent tools
dialog. SOCKS5 and TUN use separate plugin controllers and can run together.

## Security and logging

Plugin downloads require the beacon secret and a registered session ID. The
server logs plugin delivery metadata and operator pivot actions. Agent-side
plugin input, output, URLs, and credentials are not written to the target log.
Use these plugins only on systems you own or are explicitly authorized to
manage.

## Workspace data plane

When `RMM_STORAGE_MODE=workspace`, the central server starts a transport-only
bridge for each active SOCKS or TUN relay. It does not bind the operator SOCKS
port or create the TUN device. `workspace_service.py` starts those local
components and exchanges task batches through the authenticated workspace
endpoints. If the central handoff is temporarily unavailable, local pending
tasks are requeued; the workspace service restores local listeners after a
restart when the central relay is still active.

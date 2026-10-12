# Pivot plugins

SOCKS5 is the first pivot type exposed by the WebUI. The server keeps the
operator-side listener in `rmm_socks.py`; the agent-side relay is loaded only
while the server reports `socks_active: true`.

## Plugin packages

| Agent | Plugin | Delivery | Relay path |
| --- | --- | --- | --- |
| Windows PowerShell | `agent-plugins/socks-powershell/socks.ps1` | Authenticated source fetch from `/tools/plugins/socks.ps1` | HTTP `GET/POST /socks` |
| Windows amd64 Go | `agent-plugins/socks` → `socks.dll` | Cross-built PE plugin from `/tools/plugins/socks.dll` | HTTP `GET/POST /socks` |

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

The Go plugin exports `Run`. Its implementation functions are
`validateConfig`, `runRelay`, `runRelayWithClient`, `newHTTPClient`, `getPoll`, `processTasks`,
`readConnectionData`, `snapshotConnections`, `closeConnection`, `writeAll`,
`closeConnections`, `postResponses`, `request`, `isPermanentHTTPError`, and
`writeOutput`; together they validate the child input, poll the server, relay
TCP data, and return the plugin status through the PE ABI.

## Operator setup

PowerShell pivot plugins are served from the repository automatically. Before
starting a pivot for a Windows amd64 Go session, build the Go plugin:

```text
POST /api/v1/plugins/build
{"name":"socks"}
```

The default output is `RMM_logs/plugins/socks.dll`. The server refuses to start
a pivot for a Go session that does not advertise `socks-plugin` or lacks this
artifact. Existing `/socks` REST, CLI, MCP, and WebUI controls remain stable;
the WebUI labels the group **Pivots** so more pivot types can be added later.

## Security and logging

Plugin downloads require the beacon secret and a registered session ID. The
server logs plugin delivery metadata and operator pivot actions. Agent-side
plugin input, output, URLs, and credentials are not written to the target log.
Use these plugins only on systems you own or are explicitly authorized to
manage.

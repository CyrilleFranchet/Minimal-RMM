# RMM-owned TUN pivot

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the control-plane, workspace,
and agent boundaries.

In the default server storage mode, the TUN device belongs to the RMM server.
In workspace mode, the central server keeps the authenticated agent transport
while the per-operator workspace service creates and owns the TUN device. The
agent remains an ordinary user-mode Go process and opens target-side TCP and
UDP sockets through a dedicated authenticated TUN channel.

```text
Exegol tool
    │ native connect()/UDP socket
    ▼
Server or workspace TUN device
    │
    ▼
rmm-tun (user-space IP/TCP/UDP stack)
    │ SOCKS5 TCP CONNECT / UDP ASSOCIATE
    ▼
RMM per-session private relay bridge
    │ authenticated HTTP /tun poll
    ▼
tun.dll in its own Go child process
    │ ordinary target-side sockets
    ▼
Target network
```

The RMM code owns the lifecycle, API, route setup, private relay transport, and
agent integration. `server-tun/` builds on the maintained
[`tun2socks`](https://github.com/xjasonlyu/tun2socks) gVisor components instead
of implementing a TCP/IP stack from scratch. It uses a custom TCP forwarder so
the agent-side SOCKS `CONNECT` completes before the local TUN client receives a
TCP SYN-ACK. No external proxy or console is required.

## Requirements

- The process that owns the TUN device must run on Linux or macOS and have
  permission to create and configure a TUN device and routes.
- On Linux, `/dev/net/tun` must be available and the owning process needs the
  permission to create network interfaces. A workspace container needs
  `/dev/net/tun` plus `CAP_NET_ADMIN`; the central control-plane container does
  not.
- On macOS, use an explicit `utun<N>` device name such as `utun4`. Linux names
  such as `rmm0` are not valid for the macOS helper.
- The server needs Go 1.26.3 or newer to build `rmm-tun`.
- A Windows amd64 Go agent must advertise `tun-plugin` and the server must
  have a built `tun.dll`. The current TUN lifecycle intentionally rejects
  other agent types until their transport capabilities are implemented.
- In server mode, the Exegol container must share the network namespace that
  owns the server TUN device, or the route must be added in the namespace where
  the tools run.
- In workspace mode, the workspace service and the Exegol tools must share the
  workspace network namespace.

The target agent does not need administrator rights for this feature. It does
not create an interface, add routes, or open a listening socket on the target.

## Build

Build the server helper from the Minimal-RMM repository:

```bash
make tun-build
```

Build `tun.dll` in the external plugin repository and install it in the
runtime plugin store. `make tun-build` writes `tools/rmm-tun`; set
`RMM_TUN_HELPER` if the helper is stored elsewhere.

## WebUI workflow

The WebUI exposes TUN routing in the **Pivots** tab of the main workspace after
selecting a live session. Before opening it, install `tun.dll` from the
external plugin repository and build the helper with `make tun-build`. In server mode, the helper and
interface run in the RMM server namespace. In workspace mode, they run in the
local workspace namespace. In the TUN section, configure:

- the TUN device, such as `tun://rmm0` on Linux or `utun4` on macOS, in the
  namespace where the workspace or server service runs;
- the narrow target routes, one IPv4 CIDR per line, such as
  `10.10.0.0/16` and `10.20.0.0/16`;
- the TUN address, egress interface, MTU, and UDP idle timeout.

Click **Start TUN routing** to create the device, configure the address and
routes, and start the server helper. The tab shows active relays, helper
health, beacon state, and the routes in use. Click **Stop TUN routing** to
remove the routes, stop the helper, and release the device. The TUN controls
use `GET /api/v1/tun` and `POST /api/v1/sessions/{id}/tun`; the target agent
only provides the unprivileged TCP and UDP transport.

## Start a relay

Choose a TUN device name that is free in the owning namespace and route only
the target network needed for the operation. The service refuses to reuse an
existing Linux interface so it does not overwrite another tunnel's address. Linux
example:

```bash
python rmm_cli.py tun start SESSION tun://rmm0 \
  --route 10.10.0.0/16 --route 10.20.0.0/16 --interface eth0
```

macOS example:

```bash
python rmm_cli.py tun start SESSION utun4 \
  --route 10.10.0.0/16 --route 10.20.0.0/16 --interface en0
```

The default address assigned to the TUN device is `198.18.0.1/15`. It is a
private test range and is separate from the target route. Override it with
`--address` only when it does not overlap with the Exegol or target networks.
The default MTU is 1500 and the default UDP idle timeout is 300 seconds.

The owning process starts a private SOCKS listener on an ephemeral loopback
port, starts `rmm-tun`, configures the interface, and then reports the relay as
active. `tun list` shows the helper state and configured routes:

```bash
python rmm_cli.py tun list
python rmm_cli.py tun stop SESSION
```

The corresponding REST calls are:

```http
GET /api/v1/tun
POST /api/v1/sessions/{id}/tun
Content-Type: application/json

{"device":"tun://rmm0","routes":["10.10.0.0/16","10.20.0.0/16"],"interface":"eth0"}
```

The legacy single-route form `{"route":"10.10.0.0/16"}` remains accepted.
Stop with `{"stop":true}`. The same lifecycle is exposed as MCP tools
`list_tun`, `start_tun`, and `stop_tun`; pass multiple CIDRs in `routes`.

## Using Exegol

After the relay starts, verify that the routes are present in the namespace
that runs the tools. In workspace mode, run these checks inside the workspace
container:

```bash
ip addr show rmm0
ip route get 10.10.20.15
```

Tools that use normal TCP or UDP sockets can then use the target network
without proxy configuration. Examples include `curl`, `ssh`, `smbclient`,
Impacket protocols that use TCP, and TCP port scans. For TCP, the helper waits
for the agent-side `CONNECT` result before it completes the local handshake, so
closed target ports are reset instead of being acknowledged as open by the
local stack. Scan timing includes the beacon and relay latency; a scanner with
a shorter timeout can report a reachable port as filtered. Keep the route
narrow so unrelated traffic does not enter the pivot.

The transport still carries TCP and UDP socket flows rather than target raw
packets. ICMP echo requests, `ping`, and tools that need target-side raw packet
semantics remain unsupported. UDP protocols are supported through the SOCKS5
UDP association, subject to the protocol's socket behavior and the agent's
beacon latency.

## Lifecycle and failure handling

- `tun start` requires the Go agent's `tun-plugin` capability, the built
  `tun.dll`, and an executable `rmm-tun` helper in the namespace that owns the
  TUN device.
- The server refuses a second TUN relay for the same session until the first
  one is stopped. A normal SOCKS listener and a TUN relay are independent and
  can run at the same time for one session.
- Stopping the TUN relay removes the configured routes, stops the helper, and
  stops the private SOCKS bridge. Killing the session performs the same
  cleanup.
- The server logs relay start, stop, and setup failures. The target agent log
  does not receive TUN configuration, plugin input, plugin output, URLs, or
  credentials.
- The private SOCKS bridge keeps a successful CONNECT pending until the local
  helper receives its SOCKS success reply. This preserves accurate open-port
  results when the target closes immediately after accepting a connection.

## Function reference

The server-side implementation is split across these functions and classes:

| Symbol | File | Role |
|--------|------|------|
| `normalize_tun_config` | `rmm_tun.py` | Validate device, routes, address, MTU, and UDP timeout values |
| `_device_name` / `_run_system_command` | `rmm_tun.py` | Parse safe device names and execute fixed network command argument lists |
| `ServerTunRelay` | `rmm_tun.py` | Own one helper process, interface, routes, and private relay bridge |
| `ServerTunRelay.start` / `stop` | `rmm_tun.py` | Configure and tear down one relay |
| `ServerTunRelay._read_helper_log` | `rmm_tun.py` | Forward helper diagnostics to the server log |
| `ServerTunRelay.alive` / `status` | `rmm_tun.py` | Report helper health and safe operator state |
| `ServerTunRelay._prepare_linux_device` / `_configure_interface` / `_remove_route` | `rmm_tun.py` | Apply platform-specific TUN and route operations |
| `TunManager` | `rmm_tun.py` | Thread-safe registry for active relays |
| `TunManager.start` / `stop` / `stop_all` / `get` / `list_status` | `rmm_tun.py` | Manage relay instances and inventory |
| `RMMServer.start_tun` / `stop_tun` | `server_rmm.py` | Operator lifecycle and event logging |
| `RMMServer.tun_plugin_error` | `server_rmm.py` | Check Go transport and helper readiness |
| `RMMServer.list_tun_status` | `server_rmm.py` | Return operator-visible relay state |
| `tunRelayRoutes` / `refreshTunRelays` / `startSelectedTun` / `stopTunRelay` | `web/app.js` | WebUI route display, inventory, and TUN lifecycle controls |
| `tunConfig` / `validateConfig` | `server-tun/main.go` | Validate helper flags before starting the stack |
| `runStack` | `server-tun/main.go` | Open the TUN device, configure the stack, and wait for shutdown |
| `parseTUNDevice` / `parseSocks5Proxy` | `server-tun/main.go` | Parse the helper's TUN and private SOCKS endpoints |
| `createStack` / `installICMPHandler` | `server-tun/main.go` | Build the gVisor stack and install transport handlers before the NIC starts |
| `relayHandler` | `server-tun/main.go` | Bridge gVisor TCP and UDP flows to the private SOCKS proxy |
| `relayHandler.handleTCPRequest` | `server-tun/main.go` | Perform proxy `CONNECT` before the local TCP handshake |
| `relayHandler.handleUDPRequest` / `handleUDPConn` | `server-tun/main.go` | Create and relay one SOCKS5 UDP association |
| `metadataFromID` / `parseStackAddress` | `server-tun/main.go` | Convert gVisor endpoint IDs into proxy metadata |
| `pipeTCP` / `copyTCPDirection` | `server-tun/main.go` | Copy TCP bytes with half-close handling |
| `pipeUDP` / `copyUDPDirection` | `server-tun/main.go` | Copy UDP datagrams with idle expiry |
| `symmetricNATPacketConn` / `newSymmetricNATPacketConn` | `server-tun/main.go` | Restrict UDP replies to the requested destination |
| `icmpForwarder.handlePacket` / `handleIPv4` | `server-tun/main.go` | Answer local IPv4 echo requests without forwarding raw ICMP |
| `main` / `validateConfig` | `server-tun/main.go` | Parse flags, validate input, and start the helper |

The agent-side UDP additions are documented in
`docs/go-agent-plugins.md`; the external `tun.dll` package uses
`processUDPTasks`, `readUDPData`, and the UDP connection cleanup helpers for
the TUN transport. The SOCKS plugin has the same task protocol in its own
binary.

The Go beacon keeps the lifecycle in `pollOnce` and uses
`isPermanentHTTPError` in `agent-go/main.go` to stop a stale pivot child after
an authentication or session failure.

The server SOCKS additions are in `rmm_socks.py`: `_socks5_read_command`,
`_handle_udp_associate`, `_udp_loop`, `_expire_udp_tunnels`,
`_send_udp_datagram`, `_decode_socks5_udp`, and `_encode_socks5_udp` implement
the UDP association and packet framing used by the helper.

## Validation

Run the focused checks before trying a real interface:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/minimal-rmm-pycache \
  python3 -m unittest tests.test_rmm_tun tests.test_rmm_socks tests.test_server_rmm_plugins
GOCACHE=/private/tmp/minimal-rmm-go-cache \
  sh -c 'cd server-tun && go test ./...'
```

An end-to-end test must run on the server host with TUN permissions and a live
Go agent. From Exegol, confirm the route and then use a TCP client against a
known target service before testing larger tools.

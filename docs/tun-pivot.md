# RMM-owned TUN pivot

See the [documentation wiki](README.md) for navigation and the
[architecture overview](architecture.md) for the control-plane, workspace,
and agent boundaries.

The central server keeps the authenticated agent transport
while the per-operator workspace service creates and owns the TUN device. The
agent remains an ordinary user-mode Go process and opens target-side TCP and
UDP sockets through a dedicated authenticated TUN channel.

```text
Exegol tool
    │ native connect()/UDP socket
    ▼
Workspace TUN device
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
- The workspace service and the Exegol tools must share the
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
external plugin repository and build the helper with `make tun-build`. The
helper and interface run in the local workspace namespace. In the TUN section, configure:

- the TUN device, such as `tun://rmm0` on Linux or `utun4` on macOS, in the
  namespace where the workspace service runs;
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
The helper starts with informational flow logging. Set `RMM_TUN_LOG_LEVEL=debug`
before starting the server for additional diagnostics, or use `warn` to reduce
the output after the relay is working.

The owning process starts a private SOCKS listener on an ephemeral loopback
port, starts `rmm-tun`, configures the interface, and then reports the relay as
active. `tun list` shows the helper state and configured routes:

```bash
python rmm_cli.py tun list
python rmm_cli.py tun stop SESSION
```

The private listener uses the same full-duplex TCP relay as ordinary SOCKS5.
Its local reader does not hold the response-writer lock during a blocking
receive, so LDAP replies can be delivered while the client waits for them.
See [SOCKS relay behavior and checks](web-socks.md#agent-plugin-lifecycle).

The external `tun.dll` completes target TCP CONNECT attempts independently. A
filtered port can remain in flight without delaying the response for a
successful port from the same poll batch. Replace the plugin artifact and
restart the active TUN relay when deploying this behavior. Each target-side TCP
CONNECT has a five-second timeout; a timeout produces no success response and
leaves the port filtered.

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
local stack. Use a TCP connect scan such as `nmap -sT`; raw SYN scans depend on
packet timing and are not a reliable validation of this socket-based transport.
Scan timing includes the beacon and relay latency; a scanner with a shorter
timeout can report a reachable port as filtered. Keep the route narrow so
unrelated traffic does not enter the pivot.

The transport still carries TCP and UDP socket flows rather than target raw
packets. ICMP echo requests, `ping`, and tools that need target-side raw packet
semantics remain unsupported. UDP protocols are supported through the SOCKS5
UDP association, subject to the protocol's socket behavior and the agent's
beacon latency.

## Scan load and connection cleanup

The central broker deduplicates retained `connect`, `close`, and `udp_close`
controls by connection ID and operation. Repeated workspace syncs do not grow
those queues while a target connection is pending; TCP payload sends remain
ordered and are never deduplicated.

The bridge tracks live CONNECT ownership from enqueue through cleanup. A
success without an active request or tunnel is closed, even after a prolonged
outage. `_cancel_connect` removes timed-out work and requests remote closure.
The workspace applies agent acknowledgments before resending controls; the
central broker also suppresses controls whose acknowledgment is still queued.
Acknowledgment storage and task removal happen under the same lock.
When a workspace handoff fails, the local bridge restores every drained TCP
and UDP payload in order. Only retained controls are deduplicated; repeated
equal-valued payloads remain distinct tasks.

The external TUN plugin uses independent, ordered TCP writers. `queueWrite`
creates one `tcpWriter` with a 32-payload FIFO per writing flow; each payload
write has a five-second deadline. Ordinary close requests drain accepted bytes
before closing. Overflow, write failure, and shutdown abort that flow.
`closeFlow` cancels its worker, and `collectWrites` applies completion on the
registry-owning relay loop. A blocked target does not hold up other flows.
Cancelled CONNECT attempts keep their pending identity until their result is
discarded, preventing a duplicate attempt while cancellation is outstanding.

`readConnectionsData` reads TCP sockets concurrently using private worker state,
then applies removals on the relay loop. Idle sockets share their read wait.
UDP reads retain their existing behavior. `flushResponses` splits ordered
responses into approximately 512 KiB HTTP bodies and returns only the unsent
suffix on failure. A 413 response triggers smaller batches; an unsplittable
single-entry 413 stops the plugin rather than retrying forever. HTTP response
loss can still produce a replay because the transport has no byte-level ACK.
`uploadResponseBatches` owns those ordered POSTs on a background worker, so a
slow response upload does not prevent the relay loop from polling new tasks.
Its 32-batch input queue bounds queued work and deliberately applies
backpressure if an outage lasts long enough to fill it. Context-aware polling
and posting use `getPollContext`, `flushResponsesContext`,
`postResponsesContext`, and `requestContext` to cancel an in-flight request
when the relay stops. While a data batch is queued, the relay pauses target
socket reads but continues polling and processing agent tasks; TCP socket
backpressure therefore preserves queue capacity for connection acknowledgments.

The stack helper releases each pending-handshake slot immediately after the
local handshake completes, allowing established streams to stay open without
consuming the 2,048-request forwarder limit.

Deploy all parts: restart the updated central and workspace services, rebuild
and install both `tun.dll` and the `rmm-tun` helper, then stop and restart existing
TUN relays. Updating source alone does not replace running binaries. Host tests
cover queue retries, late results, concurrent reads, ordered writes, overflow,
shutdown, HTTP response splitting, and handshake-slot reuse. A real Windows
agent and privileged TUN scan remain necessary for end-to-end validation.

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

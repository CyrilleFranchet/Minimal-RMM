---
id: exegol-rmm-pivot
title: Exegol tools and RMM pivots
description: Route authorized security tasks between Minimal-RMM agents and Exegol MCP; choose SOCKS5 or TUN when Exegol must reach an agent network.
default: false
---

# Exegol tools and RMM pivots

Use this skill for authorized lab work that combines a Minimal-RMM Windows
agent with Exegol MCP tools. Keep the two execution environments separate:

- **Minimal-RMM tools** control the Windows agent, its beacon session, agent
  files, screenshots, and RMM-owned pivots.
- **Exegol MCP tools** run security tools and Linux commands inside an Exegol
  container, or manage Exegol containers and images.

Only use a tool when it is present in the live MCP tool list. Exegol MCP tool
names and input schemas can change between versions; inspect the available
tool description instead of inventing arguments. If Exegol MCP is not
connected, say so and use the Minimal-RMM tools that can complete the task.

## Choose the right tool surface

| Task | Prefer | Reason |
|------|--------|--------|
| Find or identify a managed host | RMM `list_sessions`, then `get_session` | The RMM session is the authoritative agent identity and capability record. |
| Run a command under the Windows agent token | RMM `exec_command` or `queue_command` | This preserves the beacon session, transcript, and agent context. Follow `agent-command-dispatch` for `PS:` and `cmd:` prefixes. |
| Download, upload, exfiltrate, screenshot, or inspect RMM events | RMM operator tools | These operations have RMM-specific queues, artifacts, and audit events. |
| List, start, or stop Exegol containers and manage images | Exegol `list_exegol_containers`, `start_container`, `stop_container`, `list_installed_images`, `list_all_images`, or `download_image` | These actions belong to Exegol orchestration. Do not use a Windows agent command to manage the operator container. |
| Run `nmap`, NetExec, Impacket, web tools, scripts, or other Linux security tooling | Exegol `execute_command_in_container` or the matching specialized Exegol tool | The tool must execute in the Exegol environment, not on the managed Windows agent. |
| Use a direct SSH, WinRM, SMB, MSSQL, WMI, or RDP workflow | Exegol `execute_remote_command` when the requested protocol and credentials are available | This is a direct protocol workflow. It is not a substitute for RMM beacon control. |
| Inspect what an Exegol image provides | Exegol `list_installed_tools` or `list_installed_exegol_resources` | Discover available tools and resources before guessing a binary or resource name. |

Do not send `nmap`, `proxychains`, or other Exegol commands through RMM
`exec_command`: that tool runs on the Windows agent. Do not use Exegol's
direct remote-command tool merely to replace an RMM command on a host already
managed by the beacon.

## When a pivot is required

An RMM command does not require SOCKS or TUN: the authenticated beacon already
connects the operator API to the Windows agent. A pivot is required when a
tool running in Exegol must open a connection to the agent host or to another
network reachable from that host, and Exegol has no route to that network.

Before starting a pivot:

Establish or reuse the selected agent's local baseline as described in
`local-recon-before-network` before a discovery task. Verify identity, network
configuration, and domain context before choosing targets or routes.

1. Use RMM `list_sessions` and `get_session` to select the exact live agent.
2. Check whether the target is already reachable from the Exegol namespace.
   Do not create a pivot just because the agent is selected.
3. Check `list_socks` and `list_tun` so an existing relay is reused when it
   matches the selected session.
4. Keep the target scope and routes supplied by the operator. Do not broaden a
   route to `0.0.0.0/0` or scan unrelated networks.

Use the smallest transport that supports the requested Exegol tool:

| Need | Prefer | Why |
|------|--------|-----|
| One or a few TCP connections and the tool supports a SOCKS proxy | SOCKS5 | It needs no route changes and is the lightest pivot. |
| Several native TCP tools, UDP, or a tool with no proxy setting | TUN | Exegol tools use ordinary sockets and the configured routes. |
| Raw SYN, ICMP, or other raw-packet semantics | Neither current pivot provides this | TUN carries TCP and UDP socket flows; use TCP connect checks such as `nmap -sT`, and report that `ping` and raw SYN validation are unsupported or timing-sensitive. |

SOCKS5 and TUN are independent. Start only the one needed first; both can be
active for the same session when the task genuinely needs both.

## SOCKS5 workflow

1. Call RMM `start_socks` for the selected `session_ref`, normally on the
   default loopback bind and port unless the operator supplied different
   values.
2. Call `list_socks` and use the returned `socks_url`, host, and port. Do not
   assume that `127.0.0.1:1080` is reachable from every process.
3. Run the Exegol command through the proxy using the tool's documented proxy
   option or a proxy-aware wrapper such as `proxychains` when appropriate.
   Prefer a tool-native proxy option when one exists.
4. Verify one intended target connection from Exegol before doing a larger
   operation. A successful relay start alone does not prove target reachability.
5. Call RMM `stop_socks` when the operation is complete unless the operator
   explicitly wants the relay to remain active.

SOCKS is visible only in the namespace where the listener is bound. In
workspace mode the listener normally belongs to the operator's Exegol
workspace, so `socks5://127.0.0.1:1080` is local to that workspace. In server
mode, Exegol tools must run in the namespace that owns the server listener, or
the operator must deliberately provide a reachable bind address. Do not expose
an unauthenticated SOCKS listener broadly just to fix a namespace mismatch.

SOCKS readiness depends on the agent plugin. PowerShell agents need the
deployed `socks.ps1`; Go agents need the advertised `socks-plugin` capability
and the installed `socks.dll`. If `start_socks` reports a plugin or capability
error, report that error and fix deployment before retrying.

## TUN workflow

Use TUN when the Exegol tool needs normal routed sockets, UDP, or cannot use a
proxy configuration.

1. Confirm the selected session is a Go Windows amd64 agent with the
   `tun-plugin` capability and that `tun.dll` is installed. TUN is not
   currently available through the PowerShell agent.
2. Call RMM `start_tun` with a free device and only the target IPv4 CIDR routes
   needed for the task. On Linux use a device such as `tun://rmm0`; on macOS
   use an explicit `utun<N>` device. Keep the default TUN address unless it
   overlaps the operator or target network.
3. Call `list_tun` and confirm the relay is active, then verify the route from
   inside the same Exegol namespace with the Exegol in-container command tool,
   for example `ip addr` and `ip route get <target>`.
4. Run the requested Exegol tool with its normal target address; no proxy
   variable is needed when the route is correct.
5. Account for beacon and relay latency. Use connect-based validation and
   realistic timeouts rather than treating a short scanner timeout as proof
   that a target is filtered.
6. Call RMM `stop_tun` when the operation is complete unless the operator asks
   to keep it active.

TUN ownership follows the deployment mode. In server mode, the Exegol process
and the server-side TUN device must share a network namespace, or the route
must be installed in the namespace where the Exegol tools run. In workspace
mode, the workspace service creates the local device and routes; the workspace
needs Linux `/dev/net/tun`, `CAP_NET_ADMIN`, the `rmm-tun` helper, and a shared
namespace with the tools. The central control-plane container does not own the
workspace TUN device.

## Reporting

Keep Exegol output, agent output, and relay status distinct. Include exact
target scope and transport. For failures, distinguish missing capability,
plugin/helper, route, wrong namespace, and target refusal. A relay start
response alone does not prove reachability. Report the resulting evidence or
specific blocking prerequisite after cleaning up temporary pivots.

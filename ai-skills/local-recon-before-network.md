---
id: local-recon-before-network
title: Local reconnaissance before network discovery
description: Establish the selected agent's identity, token, OS, network configuration, and domain context before network discovery or AD collection, then hunt scoped local secrets to progress faster, with compiled tools such as Seatbelt as a last resort.
default: true
---

# Local reconnaissance before network discovery

## Scope and order

Before network discovery, domain enumeration, AD collection, or a pivot for
those tasks, establish a local baseline on the selected Windows agent. Then
check relevant local secret sources within the authorized task scope before
broader discovery. This does not trigger recon for explanations or artifact
review, or authorize scanning every discovered subnet.

Reuse recent verified evidence from the same session. Refresh after a host,
identity, reboot, or relevant network change. Record unavailable facts as
unknown; resolve only those that block choosing the target, identity, or route.

## Local baseline

Select the exact agent with RMM `list_sessions` / `get_session`. Registration
metadata is context, not verified identity. Run small, inspectable commands via
RMM `exec_command` / `queue_command`; Exegol output describes the container.
Use `PS:` for PowerShell as specified by `agent-command-dispatch`.

| Evidence | Local query | Interpretation |
|----------|-------------|----------------|
| Identity, SID, token groups, privileges, integrity | `whoami /all` (or narrower `/user`, `/groups`, `/priv`) | Service/SYSTEM may differ from the desktop user; deny-only groups do not prove elevation. |
| Host and domain membership | `PS: Get-CimInstance Win32_ComputerSystem -ErrorAction Stop \| Select-Object Name,Domain,PartOfDomain,DomainRole` | Account domain, computer domain, and DNS suffix are distinct. |
| OS/build | `PS: Get-CimInstance Win32_OperatingSystem -ErrorAction Stop \| Select-Object Caption,Version,BuildNumber,OSArchitecture,LastBootUpTime` | Process architecture may differ from OS architecture. |
| Interfaces, DNS, routes | `ipconfig /all`, then `route print` | Include relevant NICs, VPNs, IPv6, and absent default routes. |
| Process and filesystem context | The command below | Resolve paths under the agent token. |

```text
PS: [PSCustomObject]@{Is64BitProcess=[Environment]::Is64BitProcess; Profile=[Environment]::GetFolderPath('UserProfile'); Temp=[IO.Path]::GetTempPath(); WorkingDirectory=(Get-Location).Path; UserDomain=$env:USERDOMAIN; UserDnsDomain=$env:USERDNSDOMAIN; LogonServer=$env:LOGONSERVER}
```

Retain sources and observation times. Use local CIM without remote parameters.
Do not modify adapters/routes or contact a public-IP service for this baseline.
Use structured `Get-Net*` queries only when existing output is insufficient.
`LOGONSERVER` can be absent or stale; neither it, a gateway, nor a DNS server
proves a currently reachable DC. Avoid full environment dumps.

## Scoped local secret checks

Use built-in queries first and search only locations relevant to the task.
Inspect metadata and paths before content; do not dump drives or unrelated
user documents. Report location, type, and associated account/system, never raw
passwords, keys, or tokens in tool output or the stored chat transcript. Finding
material does not authorize using it against another system.

| Source | Inspect within scope |
|--------|----------------------|
| Scheduled tasks and services | Run-as accounts and referenced scripts/configuration |
| Application directories | Connection strings and secret fields in app/web configuration |
| Panther/sysprep files | Unattend answer files |
| Cached Group Policy preferences | XML files containing `cpassword` |
| Winlogon autologon configuration | Account and presence of password settings, without returning values |
| Command histories and client configuration | Relevant profile paths, saved credentials, or keys |

Resolve profile locations with `windows-user-profile-path`. Return paths and
line numbers for content matches, not matching lines. Adapt search roots to the
application or operator-specified location. Keep sensitive values out of output
by projecting metadata or presence indicators before returning results.

Compiled collectors such as Seatbelt are a last resort when explicitly
requested for a broad sweep or built-in queries cannot answer the question.
Deployment must be authorized: use the approved RMM upload/plugin path, not an
internet download on the agent. Scope command groups and report executed
checks. If deployment is unavailable or unauthorized, report the gap.

## Network handoff

Live DC discovery requires a separate, scoped DNS/directory step when needed.
`nltest /dsgetdc`, `Get-ADDomain`, and BloodHound are not purely local evidence.
Use the selected directory workflow: normally `windows-ad-recon-stealth`, with
explicitly selected `ad-collection-analysis` governing graph collection.

Caches and existing connections may suggest peers when relevant, but do not
prove current reachability or authorize probing them. Before network work,
summarize identity/token limits, host/OS, relevant addressing/routes/DNS,
membership, DC hints, scoped findings, and unknowns. Identify exact next targets
and execution environment. Apply `exegol-rmm-pivot` when routing is required;
verify connectivity from that environment and reuse suitable relays. Do not
start SOCKS/TUN merely because an agent has a connected subnet.

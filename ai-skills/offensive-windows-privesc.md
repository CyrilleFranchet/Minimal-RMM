---
id: offensive-windows-privesc
title: Windows privilege-escalation investigation
description: Investigate a Windows target's local escalation opportunities from its actual token, service/task permissions, and execution prerequisites. Use for local privilege questions, not AD graph collection or ordinary identity checks.
default: false
---

# Windows privilege-escalation investigation

Adapted from [Claude-Red offensive-windows-privesc](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/privesc/offensive-windows-privesc/SKILL.md).
Retain its token/service/task/DLL investigation categories; validate findings
against the actual host instead of treating its example exploit commands or
version ranges as current proof. `operator-boundaries` governs mutations.

## Bind the investigation to a target

Resolve the full RMM session ID and confirm hostname, current account, integrity
level, process architecture, and OS build on that target. Execute local checks
through that session; an Exegol MCP command otherwise inspects Exegol, not the
Windows host. Use Exegol for offline analysis and explicitly selected network
access. Load `agent-command-dispatch` for queued-result correlation when needed.

Start with built-in read-only observations, for example:

```powershell
whoami /all
Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber
Get-CimInstance Win32_Service | Select-Object Name,StartName,State,PathName
```

Interpret the actual token: deny-only groups, disabled privileges, and integrity
level matter. Administrator membership is not an elevated token; SYSTEM is not
Domain Admin. If the target is already at the requested privilege, report that
fact instead of manufacturing an escalation. Use existing enumeration tools
only for a remaining evidence gap; do not download or install a scanner.

## Investigate evidence-supported branches

| Candidate | Read-only evidence to establish | Do not conclude from this alone |
|-----------|---------------------------------|---------------------------------|
| Service configuration control | Exact service DACL, effective caller rights, run account, start/stop rights, and trigger availability. | A writable service binary and permission to change service configuration are different conditions. |
| Writable service or task action | Canonical executable/script path, argument parsing, file and parent-directory ACLs, privileged run identity, and next trigger. | A write ACE for a group name is not a complete effective-access calculation. |
| Unquoted service path | Actual executable search candidates before the intended binary, permissions on those candidates, service identity, and trigger. | Spaces without quotes are not sufficient; writable locations after the selected executable do not prove interception. |
| Token impersonation | Current privileges, token type/state, OS/build, reachable privileged authentication mechanism, installed compatible tooling. | SeImpersonate or SeAssignPrimaryToken presence is a prerequisite, not guaranteed SYSTEM execution. |
| DLL/search-path opportunity | Privileged loader context, actual requested DLL, resolution order under its environment, and writable selected location. | A writable PATH directory or a missing DLL reported by a scanner does not prove a privileged process will load it. |
| Scheduled task or autorun | Task/action principal, trigger, working directory, effective permissions, and who performs the next logon/run. | A user autorun normally runs as that user; creating a task may already require the privilege being sought. |
| Installer policy | Both relevant machine and current-user policy values and effective policy. | Report the condition; MSI installation is outside this project's no-installation boundary. |
| Vulnerable component | Exact product/build, vendor patch evidence, configuration, and reachable prerequisite. | A scanner title, missing single KB, or a historical exploit's OS list proves applicability. |

For a selected service use targeted `sc.exe qc <service>` and
`sc.exe sdshow <service>`; use `icacls <exact-path>` or `Get-Acl` for relevant
files. Inspect the real task definition with built-in task queries. Record
access-denied results as visibility limits rather than absence of the condition.
Do not replace a failed read-only check with a write probe against an existing
service binary, task, registry key, or file.

## Rank and validate a path

For each candidate record: current principal → controllable object → privileged
consumer → trigger → intended privilege. Name missing links explicitly. Rank by
evidence completeness, reliability, reversibility, and the demo's desired
observable behavior. Prefer a fully evidenced configuration issue to guessing
an exploit from an OS version. A UAC elevation question is distinct from a
standard-user-to-administrator escalation and from a domain compromise.

For requested execution, prepare the exact target, change if any, trigger, and
bounded proof. A successful proof records the resulting account/SID, integrity
and host, with matching event/operation identity. Existing authorization covers
ordinary lab actions; settings, credential, or pre-existing file changes still
need the applicable consent described in `operator-boundaries`. Do not install
MSIs, create accounts, overwrite binaries, or reset passwords as default proof.
Do not add secret extraction, persistence, or log clearing to an escalation
investigation simply because the resulting privilege would allow it.

If execution completion is uncertain, inspect the matching session output and
side effects before another trigger. Stop on verified proof, a blocked
prerequisite, or uncertainty; report remaining hypotheses separately.

## Detection and handoff

Record time, host/session, starting and resulting identity, object paths,
permission evidence, trigger, result, and any authorized changes/rollback.
Local privilege escalation may generate no useful network traffic: distinguish
host telemetry from PCAP-visible activity. Process creation, service/task
changes, registry writes, and module loads depend on the chosen path and enabled
logging. Read-only enumeration is not proof that any of those mutations occurred.
Require separate sensor evidence to mark detection observed; preserve logs and
pre-existing artifacts for the demo.

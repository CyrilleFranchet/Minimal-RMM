---
id: lateral-movement
title: Lateral movement technique selection
description: Evaluate authorized remote-access attempts against the operator's objective, diagnose failures, and verify completion before retrying or changing methods.
default: false
---

# Lateral movement technique selection

## Purpose and scope

Apply this skill when the operator asks to reach another host in the authorized
lab environment using access already established (found credentials, a
collected hash or ticket, or an agent-side pivot). Several techniques can
reach the same target, and they fail for different reasons. A failed technique
is a data point about that protocol and credential combination — not a
conclusion that the target is unreachable or the credential is invalid.

This skill does not authorize lateral movement by itself. Targets and
credentials must be within the operator's stated scope, and using a secret
found on a previous host requires the operator's intent, following
`local-recon-before-network`. Do not add new targets because a technique
failed.

## Technique menu

Before trying remote techniques, check `list_sessions` / `get_session`: if the
target already runs an RMM agent, use the beacon when it satisfies the requested
outcome. Do not substitute beacon control for an explicitly requested access-path
validation. The following execution methods are examples, not a ranked checklist:

| Technique | Typically requires | Common failure causes |
|-----------|--------------------|------------------------|
| Remote service creation (PsExec-style, SMB) | Local admin on target, SMB/445, `ADMIN$` | 445 filtered, UAC remote restrictions filtering non-RID-500 local admins, hidden `ADMIN$`, AV on the dropped binary |
| WMI (`Win32_Process.Create`) | Local admin on target, RPC/DCOM 135 | WMI service disabled, RPC blocked, UAC remote filtering |
| WinRM / PowerShell remoting | Admin or Remote Management Users group, 5985/5986 | WinRM not enabled on target, listener bound to another address, TrustedHosts mismatch, UAC remote filtering |
| Remote scheduled tasks | Local admin, SMB/RPC | Task creation blocked by policy, RPC endpoint mapper unreachable |
| RDP (interactive session) | RDP logon rights + 3389, group membership | No RDP rights, NLA requirements, no interactive logon right for service accounts |
| DCOM (MMC20.Application, ShellWindows) | Local admin via DCOM | DCOM/RPC hardening, 135 plus dynamic ports filtered |
| MSSQL (e.g. `xp_cmdshell`) | SQL sysadmin rights | `xp_cmdshell` disabled, no sysadmin, 1433 unreachable |

Authentication material (password, hash, or ticket) is separate from the
execution method; possession alone establishes neither permission nor access.
Actions requiring existing target control are not substitutes for reaching it.

Execution surface matters: commands launched through the Windows agent use
that agent's context and connectivity. Impacket-style
executors (`psexec`, `wmiexec`, `smbexec`, `atexec`), `evil-winrm`, or
NetExec run in Exegol and need their own route to the target — follow
`exegol-rmm-pivot` before blaming the credential. Keep agent-side and
Exegol-side attempts distinct in the report.

When the objective is a durable RMM agent on the target rather than one-off
commands, follow `agent-deployment`: it composes the script template, file
staging, and one of these execution methods into a full deployment, verified
by session registration.

Before using a staged file, confirm that its transfer completed. `queue_upload`
success means queued, not delivered: wait for the matching agent upload result
before copying, extracting, or executing the file. This also applies when the
next action uses a separate Exegol or remote-execution channel. Follow the
event-correlation and inconclusive-result handling in `agent-deployment`.

## Failures are technique-specific

One technique failing does not predict the next. A credential can work over
SMB and still fail over WinRM; a hash valid for authentication can lack the
rights for service creation; a target can accept RDP while 445 is filtered.
Diagnose before retrying:

- **Connectivity**: a timeout or refusal describes the attempted endpoint and
  execution environment, not global host reachability. Inspect routing and
  service evidence before concluding that a pivot is required.
- **Authentication**: distinguish NTSTATUS `STATUS_LOGON_FAILURE`
  (`0xC000006D`) from Win32 `ERROR_LOGON_FAILURE` (1326). Check account/domain
  context and available diagnostic detail before calling credentials invalid.
  Do not automatically cycle credentials after a generic logon failure.
- **Authorization**: distinguish NTSTATUS `STATUS_ACCESS_DENIED`
  (`0xC0000022`) from Win32 `ERROR_ACCESS_DENIED` (5). Access denial can reflect
  permissions, token restrictions, or policy; it does not alone establish
  successful authentication or the exact missing privilege.
- **Policy or hardening**: WinRM disabled, `ADMIN$` hidden, DCOM hardening,
  NTLM restrictions, scheduled-task policy. These block one technique while
  leaving others open.
- **Detection on the target**: classify a security-control intervention only
  when supported by evidence. A generic failure does not prove AV/EDR action
  or predict whether another method would be permitted.

Do not silently switch to an out-of-scope target, a different credential
without the operator's intent, or a destructive workaround when a technique
is blocked. The response to a blocked technique is another authorized
technique, or a reported blocker.

## Workflow

1. Confirm the authorized target, the credential to use, and the operator's
   intent. Note the credential's privilege level and where execution happens
   (agent or Exegol).
2. Start with the technique that matches what you already know: the protocol
   reachable from the execution environment, and the credential's rights.
3. Verify completion and side effects before classifying an attempt as failed.
   A timeout or missing output may follow successful execution. Inspect existing
   job/session/artifact evidence; do not duplicate an operation whose completion
   is uncertain. If it cannot be resolved, report an inconclusive result.
4. After a confirmed failure, consider another method only when its prerequisites
   are supported and it still satisfies the authorized objective. Do not repeat
   unchanged failures or exhaust the menu automatically. Stop on verified success,
   unresolved execution, account-lockout indications, exhausted supported options,
   or a change requiring new scope or operator intent.
5. Re-check prerequisites when reusing infrastructure: an
   existing SOCKS/TUN relay belongs to a specific session and route, and a
   pivot that worked for one target proves nothing about the next.

## Reporting

Report every attempt: method, target, execution environment, sanitized error
and original status-code namespace, evidence, and any change between attempts.
Exclude passwords, hashes, tickets, tokens, and credential-bearing command
strings. Record observed side effects and cleanup status; do not claim cleanup
without verification or remove pre-existing resources.

Use verified success, partial completion, confirmed failure, or inconclusive
as outcome states. Describe supported causes separately, scoped to the tested
endpoint, identity, method, and time. Multiple failed methods do not by themselves
prove invalid credentials or global target unreachability. Distinguish successful
authentication from authorization and verified completion of the requested task.

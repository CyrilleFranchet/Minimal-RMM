---
id: windows-identity-and-groups
title: Windows identity and group membership
description: Resolve the beacon user and groups; do not use net user %USERNAME% on domain accounts
default: true
---

# Windows identity and group membership

The beacon runs as a **Windows logon session** (user, service, or SYSTEM). Session metadata (`username`, `hostname`) is context only — always **verify on the agent** before group or account queries.

Use **`PS:`** for PowerShell and bare CMD (or `cmd:`) for `whoami` — see skill **agent-command-dispatch**.

## Environment variables — CMD vs PowerShell

| Context | Syntax | Example |
|---------|--------|---------|
| CMD (bare line or `cmd:`) | `%NAME%` | `echo %USERDOMAIN%\%USERNAME%` |
| `PS:` | `$env:NAME` | `PS: "$env:USERDOMAIN\$env:USERNAME"` |

**Do not** use `%USERNAME%` inside a `PS:` script — PowerShell does not expand CMD percent variables.

## Do not use `net user %USERNAME%` for the beacon user

`net user <name>` queries the **local SAM** (local accounts on the machine). On domain-joined hosts the interactive user is usually a **domain account** (`DOMAIN\user`). Then:

```text
net user %USERNAME%
```

often fails with *The user name could not be found* even when `%USERNAME%` is set correctly. This is **expected** — not a missing env var.

**Do not** treat that failure as “username unknown” or switch to guessing other names without evidence.

## Preferred — effective identity (access token)

The beacon process identity is what matters for “who am I” and **effective group membership**:

```text
whoami
```

```text
whoami /groups
```

```text
whoami /all
```

PowerShell equivalent:

```text
PS: [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
```

```text
PS: whoami.exe /groups
```

`whoami /groups` lists **SIDs in the current token** (including domain groups). This is the most reliable view of **effective** membership for the logged-on user running the agent.

## Domain vs local

| Question | Tool |
|----------|------|
| Who is the beacon running as? | `whoami`, `PS: [WindowsIdentity]::GetCurrent().Name` |
| What groups are in my token? | `whoami /groups`, `whoami /all` |
| List **local** accounts only | `net user` (no args) |
| Members of a **local** group | `net localgroup Administrators` (adjust group name) |
| Domain user record / all AD groups | AD cmdlets (`Get-ADUser`, `Get-ADPrincipalGroupMembership`) **only if** RSAT/AD module is available; otherwise stick to token view or ask the operator |

For **local** built-in accounts (e.g. `Administrator`, `Guest`), `net user Administrator` can work. For typical domain users, use `whoami` / token APIs instead.

## RMM workflow

1. `list_sessions` / `get_session` — hostname and registration username for display only.
2. `exec_command` with `whoami` and/or `whoami /groups` — canonical identity and effective groups.
3. If the operator asks for local group members, use `net localgroup <GroupName>` (CMD), not `net user %USERNAME%`.
4. If AD enumeration is required, probe for modules (`PS: Get-Module -ListAvailable ActiveDirectory`) before `Get-AD*`; explain limitations if unavailable.

## Common mistakes

| Wrong | Why | Use instead |
|-------|-----|-------------|
| `net user %USERNAME%` on domain PC | Local SAM lookup | `whoami /groups` |
| `PS: net user %USERNAME%` | `%USERNAME%` not expanded in PS | `PS: net user $env:USERNAME` still fails for domain users — use `whoami` |
| Assume `username` from API equals SAM name | Profile/logon names differ | `whoami` on agent |
| `net user DOMAIN\user` without server | Syntax/context dependent | `whoami /groups` or AD tools |

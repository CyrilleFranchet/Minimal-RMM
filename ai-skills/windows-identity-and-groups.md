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
| Domain user / group enumeration | ADWS (`Get-AD*`) first — **windows-ad-recon-stealth**; direct LDAP only if ADWS unavailable |
| List **local** accounts | `PS: Get-LocalUser` — **windows-ad-recon-stealth** |
| Members of a **local** group | `PS: Get-LocalGroupMember` — **windows-ad-recon-stealth** (not `net localgroup` by default) |

For domain users, use `whoami` / token APIs or LDAP — not `net user`. For local accounts, prefer `Get-LocalUser` over `net user`.

## RMM workflow

1. `list_sessions` / `get_session` — hostname and registration username for display only.
2. `exec_command` with `whoami` and/or `whoami /groups` — canonical identity and effective groups.
3. If the operator asks for local group members, prefer `PS: Get-LocalGroupMember -Group 'Administrators'` — see **windows-ad-recon-stealth** (avoid `net localgroup` unless operator accepts RPC noise).
4. If AD enumeration is required, probe `ActiveDirectory` module and use **`Get-AD*`** (ADWS) first — direct LDAP/`[ADSI]` only as fallback — not `net user /domain` — see **windows-ad-recon-stealth**.

## Common mistakes

| Wrong | Why | Use instead |
|-------|-----|-------------|
| `net user %USERNAME%` on domain PC | Local SAM lookup | `whoami /groups` |
| `PS: net user %USERNAME%` | `%USERNAME%` not expanded in PS | `PS: net user $env:USERNAME` still fails for domain users — use `whoami` |
| Assume `username` from API equals SAM name | Profile/logon names differ | `whoami` on agent |
| `net user DOMAIN\user` without server | RPC recon + wrong tool for domain | LDAP/`Get-ADUser` — **windows-ad-recon-stealth** |
| `net localgroup` / `net user /domain` for enum | RPC recon, EDR noise | `Get-AD*`, `[ADSI]`, `Get-LocalGroupMember` |

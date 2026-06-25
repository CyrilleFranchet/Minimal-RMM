---
id: windows-ad-recon-stealth
title: AD recon — RPC noise vs LDAP/ADWS
description: Prefer ADWS (Get-AD*) over direct LDAP and net.exe RPC recon; net user is loud on EDR
default: true
---

# AD recon — RPC noise vs LDAP/ADWS

On domain-joined Windows hosts, **how** you enumerate users and groups affects **detection**. The beacon runs in the operator's authorized lab — prefer **quieter** paths unless the operator explicitly asks for legacy/noisy tooling.

Use **`PS:`** for PowerScript below (see **agent-command-dispatch**). For identity of the **current token**, see **windows-identity-and-groups** (`whoami /groups` is appropriate and is not a substitute for full AD enumeration).

## Noisy — RPC / legacy recon (avoid by default)

These spawn **`net.exe` / `net1.exe`** or similar and drive **SAMR / LSA / MSRPC** recon that blue teams and EDR commonly alert on (process creation, RPC patterns, “recon” rule packs):

| Command / tool | Typical RPC / behavior | Risk |
|----------------|------------------------|------|
| `net user`, `net user /domain` | SAMR, domain user enum | High |
| `net group`, `net group /domain` | SAMR / domain groups | High |
| `net localgroup`, `net localgroup …` | Local group membership via RPC | Medium–high |
| `net accounts`, `net accounts /domain` | Policy / account RPC | Medium |
| `nltest /domain_trusts`, `nltest /dsgetdc` | Netlogon / domain RPC | Medium |
| `query user`, `quser` | Terminal services / session RPC | Medium |
| `wmic useraccount`, `wmic group` | WMI account recon | Medium–high |
| `dsquery *` (legacy) | LDAP via dsquery.exe binary — still a known recon binary | Medium |

**Do not** reach for `net user %USERNAME%` or `net user /domain` as a first step for domain user or group discovery. Besides **opsec**, `net user <name>` often **fails** for domain accounts (local SAM lookup) — see **windows-identity-and-groups**.

Tell the operator when you skip noisy commands and what stealthier alternative you use instead.

## Stealthier — ADWS first, direct LDAP as fallback

For **domain** user/group/membership questions (beyond the current token), prefer queries that **avoid `net.exe` RPC** and, when possible, **avoid raw LDAP** from the client.

### Preference order

| Priority | Method | Client behavior |
|----------|--------|-----------------|
| **1 — preferred** | **ADWS** via **ActiveDirectory** module (`Get-AD*`) | Cmdlets talk to **AD Web Services** on DCs (TCP **9389**), not classic `ldap.exe` / `net.exe` recon binaries |
| **2 — fallback** | **Direct LDAP** via `[ADSI]` / `DirectorySearcher` | .NET LDAP bind/search (TCP **389** / **636**) — quieter than `net`, but more visible than ADWS on many stacks |
| **3 — avoid** | `net user`, `net group`, `nltest`, etc. | SAMR / MSRPC recon — high EDR noise |

**Always try ADWS (`Get-AD*`) first.** Use direct LDAP only when the **ActiveDirectory** module is unavailable or ADWS is unreachable — and say so to the operator.

**Active Directory Web Services (ADWS)** on domain controllers backs the **ActiveDirectory** PowerShell module (RSAT). Cmdlets such as `Get-ADUser`, `Get-ADGroup`, and `Get-ADGroupMember` use the **current logon token** and the ADWS channel — not `net.exe` and typically not hand-rolled LDAP from `[ADSI]`.

Why ADWS over direct LDAP:

- Same opsec goal (no `net.exe`), but **ADWS** matches normal admin tooling (RSAT/AD module) rather than ad-hoc LDAP clients.
- Direct LDAP (`LDAP://…`, `DirectorySearcher`) still works but is a **second choice** — different protocol/port footprint and easier to tie to scripted enumeration.
- Both hit the DC; neither is invisible — ADWS is the **preferred** path when the module is present.

### 1. ADWS — ActiveDirectory module (try first)

```text
PS: Get-Module -ListAvailable ActiveDirectory | Select-Object Name,Version,Path
```

```text
PS: Import-Module ActiveDirectory -ErrorAction SilentlyContinue; Get-ADDomain | Select-Object DNSRoot,DomainControllers
```

```text
PS: Import-Module ActiveDirectory -ErrorAction SilentlyContinue; Get-ADUser -Filter 'SamAccountName -eq "jsmith"' -Properties MemberOf,PasswordLastSet,LastLogonDate | Select-Object Name,SamAccountName,Enabled,MemberOf
```

```text
PS: Import-Module ActiveDirectory -ErrorAction SilentlyContinue; Get-ADGroupMember -Identity 'Domain Admins' | Select-Object Name,SamAccountName,objectClass
```

```text
PS: Import-Module ActiveDirectory -ErrorAction SilentlyContinue; Get-ADPrincipalGroupMembership -Identity (whoami.exe /user) | Select-Object Name,GroupCategory,GroupScope
```

Adjust identity filters from **`whoami`** / operator input — never guess `C:\Users\…` or SAM names without verification.

If `Import-Module ActiveDirectory` fails or `Get-ADUser` errors (no ADWS, firewalled **9389**, offline DC), **then** fall back to direct LDAP below — do not jump to `net user /domain`.

### 2. Direct LDAP — fallback only (`[ADSI]` / `DirectorySearcher`)

When the **ActiveDirectory** module or **ADWS** is unavailable (common on endpoints without RSAT, or when TCP **9389** is blocked), use **LDAP** from .NET — still avoids `net.exe`, but is **less preferred** than ADWS:

```text
PS: $ds=[ADSI]"LDAP://RootDSE"; $base=$ds.defaultNamingContext; $s=[adsisearcher]"(&(objectCategory=user)(sAMAccountName=$env:USERNAME))"; $s.SearchRoot="LDAP://$base"; $s.FindOne().Properties | Format-List
```

```text
PS: $base=([ADSI]"LDAP://RootDSE").defaultNamingContext; $s=New-Object DirectoryServices.DirectorySearcher; $s.SearchRoot="LDAP://$base"; $s.Filter='(&(objectCategory=group)(cn=Domain Admins))'; $s.FindOne() | Select-Object -ExpandProperty Properties
```

These use the **current logon token** for bind. Tell the operator you used **direct LDAP** because ADWS/`Get-AD*` was not available.

### 3. Local accounts/groups without `net.exe`

For **local** SAM (not domain), prefer PowerShell cmdlets over `net localgroup`:

```text
PS: Get-LocalUser | Select-Object Name,Enabled,LastLogon
```

```text
PS: Get-LocalGroup | Select-Object Name,SID
```

```text
PS: Get-LocalGroupMember -Group Administrators | Select-Object Name,ObjectClass,PrincipalSource
```

If `Get-LocalUser` is unavailable, say so and ask whether the operator accepts `net localgroup` or a WMI alternative — do not silently default to `net` for domain AD questions.

## Decision guide

| Goal | Prefer | Fallback | Avoid (default) |
|------|--------|----------|-----------------|
| Who is the beacon? | `whoami`, `whoami /groups` | — | `net user %USERNAME%` |
| Domain user attributes | `Get-ADUser` (ADWS) | `[ADSI]` / `DirectorySearcher` | `net user /domain` |
| Domain group members | `Get-ADGroupMember` (ADWS) | LDAP search | `net group /domain` |
| All groups for a user | `Get-ADPrincipalGroupMembership` (ADWS) | LDAP | `net user` / `net group` loops |
| Local Administrators members | `Get-LocalGroupMember` | — | `net localgroup Administrators` |
| Trusts / DC locator | `Get-ADTrust`, `Get-ADDomain` (ADWS) | — | `nltest` (unless operator requests) |

## RMM workflow

1. Clarify whether the operator wants **token-only** view (quiet) or **directory** enumeration (ADWS first, not `net`).
2. Probe **`ActiveDirectory`** module; `exec_command` with **`PS:`** **`Get-AD*`** cmdlets (ADWS).
3. Only if ADWS/`Get-AD*` fails, fall back to **`PS:`** `[ADSI]` / `DirectorySearcher` (direct LDAP) — report which path was used.
4. If both fail (offline, no DC, non-domain host), report the error — do not chain noisy RPC tools without operator approval.

## Limitations

- ADWS and direct LDAP still generate **DC-side** logs; “stealth” means **avoiding loud client binaries and SAMR RPC**, not invisibility.
- **ADWS** requires the **ActiveDirectory** module, network path to a DC, and TCP **9389** reachable (or AD Web Services running).
- **Direct LDAP** (fallback) uses **389**/**636** and is preferable to `net.exe`, but **less preferred than ADWS** when the module is available.
- **Not applicable** on standalone workgroups — use local cmdlets or token-only recon.

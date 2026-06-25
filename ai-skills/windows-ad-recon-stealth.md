---
id: windows-ad-recon-stealth
title: AD recon — RPC noise vs LDAP/ADWS
description: Prefer LDAP/ADWS over net.exe RPC recon; net user and similar commands are loud on EDR
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

## Stealthier — LDAP via ADWS / .NET

**Active Directory Web Services (ADWS)** on domain controllers (TCP **9389**) backs the **ActiveDirectory** PowerShell module. Cmdlets such as `Get-ADUser`, `Get-ADGroup`, and `Get-ADGroupMember` typically query AD over **LDAP/ADWS** from the logged-on session's credentials — **without** launching `net.exe`.

Prefer this stack for **domain** user/group/membership questions when the operator wants enumeration, not just the current token.

### 1. Probe for the AD module (preferred)

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

### 2. LDAP without RSAT — `[ADSI]` / `DirectorySearcher`

When the **ActiveDirectory** module is missing (common on endpoints without RSAT), use **LDAP** from .NET — still avoids `net.exe`:

```text
PS: $ds=[ADSI]"LDAP://RootDSE"; $base=$ds.defaultNamingContext; $s=[adsisearcher]"(&(objectCategory=user)(sAMAccountName=$env:USERNAME))"; $s.SearchRoot="LDAP://$base"; $s.FindOne().Properties | Format-List
```

```text
PS: $base=([ADSI]"LDAP://RootDSE").defaultNamingContext; $s=New-Object DirectoryServices.DirectorySearcher; $s.SearchRoot="LDAP://$base"; $s.Filter='(&(objectCategory=group)(cn=Domain Admins))'; $s.FindOne() | Select-Object -ExpandProperty Properties
```

These use the **current logon token** for bind. They are not invisible (LDAP still hits DCs) but avoid **RPC recon binaries** and match “stay stealth vs net” operator intent.

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

| Goal | Prefer | Avoid (default) |
|------|--------|-----------------|
| Who is the beacon? | `whoami`, `whoami /groups` | `net user %USERNAME%` |
| Domain user attributes | `Get-ADUser`, LDAP searcher | `net user /domain`, `net user DOMAIN\user` |
| Domain group members | `Get-ADGroupMember` | `net group /domain` |
| All groups for a user | `Get-ADPrincipalGroupMembership` | `net user` / `net group` loops |
| Local Administrators members | `Get-LocalGroupMember` | `net localgroup Administrators` |
| Trusts / DC locator | `Get-ADTrust`, `Get-ADDomain` | `nltest` (unless operator requests) |

## RMM workflow

1. Clarify whether the operator wants **token-only** view (quiet) or **directory** enumeration (LDAP/ADWS, not `net`).
2. `exec_command` with **`PS:`** LDAP/AD cmdlets after probing `ActiveDirectory` module.
3. Summarize results; note if RSAT/AD module was missing and you fell back to `[ADSI]` / `DirectorySearcher`.
4. If LDAP fails (offline, no DC reachability, non-domain host), report the error — do not chain multiple noisy RPC tools without operator approval.

## Limitations

- LDAP/ADWS still generates **DC-side** logs; “stealth” means **avoiding loud client binaries and SAMR RPC**, not invisibility.
- `Get-AD*` requires network path to a DC and rights readable by the beacon identity.
- Some endpoints block RSAT; LDAP .NET may still work.
- **Not applicable** on standalone workgroups — use local cmdlets or token-only recon.

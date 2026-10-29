---
id: windows-ad-recon-stealth
title: AD recon — RPC noise vs LDAP/ADWS
description: Prefer ADWS (Get-AD*) over direct LDAP and net.exe RPC recon; net user is loud on EDR
default: true
---

# AD reconnaissance method selection

For authorized domain queries, prefer ADWS through the ActiveDirectory
PowerShell module, then direct LDAP if the module or ADWS is unavailable.
Avoid legacy `net.exe`/`net1.exe` enumeration by default. This is an operator
method preference, not a guarantee of lower detection: both ADWS and LDAP
produce observable activity, and visibility depends on the environment.

Use `PS:` for PowerShell via RMM, following `agent-command-dispatch`. Reuse the
baseline from `local-recon-before-network`. Current-token questions belong to
`windows-identity-and-groups`; explicitly selected `ad-collection-analysis`
takes precedence for graph collection. Direct operator instructions prevail.

## Directory workflow

1. Establish the requested domain object and scope from verified identity or
   operator input. Do not broaden a single-object lookup into domain discovery.
2. Check module availability:

   ```text
   PS: Get-Module -ListAvailable ActiveDirectory | Select-Object Name,Version
   ```

3. In a separate call, import with `-ErrorAction Stop` and run the scoped
   `Get-AD*` query. Inspect the result before choosing a fallback.
4. If the module or ADWS is unavailable, explain that limitation and use a
   separate direct LDAP query. Do not combine both methods in a large fallback
   script or repeatedly retry unchanged authentication/connectivity failures.
5. If neither method works, report the concrete error. Do not silently switch
   to legacy RPC tools; obtain operator preference before changing that method.

| Question | ADWS cmdlet |
|----------|-------------|
| User properties | `Get-ADUser` with verified identity and requested properties |
| Group members | `Get-ADGroupMember`; distinguish direct and recursive membership |
| A principal's directory groups | `Get-ADPrincipalGroupMembership` with an actual identity/SID |
| Domain information or trusts | `Get-ADDomain` / `Get-ADTrust`, scoped to the question |

Do not pass formatted `whoami /user` output as an AD identity. Resolve the SID
or account identifier first. Avoid hardcoded localized group names when a
verified SID or distinguished name is available.

## LDAP fallback and error handling

Use `[ADSI]` / `DirectorySearcher` with an explicit `DirectoryEntry` search
root, narrow search base/filter, and only the required properties. Escape
operator-supplied LDAP filter values. Use the established authentication context;
local token identity does not by itself prove a successful directory bind.

ADWS requires the module and reachable service (normally TCP 9389); LDAP uses
the configured LDAP/LDAPS endpoint. Report the method actually used and why.
An empty transcript is not proof of an empty directory result: inspect command
completion and errors. Avoid blanket `SilentlyContinue`. Do not pipe directly
from a PowerShell `foreach` statement; collect results or use `ForEach-Object`.

## Local accounts and legacy tools

For local SAM questions, use `Get-LocalUser`, `Get-LocalGroup`, or
`Get-LocalGroupMember` through `PS:`. These answer a different question from
token groups or directory membership. On workgroup hosts, use local/token
queries rather than attempting AD discovery.

If local cmdlets are unavailable, state the limitation and establish the
operator's preferred alternative. Legacy commands such as `net user /domain`,
`net group`, `net localgroup`, and `nltest` are not automatic fallback steps.
`net user <name>` queries local SAM by default; failure for a domain user does
not mean the account is unknown.

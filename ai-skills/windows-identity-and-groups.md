---
id: windows-identity-and-groups
title: Windows identity and group membership
description: Resolve the beacon user and groups; do not use net user %USERNAME% on domain accounts
default: true
---

# Windows identity and group membership

Verify identity on the selected agent before account/group queries. Registration
metadata is context only; the beacon can run as a user, service, or SYSTEM,
different from the desktop user. Reuse recent verified evidence for that token.

## Token versus directory

| Question | Query |
|----------|-------|
| Current identity | `whoami` |
| SID | `whoami /user` |
| Token groups and attributes | `whoami /groups` |
| Identity, groups, privileges, integrity | `whoami /all` |
| PowerShell identity | `PS: [System.Security.Principal.WindowsIdentity]::GetCurrent().Name` |

Token groups are not a full directory inventory. Interpret attributes such as
deny-only status; membership does not automatically establish elevation or
an enabled privilege.

`net user <name>` queries local SAM by default. Failure for a domain account
is not evidence that the username is unknown. Do not try guessed usernames or
use `net user %USERNAME%` to determine the beacon identity.

For directory questions, follow the selected AD skill: normally ADWS first
through `windows-ad-recon-stealth`, with its LDAP fallback. For local accounts
or memberships use `PS: Get-LocalUser` / `Get-LocalGroupMember` with a verified
group identity. Neither query replaces inspection of the current token.

## RMM execution

Select the session with `list_sessions` / `get_session`, then use RMM
`exec_command` or `queue_command` for the relevant query. Follow
`agent-command-dispatch`: bare commands run in CMD; PowerShell requires `PS:`.
CMD uses `%NAME%`; PowerShell uses `$env:NAME`. Do not use `%USERNAME%` inside
PowerShell. Report which identity and membership source the result describes.

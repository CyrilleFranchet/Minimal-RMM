---
id: windows-user-profile-path
title: Windows user profile path
description: Resolve the real profile folder on the agent; never assume C:\Users\<username>
default: true
---

# Windows user profile path

Beacon registration and `whoami` give you a **username**, not a reliable profile directory. Do not build paths like `C:\Users\<username>\...` unless you have confirmed that folder on the agent.

## Why shortcuts fail

- The profile folder name often **differs** from the logon name (e.g. `CORP\jsmith` → `C:\Users\jsmith.CORP` or `jsmith.001`).
- Profiles may live on another drive or share (reassigned / redirected profiles).
- The interactive user may not match the account you think you are targeting (multiple sessions, service accounts).
- Downloads, uploads, exfil, and screenshots need a **verified path** on disk.

## Resolve on the agent first

Prefer PowerShell on the **current beacon user** (exec or queue as appropriate):

```powershell
$env:USERPROFILE
[Environment]::GetFolderPath('UserProfile')
```

For a **specific account** on the machine, map to SID then read the profile path:

```powershell
# Example: resolve profile path for a username (adjust domain as needed)
$user = 'jsmith'
$nt = New-Object System.Security.Principal.NTAccount($user)
$sid = $nt.Translate([System.Security.Principal.SecurityIdentifier]).Value
Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid" |
  Select-Object -ExpandProperty ProfileImagePath
```

Or enumerate profiles:

```powershell
Get-CimInstance Win32_UserProfile |
  Where-Object { -not $_.Special -and $_.LocalPath } |
  Select-Object LocalPath, SID
```

## RMM workflow

1. Use `list_sessions` / `get_session` for hostname and username context only.
2. **Run** one of the commands above via `exec_command` (or queue if sleep is long).
3. Use the **returned path** in `queue_download`, `queue_upload`, `queue_exfil`, or shell paths.
4. If the operator names a username, resolve that user's profile on the agent before any file operation — do not guess `C:\Users\...`.

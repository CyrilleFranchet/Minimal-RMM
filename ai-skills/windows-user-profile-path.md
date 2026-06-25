---
id: windows-user-profile-path
title: Windows user profile path
description: Resolve the real profile folder on the agent; never assume C:\Users\<username>
default: true
---

# Windows user profile path

Beacon registration and `whoami` give you a **username**, not a reliable profile directory. Do not build paths like `C:\Users\<username>\...` unless you have confirmed that folder on the agent.

## Command format on the agent

Profile resolution uses **PowerShell** (pipes, cmdlets, .NET). Every `exec_command` / `queue_command` line must start with **`PS:`** (see skill **agent-command-dispatch**).

- **Do:** `PS: [Environment]::GetFolderPath('UserProfile')`
- **Do not:** `powershell -Command "…"` — CMD will break `|` and cmdlet names before PowerShell runs.

## Why shortcuts fail

- The profile folder name often **differs** from the logon name (e.g. `CORP\jsmith` → `C:\Users\jsmith.CORP` or `jsmith.001`).
- Profiles may live on another drive or share (reassigned / redirected profiles).
- The interactive user may not match the account you think you are targeting (multiple sessions, service accounts).
- Downloads, uploads, exfil, and screenshots need a **verified path** on disk.

## Resolve on the agent first

Use **`PS:`** on the **current beacon user** (`exec_command` or `queue_command`):

```text
PS: $env:USERPROFILE
```

```text
PS: [Environment]::GetFolderPath('UserProfile')
```

List Downloads under the resolved profile:

```text
PS: $p=[Environment]::GetFolderPath('UserProfile'); Get-ChildItem -Force (Join-Path $p 'Downloads') | Select-Object Mode,Length,LastWriteTime,Name | Format-Table -AutoSize
```

For a **specific account** on the machine, map to SID then read the profile path:

```text
PS: $user='jsmith'; $nt=New-Object System.Security.Principal.NTAccount($user); $sid=$nt.Translate([System.Security.Principal.SecurityIdentifier]).Value; (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
```

Or enumerate profiles:

```text
PS: Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.LocalPath } | Select-Object LocalPath, SID | Format-Table -AutoSize
```

## RMM workflow

1. Use `list_sessions` / `get_session` for hostname and username context only.
2. **Run** a `PS:` profile command via `exec_command` (or `queue_command` if sleep is long).
3. Use the **returned path** in `queue_download`, `queue_upload`, `queue_exfil`, or shell paths.
4. If the operator names a username, resolve that user's profile on the agent before any file operation — do not guess `C:\Users\...`.

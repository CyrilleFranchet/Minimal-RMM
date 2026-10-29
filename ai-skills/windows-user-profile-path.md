---
id: windows-user-profile-path
title: Windows user profile path
description: Resolve the real profile folder on the agent; never assume C:\Users\<username>
default: true
---

# Windows user profile path

Resolve paths on the selected agent before file operations. Registration and
`whoami` provide an account name, not its profile directory. Folder names,
drives, redirection, and service versus interactive identities can differ.
Never construct `C:\Users\<username>` from an unverified account name.

Use RMM `exec_command` / `queue_command` with the `PS:` prefix from
`agent-command-dispatch`:

```text
PS: [Environment]::GetFolderPath('UserProfile')
```

For a named account, resolve its qualified identity to SID, then inspect its
profile mapping; replace the example identity with the verified account:

```text
PS: $nt=New-Object System.Security.Principal.NTAccount('DOMAIN\user'); $sid=$nt.Translate([System.Security.Principal.SecurityIdentifier]).Value; (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid" -ErrorAction Stop).ProfileImagePath
```

When profile discovery is needed:

```text
PS: Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.LocalPath } | Select-Object LocalPath,SID
```

Verify the returned path and intended subfolder before using it for downloads,
uploads, exfiltration, or shell operations. A resolved profile does not prove
that Downloads or another known folder exists beneath it; check redirection
when relevant. Missing mappings or access failures are findings, not a reason
to guess a path or substitute another user's profile.

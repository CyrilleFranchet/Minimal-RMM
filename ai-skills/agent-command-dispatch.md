---
id: agent-command-dispatch
title: Agent command dispatch (CMD vs PowerShell)
description: How exec_command and queue_command run on the Windows agent; always use PS: for PowerShell
default: true
---

# Agent command dispatch (CMD vs PowerShell)

RMM `exec_command` and `queue_command` send strings to the Windows agent's
command router, not an interactive PowerShell/SSH shell. PowerShell and Go
Windows agents share these prefix rules:

| Prefix | Engine | Use |
|--------|--------|-----|
| None, or `cmd:` | CMD | Simple built-ins and executables such as `dir`, `whoami`, `sc query` |
| `PS:` or `powershell:` | Windows PowerShell | Cmdlets, variables, pipelines, .NET |
| `pwsh:` | PowerShell 7 when available | Falls back to Windows PowerShell if unavailable |

## PowerShell scripts

Send only the script body after `PS:`. Do not wrap it in `powershell.exe`,
`-Command`, or `-NoProfile`; the router supplies its encoded invocation.

```text
PS: Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special } | Select-Object LocalPath,SID
```

Avoid nested shell quoting and unprefixed PowerShell pipelines. If a cmdlet is
reported as “not recognized,” inspect dispatch and the prefix before assuming
the cmdlet is absent. Use `PS:` for logic beyond simple CMD commands.

## CMD and tool details

- An exact bare `ls`, including `cmd: ls`, maps to `dir` on Windows agents.
- Simple single-quoted segments are converted to CMD double quotes. Quote paths
  containing spaces; use PowerShell for complex expressions.
- For `net group`, put the group name before `/domain` when that command is
  appropriate under the selected reconnaissance workflow.
- The agent tracks the current directory (`RMM_CWD_SIG`) across commands; both
  engines honor it.
- `exec_command` waits for output; use it for short checks. `queue_command`
  queues for a later beacon and is suitable when sleep/jitter is long. Queue
  acceptance is not execution completion.
- Pass the exact command string, including its prefix, to either RMM tool.

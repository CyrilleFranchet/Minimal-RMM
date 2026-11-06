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

## Long operations and recovery

Both agents wait synchronously for command completion. Queueing a command does
not move its execution off the agent's command loop. A foreground interactive
shell or long-lived process can prevent further polling indefinitely. Use the
`spawn_session` tool to start a second agent; it detaches the child with
independent standard streams and a fresh session ID before another agent is
needed. Tool wait timeouts do not establish target-side cancellation or agent
process death.

On Go agent sessions, `make_token` switches the network identity of
subsequently spawned commands (`runas /netonly` semantics); `rev2self`
restores the agent identity. Credentials are passed as explicit
parameters on every surface; never repeat the password in responses or
resubmit it — report the identity label and agent output only. Use
`session_uid` to confirm which identity is active.

`lateral_move` starts a Go agent on another host (psexec, wmiexec, winrm) and verifies that both sessions answer a harmless
command; report `verified: false` instead of resubmitting.

Before a long command, estimate its upper bound from targets, per-target
timeouts, retries, and beacon latency. Use the tool's supported wait budget or
queued execution when that estimate exceeds the synchronous wait. Do not
assume a background AI job extends a tool's own timeout.

Record the session and event cursor before dispatch. Keep the returned job or
operation identifier when available; retrieve matching completion evidence
through existing event/result tools. Return a short summary and artifact path
instead of repeatedly sending full output to the model.

A wait timeout means completion is unknown. Inspect matching events and output
artifacts before another dispatch. Do not rerun a command merely because the
model request timed out. After confirmed completion, preserve its result in the
task report and continue from the next unfinished step.

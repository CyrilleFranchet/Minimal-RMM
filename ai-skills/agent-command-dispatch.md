---
id: agent-command-dispatch
title: Agent command dispatch (CMD vs PowerShell)
description: How exec_command and queue_command run on the Windows agent; always use PS: for PowerShell
default: true
---

# Agent command dispatch (CMD vs PowerShell)

Commands sent with **`exec_command`** or **`queue_command`** are executed on the Windows agent by `Invoke-RmmUserCommand` in `client_rmm.ps1`. This is **not** a native PowerShell or SSH shell — each line is dispatched through a small router.

## Dispatch modes

| Prefix | Engine | When to use |
|--------|--------|-------------|
| *(none)* | **cmd.exe** | Built-in CMD tools: `dir`, `whoami`, `ipconfig`, `sc query`, `net user`, etc. |
| `PS:` or `powershell:` | **Windows PowerShell** | Scripts, cmdlets, pipelines (`|`), `Get-*`, `Select-Object`, .NET APIs |
| `pwsh:` | **PowerShell 7** (if installed) | Same as `PS:` but prefers `pwsh.exe` |
| `cmd:` | **cmd.exe** (explicit) | Force CMD when the line could be ambiguous |

**Default is CMD.** A bare line never runs in PowerShell.

## Critical rule — PowerShell must use `PS:`

Put the **script body only** after the prefix. Do **not** wrap with `powershell.exe`, `-Command`, or `-NoProfile` — the agent adds that internally via `-EncodedCommand`.

### Correct (PowerShell with pipes)

```text
PS: $p=[Environment]::GetFolderPath('UserProfile'); Get-ChildItem -Force (Join-Path $p 'Downloads') | Select-Object Mode,Length,LastWriteTime,Name | Format-Table -AutoSize
```

```text
PS: Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special } | Select-Object LocalPath, SID
```

### Wrong — CMD breaks the pipeline

```text
powershell -NoProfile -Command "$p=[Environment]::GetFolderPath('UserProfile'); Get-ChildItem ... | Select-Object ..."
```

Why this fails: the line runs under **cmd.exe** first. In CMD, `|` is a **CMD pipe even inside double quotes**. CMD splits the line and tries to run `Select-Object`, `Format-Table`, etc. as external programs:

```text
'Select-Object' is not recognized as an internal or external command
```

Same failure mode for `powershell -Command "… | …"` without the `PS:` prefix.

## CMD examples

Bare CMD (no prefix):

```text
whoami
dir C:\ /w
sc query wuauserv
```

Explicit CMD prefix (optional):

```text
cmd: net user
```

CMD tips:

- `ls` is mapped to `dir` on the agent.
- Simple `'single-quoted'` segments are converted to CMD double quotes.
- Paths with spaces may need CMD quoting; for complex logic, use `PS:` instead.

## Working directory

The agent tracks cwd across commands (`RMM_CWD_SIG`). Both CMD and PowerShell modes honor the current directory on the agent.

## Tool usage

- **`exec_command`** — wait for output (blocking). Use for discovery, one-shot checks, short scripts.
- **`queue_command`** — queue for next beacon (non-blocking). Use when sleep/jitter is long.

Both tools take the **exact string** the agent will receive — including `PS:` / `cmd:` when needed.

## Quick decision

| Need | Send |
|------|------|
| `dir`, `whoami`, `net`, `sc` | bare CMD line |
| `Get-ChildItem`, `|`, `$variables`, .NET | `PS: …` |
| PowerShell 7 features | `pwsh: …` |
| Unsure | prefer `PS:` for anything beyond simple CMD builtins |

# Client command execution (hidden processes)

User commands on the Windows agent run through `Invoke-RmmUserCommand` in `client_rmm.ps1`. Default execution uses **cmd.exe**; operators can prefix with `PS:` / `powershell:`, `pwsh:`, or `cmd:`.

## Hidden window behavior

CMD and PowerShell child processes are launched with `System.Diagnostics.ProcessStartInfo`:

- `UseShellExecute = $false`
- `CreateNoWindow = $true`
- stdout and stderr redirected and merged for the operator result

This avoids the brief **cmd.exe console flash** that could occur with `Start-Process -NoNewWindow`. PowerShell launches also pass `-WindowStyle Hidden` for belt-and-suspenders suppression.

The same argument-quoting helper used for rclone (`Format-RmmRcloneProcessArgs`) builds PowerShell command lines. **CMD** runs each operator line via a **temp `.cmd` batch file** under `%TEMP%`: the inner command and `echo RMM_CWD_SIG:%CD%` are written literally to disk; `cmd.exe /d /c` receives only the script path (CreateProcess quoting via `Join-RmmWindowsProcessArguments`). Cwd is set with `ProcessStartInfo.WorkingDirectory`.

**Why a batch file:** quoting nested `"` through `/S /C ""…""` or a single `/c "…"` string breaks in multiple ways — especially `%CD%` expanding to `H:\` before the closing `""`, which makes cmd treat the whole string as a program name. A `.cmd` file avoids passing the operator line through CreateProcess or `/S` parsing; `%CD%` expands on the `echo` batch line after the user command runs.

**Quoting history (avoid regressions):**

| Approach | Issue |
|----------|-------|
| `cd /d "…"` inside `/c` script | Quoting fights with nested `"` and paths with spaces |
| `/d /c "…"` with `""` doubling, no `/S` | Nested `"` still broke |
| CreateProcess `/d /c` + inner `"` only | Worked on some hosts; fragile across cmd parse rules |
| `/S /C ""…""` + `%CD%` or `!CD!` | **`H:\` root cwd** breaks `/S` parse (`'"whoami & echo …H:\"'`) |
| **Temp `.cmd` + `WorkingDirectory`** | Current — operator line never embedded in `/c` argv |

**`net group` syntax:** group name before `/domain` — `net group "Domain Admins" /domain`. Wrong: `net group /domain "Domain Admins"`. Single-quoted segments (`'Domain Admins'`) are converted to CMD `"…"`.

## Working directory

After each command, the agent appends `RMM_CWD_SIG:<path>` (CMD via `%CD%` in the temp batch file, PowerShell via `Get-Location`) and `Apply-RmmCwdFromCmdOutput` updates `$script:RmmShellCwd` for the next command.

## Functions

| Function | Role |
|----------|------|
| `Invoke-RmmUserCommand` | Route operator line to CMD or PowerShell; normalize `ls` → `dir`, single-quote → CMD double-quote |
| `Get-RmmPlainCmdOutput` | Write temp `.cmd`, run via `/d /c` + `WorkingDirectory`; apply cwd sig |
| `Join-RmmWindowsProcessArguments` | CreateProcess quoting for `/d /c` script path only |
| `Normalize-RmmNetGroupCommand` | Rewrite `net group /domain <name>` → `net group <name> /domain` |
| `Invoke-RmmHiddenProcessWait` | Start hidden child process; optional `WorkingDirectory`; async read stdout/stderr; return exit code + streams |
| `Join-RmmProcessOutputText` | Merge stdout/stderr; optional empty-output exit-code message (CMD only) |
| `Invoke-RmmHiddenEncodedPowerShell` | Run `-EncodedCommand` script in hidden `powershell.exe` / `pwsh.exe` |
| `Build-RmmEncodedPowerShellScript` | Wrap `PS:` body with cwd, `$ProgressPreference`, and `RMM_CWD_SIG` |
| `Remove-RmmClixmlProgressOutput` | Strip leaked `#< CLIXML` progress blobs from redirected stdout |
| `Apply-RmmCwdFromCmdOutput` | Strip `RMM_CWD_SIG` lines and update shell cwd |
| `ConvertTo-RmmPlainText` | Flatten PowerShell `ErrorRecord` objects when capturing in-process (legacy paths) |

## Limitations

- Not a full interactive shell — one-shot commands only; latency is one beacon interval.
- Very large stdout/stderr can still block if pipe buffers fill before the child exits (same class of risk as any redirected process).
- CMD quoting rules still apply; complex nested quotes may need `PS:` with `-EncodedCommand` semantics.
- **AI operators:** PowerShell sent without `PS:` / `powershell:` runs through CMD and CMD **still splits on `\|` inside double quotes** — use skill `agent-command-dispatch` and prefix `PS:` for any script with pipelines or cmdlets. See `docs/web-ai-skills.md`.
- **CLIXML noise:** Hidden PowerShell with redirected stdout can serialize progress records (`Preparing modules for first use.`) as `#< CLIXML …` XML. The agent sets `$ProgressPreference = 'SilentlyContinue'` in the `PS:` wrapper and strips any remaining CLIXML from output.

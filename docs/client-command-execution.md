# Client command execution (hidden processes)

User commands on the Windows agent run through `Invoke-RmmUserCommand` in `client_rmm.ps1`. Default execution uses **cmd.exe**; operators can prefix with `PS:` / `powershell:`, `pwsh:`, or `cmd:`.

## Hidden window behavior

CMD and PowerShell child processes are launched with `System.Diagnostics.ProcessStartInfo`:

- `UseShellExecute = $false`
- `CreateNoWindow = $true`
- stdout and stderr redirected and merged for the operator result

This avoids the brief **cmd.exe console flash** that could occur with `Start-Process -NoNewWindow`. PowerShell launches also pass `-WindowStyle Hidden` for belt-and-suspenders suppression.

The same argument-quoting helper used for rclone (`Format-RmmRcloneProcessArgs`) builds PowerShell command lines. **CMD** hidden processes pass `/d`, `/c`, and the script as **separate argv tokens** via `Join-RmmWindowsProcessArguments` (CreateProcess rules — same as legacy `Start-Process -ArgumentList`). Inner paths in the script still use CMD `""` doubling inside `cd /d "…"`.

**Quoting history (avoid regressions):**

| Commit | Approach | Issue |
|--------|----------|-------|
| pre-`590d3c3` | `Start-Process -ArgumentList @('/d','/c',$combined)` | Baseline — cwd with spaces and `net group "…"` both worked |
| `590d3c3` | `Format-RmmRcloneProcessArgs` on `@('/d','/c',$combined)` | Naive `\"` breaks paths with trailing backslashes before `"` |
| `5fd7a08` | `Format-RmmCmdProcessArguments` — wrap script in `/d /c "…"` with all `"` → `""` | Different encoding than CreateProcess; broke nested quotes (`net group "Domain Admins" /domain`) |
| current | `Join-RmmWindowsProcessArguments` | Matches `Start-Process -ArgumentList` / `subprocess.list2cmdline` on Windows |

Do not wrap the entire `/c` script in one CMD doubled-quote envelope, and do not reuse rclone-style `\"` quoting for the `/c` argv token.

## Working directory

After each command, the agent appends `RMM_CWD_SIG:<path>` (CMD via `%CD%`, PowerShell via `Get-Location`) and `Apply-RmmCwdFromCmdOutput` updates `$script:RmmShellCwd` for the next command.

## Functions

| Function | Role |
|----------|------|
| `Invoke-RmmUserCommand` | Route operator line to CMD or PowerShell; normalize `ls` → `dir`, single-quote → CMD double-quote |
| `Get-RmmPlainCmdOutput` | Run inner CMD line in current `$script:RmmShellCwd`; apply cwd sig |
| `Join-RmmWindowsProcessArguments` | Build `ProcessStartInfo.Arguments` with Windows CreateProcess quoting (`/d`, `/c`, script as separate tokens) |
| `Quote-RmmWindowsProcessArgument` | Quote one argv token for CreateProcess (matches `Start-Process -ArgumentList` behavior) |
| `Invoke-RmmHiddenProcessWait` | Start hidden child process; async read stdout/stderr; return exit code + streams |
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

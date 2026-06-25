# Client command execution (hidden processes)

User commands on the Windows agent run through `Invoke-RmmUserCommand` in `client_rmm.ps1`. Default execution uses **cmd.exe**; operators can prefix with `PS:` / `powershell:`, `pwsh:`, or `cmd:`.

## Hidden window behavior

CMD and PowerShell child processes are launched with `System.Diagnostics.ProcessStartInfo`:

- `UseShellExecute = $false`
- `CreateNoWindow = $true`
- stdout and stderr redirected and merged for the operator result

This avoids the brief **cmd.exe console flash** that could occur with `Start-Process -NoNewWindow`. PowerShell launches also pass `-WindowStyle Hidden` for belt-and-suspenders suppression.

The same argument-quoting helper used for rclone (`Format-RmmRcloneProcessArgs`) builds the process command line.

## Working directory

After each command, the agent appends `RMM_CWD_SIG:<path>` (CMD via `%CD%`, PowerShell via `Get-Location`) and `Apply-RmmCwdFromCmdOutput` updates `$script:RmmShellCwd` for the next command.

## Functions

| Function | Role |
|----------|------|
| `Invoke-RmmUserCommand` | Route operator line to CMD or PowerShell; normalize `ls` → `dir`, single-quote → CMD double-quote |
| `Get-RmmPlainCmdOutput` | Run inner CMD line in current `$script:RmmShellCwd`; apply cwd sig |
| `Invoke-RmmHiddenProcessWait` | Start hidden child process; async read stdout/stderr; return exit code + streams |
| `Join-RmmProcessOutputText` | Merge stdout/stderr; optional empty-output exit-code message (CMD only) |
| `Invoke-RmmHiddenEncodedPowerShell` | Run `-EncodedCommand` script in hidden `powershell.exe` / `pwsh.exe` |
| `Apply-RmmCwdFromCmdOutput` | Strip `RMM_CWD_SIG` lines and update shell cwd |
| `ConvertTo-RmmPlainText` | Flatten PowerShell `ErrorRecord` objects when capturing in-process (legacy paths) |

## Limitations

- Not a full interactive shell — one-shot commands only; latency is one beacon interval.
- Very large stdout/stderr can still block if pipe buffers fill before the child exits (same class of risk as any redirected process).
- CMD quoting rules still apply; complex nested quotes may need `PS:` with `-EncodedCommand` semantics.

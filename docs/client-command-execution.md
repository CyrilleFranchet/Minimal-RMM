# Client command execution (hidden processes)

User commands on the Windows agent run through `Invoke-RmmUserCommand` in `client_rmm.ps1`. Default execution uses **cmd.exe**; operators can prefix with `PS:` / `powershell:`, `pwsh:`, or `cmd:`.

## Hidden window behavior

CMD and PowerShell child processes are launched with `System.Diagnostics.ProcessStartInfo`:

- `UseShellExecute = $false`
- `CreateNoWindow = $true`
- stdout and stderr redirected and merged for the operator result

This avoids the brief **cmd.exe console flash** that could occur with `Start-Process -NoNewWindow`. PowerShell launches also pass `-WindowStyle Hidden` for belt-and-suspenders suppression.

The same argument-quoting helper used for rclone (`Format-RmmRcloneProcessArgs`) builds PowerShell command lines. **CMD** uses two launch paths (both with `ProcessStartInfo.WorkingDirectory`, never `cd /d "…"` in the script):

| Inner command | Launch | Cwd marker |
|---------------|--------|------------|
| No `"` in the line (e.g. `whoami`, `net localgroup Administrators`) | `Join-RmmWindowsProcessArguments` → `/d /c …` (CreateProcess argv) | `%CD%` |
| Contains `"` (e.g. `net group "Domain Admins" /domain`) | `Format-RmmCmdSlashSProcessArguments` → `/d /v:on /s /c ""…""` | **`!CD!`** (delayed expansion) |

**Why two paths:** `/S /C ""…""` is required for nested CMD quotes, but **`%CD%` must not appear inside that wrapper** — CMD expands `%CD%` when the `/c` line is first read. On a root drive cwd (`H:\`), that inserts a trailing `\` before the closing `""`, so cmd tries to run the whole quoted string as a program name (`'"whoami & echo RMM_CWD_SIG:H:\"'`). Use **`!CD!`** with `/v:on` in the `/S` path only; it expands when `echo` runs, after parsing.

**Quoting history (avoid regressions):**

| Commit | Approach | Issue |
|--------|----------|-------|
| pre-`590d3c3` | `Start-Process -ArgumentList` + `cd /d` in script | Baseline when quoting was correct |
| `590d3c3` | `Format-RmmRcloneProcessArgs` | Naive `\"` breaks `cd /d` paths |
| `5fd7a08` | `/d /c "…"` + `""` **without `/S`** | Partial cwd fix; nested quotes broke |
| `315df1d` | `Join-RmmWindowsProcessArguments` + **`WorkingDirectory`** | Simple CMD OK on `H:\`; nested `"` still broke |
| `86ace8d` | **`/S /c ""…""` + `%CD%` for all commands** | Broke **every** command on root-drive cwd (`H:\`) |
| current | **Hybrid** + **`WorkingDirectory`** | Simple → Join + `%CD%`; quoted → `/S` + `!CD!` |

Do not use `/S /C ""…""` with **`%CD%`** inside the script. Do not embed `cd /d "…"` in the `/c` script.

**`net group` syntax:** group name before `/domain` — `net group "Domain Admins" /domain`. Single-quoted segments (`'Domain Admins'`) are converted to CMD `"…"`.

## Working directory

After each command, the agent appends `RMM_CWD_SIG:<path>` (CMD via `%CD%` or delayed `!CD!` on the `/S` path, PowerShell via `Get-Location`) and `Apply-RmmCwdFromCmdOutput` updates `$script:RmmShellCwd` for the next command.

## Functions

| Function | Role |
|----------|------|
| `Invoke-RmmUserCommand` | Route operator line to CMD or PowerShell; normalize `ls` → `dir`, single-quote → CMD double-quote |
| `Get-RmmPlainCmdOutput` | Run inner CMD line in current `$script:RmmShellCwd`; apply cwd sig |
| `Format-RmmCmdSlashSProcessArguments` | Build `/d /v:on /s /c ""…""` when inner command contains `"` |
| `Test-RmmCmdInnerNeedsSlashSQuoteWrapper` | True when inner CMD line contains `"` (selects `/S` vs CreateProcess path) |
| `Join-RmmWindowsProcessArguments` | CreateProcess quoting for simple `/d /c` lines (no inner `"`) |
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

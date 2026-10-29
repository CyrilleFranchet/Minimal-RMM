$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot '../agent-powershell/client_rmm.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-RmmChildProcess' }, $true)
Invoke-Expression $definition.Extent.Text
foreach ($exited in @($false, $true)) {
    $child = [pscustomobject]@{ HasExited = $exited; Killed = $false; Waited = $false; Disposed = $false }
    $child | Add-Member ScriptMethod Kill { $this.Killed = $true }
    $child | Add-Member ScriptMethod WaitForExit { param($timeout) $this.Waited = $timeout -eq 5000; return $true }
    $child | Add-Member ScriptMethod Dispose { $this.Disposed = $true }
    Stop-RmmChildProcess $child
    if (-not $child.Disposed -or $child.Killed -eq $exited -or $child.Waited -eq $exited) {
        throw 'Child process cleanup did not follow its lifetime state'
    }
}
$broken = [pscustomobject]@{ HasExited = $false; Disposed = $false }
$broken | Add-Member ScriptMethod Kill { throw 'Already gone' }
$broken | Add-Member ScriptMethod Dispose { $this.Disposed = $true }
Stop-RmmChildProcess $broken
if (-not $broken.Disposed) { throw 'Process handle was not disposed after a kill race' }
Stop-RmmChildProcess $null
Write-Output 'PowerShell agent cleanup tests passed'

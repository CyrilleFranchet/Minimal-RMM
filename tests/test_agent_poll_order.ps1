$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot '../agent-powershell/client_rmm.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$source = [IO.File]::ReadAllText($path)
$match = [regex]::Match($source, '(?s)# Main loop.*?(?<poll>\$headers = Get-RmmRequestHeaders.*?)(?=        if \(\$command -and)')
if (-not $match.Success) { throw 'Cannot locate production beacon polling block' }
$poll = [scriptblock]::Create($match.Groups['poll'].Value)
$script:calls = [System.Collections.Generic.List[string]]::new()
function Get-RmmRequestHeaders { return @{} }
function Register-RmmSession {
    param([switch]$Quiet)
    $script:calls.Add('register')
    if ($script:registrationFailure) { throw 'registration unavailable' }
    return $true
}
function Invoke-RmmRestMethod {
    param($Uri, $Method, $Headers, $RestErrorAction)
    $script:calls.Add('poll')
    return [pscustomobject]@{ command = 'echo regression'; type = 'execute' }
}
function Parse-CmdResponse { param($response) return $response }
function Sync-RmmSocksChannelFromServer { param($CmdData) }
$u = 'http://offline.invalid'
$sessionId = 'offline-regression'
$debugCmdResponse = $false
$script:registrationFailure = $true
$failed = $false
try { . $poll } catch {
    if ($_.Exception.Message -ne 'registration unavailable') { throw }
    $failed = $true
}
if (-not $failed -or ($script:calls -join ',') -ne 'register') {
    throw 'A failed registration dequeued a command'
}
$script:calls.Clear()
$script:registrationFailure = $false
. $poll
if (($script:calls -join ',') -ne 'register,poll' -or $command -ne 'echo regression' -or $cmdType -ne 'execute') {
    throw 'Successful registration did not preserve command dispatch'
}
Write-Output 'PowerShell registration precedes command dequeue'

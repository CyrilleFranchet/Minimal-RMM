param([Parameter(Mandatory = $true)][string]$BaseUrl)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '../agent-powershell/client_rmm.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Resolve-RmmTunnelIpv4', 'Convert-RmmHttpResponseContent', 'Invoke-RmmRestMethod', 'Get-RmmHttpErrorBody', 'Get-RmmHttpResponseHeader', 'Format-RmmHttpErrorBody', 'Write-RmmHttpErrorDetails', 'Get-RmmHttpStatusHint', 'Get-RmmWebExceptionResponse', 'Test-RmmSessionTerminated')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Invoke-Expression $definition.Extent.Text
}
function Write-RmmLog { param($Message, $Level) }
$script:UsePersistentHttp = $false
$script:RmmWebProxy = $null
foreach ($path in @('terminated', 'forbidden', 'server-error')) {
    $caught = $false
    try { Invoke-RmmRestMethod -Uri "$BaseUrl/$path" -Method Get -Headers @{} -RestErrorAction Stop } catch {
        $caught = $true
        $terminated = Test-RmmSessionTerminated -ErrorRecord $_
        if ($terminated -ne ($path -eq 'terminated')) { throw "Incorrect termination detection for $path : $($_.Exception.Message)" }
    }
    if (-not $caught) { throw "Expected an HTTP error for $path" }
}

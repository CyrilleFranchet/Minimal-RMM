param([Parameter(Mandatory = $true)][string]$BaseUrl, [Parameter(Mandatory = $true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot '../agent-powershell/client_rmm.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Resolve-RmmTunnelIpv4', 'Convert-RmmHttpResponseContent', 'Invoke-RmmRestMethod', 'Save-RmmBeaconFile')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Invoke-Expression $definition.Extent.Text
}
function Write-RmmLog { param($Message, $Level) }
$script:UsePersistentHttp = $false
$script:RmmWebProxy = $null
$u = $BaseUrl
$sessionId = 'offline-regression'
$headers = @{ 'X-RMM-Beacon-Token' = 'fake-test-token' }
[IO.File]::WriteAllText($OutputPath, 'original')
foreach ($code in @(301, 302, 303, 307, 308)) {
    foreach ($method in @('Get', 'Post', 'File')) {
        $rejected = $false
        try {
            if ($method -eq 'File') {
                Save-RmmBeaconFile -RelativeUrl "/$code" -DestPath $OutputPath -Headers $headers
            } else {
                Invoke-RmmRestMethod -Uri "$BaseUrl/$code" -Method $method -Headers $headers -Body '{}' -RestErrorAction Stop
            }
        } catch {
            if ($_.Exception.Message -notmatch 'redirects are not accepted') { throw }
            $rejected = $true
        }
        if (-not $rejected) { throw "Redirect $code was accepted by $method" }
        if ([IO.File]::ReadAllText($OutputPath) -ne 'original') { throw 'Redirect response replaced the destination file' }
    }
}
Write-Output 'PowerShell beacon redirects rejected'

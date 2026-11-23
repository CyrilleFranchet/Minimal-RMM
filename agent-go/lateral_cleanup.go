package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// lateralCleanupTagDomain binds the cleanup ownership proof to one purpose so
// the beacon secret can never sign an unrelated message through this channel.
const lateralCleanupTagDomain = "lateral-cleanup-v1"

// lateralCleanupWorkerTimeout bounds the whole worker process, not the native
// calls inside it: a blocked SMB/SCM operation cannot be canceled, but the
// bounded child can be terminated at this deadline.
const lateralCleanupWorkerTimeout = 2 * time.Minute

// lateralCleanupOwnershipMessage is the canonical signed text: the deployment
// identity plus the exact artifacts of that deployment. The control plane and
// the agent must build it identically (see lateral_cleanup_ownership_tag in
// server_rmm.py).
func lateralCleanupOwnershipMessage(deploymentID, host, serviceName, binaryName string) string {
	return strings.Join([]string{lateralCleanupTagDomain, deploymentID, host, serviceName, binaryName}, "\n")
}

// lateralCleanupOwnershipTag mints the HMAC-SHA256 proof over a deployment's
// artifacts using the shared beacon secret.
func lateralCleanupOwnershipTag(beaconSecret, deploymentID, host, serviceName, binaryName string) string {
	mac := hmac.New(sha256.New, []byte(beaconSecret))
	mac.Write([]byte(lateralCleanupOwnershipMessage(deploymentID, host, serviceName, binaryName)))
	return hex.EncodeToString(mac.Sum(nil))
}

// verifyLateralCleanupOwnership checks the control plane's proof for one
// deployment. The tag allows a cleanup retry to survive an agent restart,
// where this process's in-memory ownership record is gone but the server's
// durable deployment record still names these exact artifacts. Only the
// beacon secret holder can mint the tag, and it binds the deployment id and
// both artifact names, so it can never authorize cleanup of another
// deployment's service or binary.
func verifyLateralCleanupOwnership(payload lateralCleanupPayload) bool {
	if payload.DeploymentID == "" || payload.OwnershipTag == "" {
		return false
	}
	cfg, err := loadConfig()
	if err != nil {
		return false
	}
	expected := lateralCleanupOwnershipTag(cfg.BeaconSecret, payload.DeploymentID,
		payload.Host, payload.ServiceName, payload.BinaryName)
	return subtle.ConstantTimeCompare([]byte(expected), []byte(payload.OwnershipTag)) == 1
}

// lateralCleanupRequestAuthorized reports whether a cleanup request may run.
// Either this process recorded the exact acknowledged artifacts of the
// deployment (the same-process fast path, unchanged), or the control plane
// proves durable ownership of the deployment with the beacon-secret tag. A
// request matching neither fails closed: operator-crafted command traffic
// must never turn the cleanup command into a service-deletion primitive.
func lateralCleanupRequestAuthorized(payload lateralCleanupPayload) bool {
	if ownsLateralCleanup(payload) {
		return true
	}
	return verifyLateralCleanupOwnership(payload)
}

// powerShellSingleQuote renders a literal PowerShell single-quoted string.
// Doubling the only metacharacter makes every value embedding safe.
func powerShellSingleQuote(value string) string {
	return strings.ReplaceAll(value, "'", "''")
}

// lateralCleanupWorkerScript renders the dedicated cleanup worker: a tightly
// scoped PowerShell body that owns exactly one deployment's service stop
// wait, service deletion, and launcher-binary removal. All request values are
// embedded through single-quoted literals, so no operator-supplied string can
// break out of the script.
func lateralCleanupWorkerScript(host, serviceName, binaryName string) string {
	var script strings.Builder
	script.WriteString("$ErrorActionPreference = 'Continue'\n")
	fmt.Fprintf(&script, "$target = '%s'\n", powerShellSingleQuote(host))
	fmt.Fprintf(&script, "$serviceName = '%s'\n", powerShellSingleQuote(serviceName))
	fmt.Fprintf(&script, "$binaryName = '%s'\n", powerShellSingleQuote(binaryName))
	script.WriteString(`$sc = Join-Path $env:SystemRoot 'System32\sc.exe'
`)

	// 1. Wait until this deployment's launcher service stops (an already
	//    removed service, exit 1060, is clean and needs no wait).
	script.WriteString(`$serviceAbsent = $false
$deadline = (Get-Date).AddSeconds(15)
while ($true) {
    $query = & $sc ("\\" + $target) query $serviceName
    $scExit = $LASTEXITCODE
    if ($scExit -eq 1060) { $serviceAbsent = $true; break }
    if ($scExit -ne 0) {
        Write-Output ("lateral cleanup failed: query launcher service on " + $target + " (sc.exe exit " + $scExit + ")")
        exit 1
    }
    $state = ''
    if (($query -join ' ') -match 'STATE\s+:\s+(\d+)') { $state = $Matches[1] }
    if ($state -eq '1') { break }
    if ((Get-Date) -ge $deadline) {
        Write-Output "lateral cleanup failed: launcher service has not stopped; cleanup pending"
        exit 1
    }
    Start-Sleep -Milliseconds 200
}
`)

	// 2. Mark the service for deletion. Absent (1060) and already marked
	//    (1072) are both success states, so duplicate retries stay
	//    idempotent.
	script.WriteString(`$serviceDetail = ''
if ($serviceAbsent) {
    $serviceDetail = 'service not found or already removed'
} else {
    & $sc ("\\" + $target) delete $serviceName *> $null
    $scExit = $LASTEXITCODE
    if ($scExit -eq 0) {
        $serviceDetail = 'service deleted'
    } elseif ($scExit -eq 1060 -or $scExit -eq 1072) {
        $serviceDetail = 'service not found or already removed'
    } else {
        Write-Output ("lateral cleanup failed: delete service on " + $target + " (sc.exe exit " + $scExit + ")")
        exit 1
    }
}
`)

	// 3. Remove the staged launcher binary, tolerating the image-handle
	//    close race right after the service stops.
	script.WriteString(`$sharePath = "\\" + $target + '\ADMIN$\System32\' + $binaryName
try {
    $binaryExists = Test-Path -LiteralPath $sharePath -ErrorAction Stop
} catch {
    Write-Output ("lateral cleanup failed: inspect launcher binary on " + $target + " (" + $_.Exception.Message + ")")
    exit 1
}
if (-not $binaryExists) {
    Write-Output ("lateral cleanup: " + $serviceDetail + "; launcher binary not found on ADMIN$")
    exit 0
}
$deadline = (Get-Date).AddSeconds(5)
while ($true) {
    try {
        Remove-Item -LiteralPath $sharePath -Force -ErrorAction Stop
        Write-Output ("lateral cleanup: " + $serviceDetail + "; launcher binary removed from ADMIN$")
        exit 0
    } catch {
        if ((Get-Date) -ge $deadline) {
            Write-Output ("lateral cleanup failed: remove launcher binary on " + $target + " (" + $_.Exception.Message + ")")
            exit 1
        }
        Start-Sleep -Milliseconds 200
    }
}
`)
	return script.String()
}

// lateralCleanupWorkerArguments assembles the worker command line. The
// script travels as an -EncodedCommand value so no quoting or newline of the
// body is ever visible to command-line parsing.
func lateralCleanupWorkerArguments(script string) []string {
	return []string{"-NoLogo", "-NoProfile", "-NonInteractive",
		"-EncodedCommand", winrmEncodedCommand(script)}
}

// windowsPowerShellExecutable resolves Windows PowerShell from System32.
// It is the host process for native operations that must never depend on
// this agent's own image: re-executing os.Executable() inside a loader host
// would relaunch powershell.exe without any agent capability.
func windowsPowerShellExecutable() (string, error) {
	candidates := []string{
		filepath.Join(os.Getenv("SystemRoot"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
	}
	if resolved, err := exec.LookPath("powershell.exe"); err == nil && resolved != "" {
		candidates = append(candidates, resolved)
	}
	for _, candidate := range candidates {
		if candidate == "" {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, nil
		}
	}
	return "", errors.New("the operation requires Windows PowerShell (powershell.exe) in System32")
}

// runLateralCleanupWorker executes the PowerShell cleanup worker as a
// bounded child process. A goroutine was rejected here on purpose: native
// SCM and SMB calls cannot be canceled when a Go context expires, so moving
// the previous child code into a goroutine would keep a blocked native call
// pinned to an unkillable thread with no termination boundary. The separate
// worker process keeps that boundary (the bounded command terminates the
// child at the deadline), while depending only on Windows binaries, never on
// the executable that hosts this agent. The child runs under the active
// make_token identity when this process holds the process-creation
// privilege, exactly like the previous self-relaunching child.
func runLateralCleanupWorker(payload lateralCleanupPayload) (string, error) {
	debugEvent("lateral.cleanup_worker.begin", nil)
	executable, err := windowsPowerShellExecutable()
	if err != nil {
		debugEvent("lateral.cleanup_worker.executable.missing", err)
		return "", err
	}
	script := lateralCleanupWorkerScript(payload.Host, payload.ServiceName, payload.BinaryName)
	ctx, cancel := context.WithTimeout(agentContext, lateralCleanupWorkerTimeout)
	defer cancel()
	child := newBoundedCommand(ctx, executable, lateralCleanupWorkerArguments(script)...)
	applyCommandToken(child)
	debugEvent("lateral.cleanup_worker.child.start", nil)
	out, err := child.CombinedOutput()
	reestablishThreadToken()
	debugEvent("lateral.cleanup_worker.child.returned", err)
	result := ""
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		if trimmed := strings.TrimSpace(line); strings.HasPrefix(trimmed, "lateral cleanup: ") {
			result = trimmed
		}
	}
	if err != nil {
		if message := lastNonEmptyLine(out); message != "" && !strings.HasPrefix(message, "lateral cleanup: ") {
			return "", errors.New(message)
		}
		if result == "" {
			return "", fmt.Errorf("lateral cleanup worker failed: %w", err)
		}
	}
	if result == "" {
		return "", fmt.Errorf("cleanup did not confirm success: %s", strings.TrimSpace(string(out)))
	}
	debugEvent("lateral.cleanup_worker.complete", nil)
	return result, nil
}

// lastNonEmptyLine surfaces the worker's own failure explanation from mixed
// stdout/stderr output.
func lastNonEmptyLine(output []byte) string {
	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if trimmed := strings.TrimSpace(lines[i]); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

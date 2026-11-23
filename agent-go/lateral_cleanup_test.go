package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"os/exec"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

// The ownership message is the cross-language contract with the control
// plane (lateral_cleanup_ownership_tag in server_rmm.py); the domain line
// keeps the beacon secret from ever signing an unrelated message.
func TestLateralCleanupOwnershipTagIsBoundToExactArtifacts(t *testing.T) {
	tag := lateralCleanupOwnershipTag("beacon-secret", "deployment-0001", "ws01",
		"MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe")
	if len(tag) != 64 || tag != strings.ToLower(tag) {
		t.Fatalf("ownership tag must be a lowercase hex digest: %q", tag)
	}
	// Any change to the deployment identity or any artifact name invalidates
	// the proof, so one deployment's tag can never remove another's.
	if lateralCleanupOwnershipTag("beacon-secret", "deployment-0002", "ws01",
		"MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe") == tag {
		t.Fatal("the tag must include the deployment identity")
	}
	if lateralCleanupOwnershipTag("beacon-secret", "deployment-0001", "ws01",
		"MinimalRmmAgent-0011223344556677", "other-agent-0011223344556677.exe") == tag {
		t.Fatal("the tag must include every artifact name")
	}
	if lateralCleanupOwnershipTag("other-secret", "deployment-0001", "ws01",
		"MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe") == tag {
		t.Fatal("the tag must be keyed by the beacon secret")
	}
}

// The verifiable tag is what makes a cleanup recoverable after the source
// agent restarts: the in-process record is gone, but the control plane's
// durable deployment record still proves ownership with the shared secret.
func TestVerifyLateralCleanupOwnershipRequiresConfigAndTag(t *testing.T) {
	payload := lateralCleanupPayload{Host: "ws01",
		ServiceName:  "MinimalRmmAgent-0011223344556677",
		BinaryName:   "minimal-rmm-agent-0011223344556677.exe",
		DeploymentID: "deployment-0001"}
	if verifyLateralCleanupOwnership(payload) {
		t.Fatal("a missing tag must fail closed")
	}
	t.Setenv("RMM_BASE_URL", "http://127.0.0.1:8787")
	t.Setenv("RMM_BEACON_SECRET", "fixture-beacon-secret")
	payload.OwnershipTag = lateralCleanupOwnershipTag("fixture-beacon-secret", payload.DeploymentID,
		payload.Host, payload.ServiceName, payload.BinaryName)
	if !verifyLateralCleanupOwnership(payload) {
		t.Fatal("a correctly signed deployment tag must verify")
	}
	payload.OwnershipTag = lateralCleanupOwnershipTag("wrong-secret", payload.DeploymentID,
		payload.Host, payload.ServiceName, payload.BinaryName)
	if verifyLateralCleanupOwnership(payload) {
		t.Fatal("a tag minted with a different secret must not verify")
	}
	payload.OwnershipTag = lateralCleanupOwnershipTag("fixture-beacon-secret", payload.DeploymentID,
		payload.Host, payload.ServiceName, "other-agent-0011223344556677.exe")
	if verifyLateralCleanupOwnership(payload) {
		t.Fatal("a tag over different artifacts must not verify")
	}
}

// The same-process record and the signed tag are alternative authorities;
// an unmatched request fails closed, so operator-crafted command traffic
// cannot delete arbitrary services through this command.
func TestLateralCleanupRequestAuthorizationFailsClosed(t *testing.T) {
	artifact := lateralCleanupPayload{Host: "ws01",
		ServiceName:  "MinimalRmmAgent-0011223344556677",
		BinaryName:   "minimal-rmm-agent-0011223344556677.exe",
		DeploymentID: "deployment-0001"}
	defer func() {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, artifact)
		lateralCleanupOwners.Unlock()
	}()
	if lateralCleanupRequestAuthorized(artifact) {
		t.Fatal("an unowned and unsigned request must fail closed")
	}
	lateralCleanupOwners.Lock()
	lateralCleanupOwners.artifacts[artifact] = true
	lateralCleanupOwners.Unlock()
	if !lateralCleanupRequestAuthorized(artifact) {
		t.Fatal("the same-process ownership record must authorize the retry")
	}
}

// The cleanup worker script embeds the request only through single-quoted
// literals, so no operator-supplied character can escape its scope, and it
// performs exactly the three narrowly scoped steps of one deployment.
func TestLateralCleanupWorkerScriptScopeAndEscaping(t *testing.T) {
	script := lateralCleanupWorkerScript("ws01", "MinimalRmmAgent-0011223344556677",
		"minimal-rmm-agent-0011223344556677.exe")
	for _, want := range []string{
		"$target = 'ws01'",
		"$serviceName = 'MinimalRmmAgent-0011223344556677'",
		"query $serviceName",
		"delete $serviceName",
		`& $sc ("\\" + $target) query $serviceName`,
		`& $sc ("\\" + $target) delete $serviceName`,
		"Test-Path -LiteralPath $sharePath -ErrorAction Stop",
		`'\ADMIN$\System32\' + $binaryName`,
		"launcher service has not stopped; cleanup pending",
		"lateral cleanup: ",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("worker script missing %q:\n%s", want, script)
		}
	}
	hostile := lateralCleanupWorkerScript("ws01'; Remove-Item C:\\ -Recurse; #",
		"MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe")
	if !strings.Contains(hostile, `$target = 'ws01''; Remove-Item C:\ -Recurse; #'`) {
		t.Fatalf("single quotes must be doubled to keep the host literal: %s", hostile)
	}
	// The result contract stays identical to the old cleanup child's.
	if !strings.Contains(script, "; launcher binary removed from ADMIN$") {
		t.Fatal("worker script must report the launcher binary removal")
	}
}

// Exercise the worker's control flow with local PowerShell command mocks:
// a failed SMB existence probe must never be reported as an absent binary.
func TestLateralCleanupWorkerRemoteTargetAndProbeErrors(t *testing.T) {
	powershell := stage1Powershell(t)
	for _, tc := range []struct {
		name, probe, want string
		failure           bool
	}{
		{"absent", "return $false", "launcher binary not found on ADMIN$", false},
		{"present", "return $true", "launcher binary removed from ADMIN$", false},
		{"access-denied", "Write-Error 'Access is denied'; return $false", "inspect launcher binary on ws01 (Access is denied)", true},
		{"smb-failure", "Write-Error 'The network path was not found'; return $false", "inspect launcher binary on ws01 (The network path was not found)", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			prelude := `function Join-Path { return 'Invoke-TestSc' }
$global:scQueryChecked = $false
$global:scDeleteChecked = $false
function Invoke-TestSc {
    param($Computer, $Action, $Service)
    $global:LASTEXITCODE = 87
    if ($Computer -cne '\\ws01') { throw ('Incorrect remote SCM target: ' + $Computer) }
    if ($Service -cne 'MinimalRmmAgent-0011223344556677') { throw 'Incorrect service' }
    Write-Host ('SC_ACTION=' + $Action)
    $global:LASTEXITCODE = 0
    if ($Action -eq 'query') { $global:scQueryChecked = $true; return 'STATE : 1 STOPPED' }
    if ($Action -ne 'delete') { throw 'Unexpected SCM action' }
    $global:scDeleteChecked = $true
}
function Remove-Item { [CmdletBinding()] param($LiteralPath, [switch]$Force) }
function Test-Path { [CmdletBinding()] param($LiteralPath)
    if (-not ($global:scQueryChecked -and $global:scDeleteChecked)) { throw 'SCM query and delete must both validate their remote target' }
` + tc.probe + "\n}\n"
			script := prelude + lateralCleanupWorkerScript("ws01", "MinimalRmmAgent-0011223344556677", "minimal-rmm-agent-0011223344556677.exe")
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			defer cancel()
			command := exec.CommandContext(ctx, powershell, lateralCleanupWorkerArguments(script)...)
			output, err := command.CombinedOutput()
			if (err != nil) != tc.failure {
				t.Fatalf("failure=%v, want %v: %v: %s", err != nil, tc.failure, err, output)
			}
			for _, want := range []string{"SC_ACTION=query", tc.want} {
				if !strings.Contains(string(output), want) {
					t.Fatalf("worker output missing %q: %s", want, output)
				}
			}
			if tc.failure && strings.Contains(string(output), "lateral cleanup: ") {
				t.Fatalf("failed existence probe reported success: %s", output)
			}
		})
	}
}

// The worker travels as PowerShell's UTF-16LE -EncodedCommand value, so the
// command line never quotes, reveals, or mangles the script body.
func TestLateralCleanupWorkerArgumentsEncodeTheScript(t *testing.T) {
	script := lateralCleanupWorkerScript("ws01", "MinimalRmmAgent-0011223344556677", "b.exe")
	args := lateralCleanupWorkerArguments(script)
	if len(args) != 5 || !strings.HasPrefix(args[0], "-No") || args[3] != "-EncodedCommand" {
		t.Fatalf("unexpected worker arguments: %#v", args)
	}
	decoded, err := base64.StdEncoding.DecodeString(args[4])
	if err != nil {
		t.Fatalf("EncodedCommand must be standard base64: %v", err)
	}
	if len(decoded)%2 != 0 {
		t.Fatal("EncodedCommand must decode to UTF-16LE bytes")
	}
	units := make([]uint16, len(decoded)/2)
	for i := range units {
		units[i] = uint16(decoded[2*i]) | uint16(decoded[2*i+1])<<8
	}
	roundTrip := string(utf16.Decode(units))
	if roundTrip != script {
		t.Fatalf("EncodedCommand must round-trip the script body")
	}
	if bytes.Contains(decoded, []byte("minimal-rmm")) && !bytes.Contains([]byte(script), []byte("minimal-rmm")) {
		t.Fatal("script content mismatch")
	}
}

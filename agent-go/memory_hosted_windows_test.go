//go:build windows

package main

import (
	"errors"
	"strings"
	"testing"
)

// An agent loaded in memory runs inside its PowerShell host process, so
// every operation that relaunches or copies os.Executable() must refuse
// with a clear error instead of deploying the host binary.
func TestMemoryHostedGuardsWindowsOperations(t *testing.T) {
	t.Setenv(memoryHostedEnv, "1")
	payload := lateralPayload{Method: "winrm", Host: "fixture-host", SessionID: "srv-memory-123"}
	if _, err := lateralMoveWithProgress(payload, nil); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("lateral move from an in-memory session: %v", err)
	}
	if _, err := spawnasCommand("__SPAWNAS__\n"+`{"user":"fixture","password":"fixture"}`, &config{}); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("spawnas from an in-memory session: %v", err)
	}
	if _, err := persistenceAction(true, &config{}); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("persistence install from an in-memory session: %v", err)
	}
	if _, err := loadPluginPEContext(nil, nil, &config{}, "http://fixture/plugin.dll", "Run", ""); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("PE plugin from an in-memory session: %v", err)
	}
}

// Loader deployments stream a server-staged artifact instead of this binary,
// and psexec stages the embedded dedicated lateral service, so an in-memory
// source must pass the hosting-mode guard for all three methods: any later
// failure names the actual missing capability, never the PowerShell host
// binary.
func TestMemoryHostedSessionMayDeployLoaderMethods(t *testing.T) {
	t.Setenv(memoryHostedEnv, "1")
	payload := lateralPayload{Method: "wmiexec", Host: "fixture-host", SessionID: "srv-memory-123",
		Stage: "0123456789abcdef0123456789abcdef", PayloadSHA256: strings.Repeat("a", 64),
		loaderDeploy: true, Loader: "csharp"}
	if _, err := lateralMoveWithProgress(payload, nil); errors.Is(err, errMemoryHosted) {
		t.Fatalf("a loader deployment must not depend on the source hosting mode: %v", err)
	}
	psexec := lateralPayload{Method: "psexec", Host: "fixture-host", SessionID: "srv-memory-123",
		Stage: "0123456789abcdef0123456789abcdef"}
	if _, err := lateralMoveWithProgress(psexec, nil); errors.Is(err, errMemoryHosted) {
		t.Fatalf("psexec stages the embedded lateral service and must not depend on the source hosting mode: %v", err)
	}
}

// The cleanup retry runs the dedicated PowerShell worker, never a copy of
// this binary, so a memory-hosted session must authorize it exactly like an
// on-disk one: the in-process artifact record or the control plane's signed
// ownership tag decides, not the hosting mode.
func TestMemoryHostedCleanupRetryAuthorizesThroughOwnership(t *testing.T) {
	request := lateralPayload{Method: "psexec", Host: "memory-cleanup-host", SessionID: "srv-memory-cleanup-1", RemoteName: "lab-agent.exe"}
	suffix := strings.Repeat("cd", 16)
	artifact := lateralCleanupPayload{Host: request.Host, ServiceName: "MinimalRmmAgent-" + suffix, BinaryName: "lab-agent-" + suffix + ".exe"}
	defer func() {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, artifact)
		lateralCleanupOwners.Unlock()
	}()
	rememberLateralCleanup(request, "lateral started: method=psexec host="+request.Host+
		" target session="+request.SessionID+" service="+artifact.ServiceName+" binary="+artifact.BinaryName)
	t.Setenv(memoryHostedEnv, "1")
	if !lateralCleanupRequestAuthorized(artifact) {
		t.Fatal("an owned artifact record must authorize cleanup from a memory-hosted session")
	}
	// A restart drops the in-process record; the signed tag from the
	// control plane's durable deployment record restores the authority.
	lateralCleanupOwners.Lock()
	delete(lateralCleanupOwners.artifacts, artifact)
	lateralCleanupOwners.Unlock()
	if lateralCleanupRequestAuthorized(artifact) {
		t.Fatal("unowned and unsigned cleanup requests must fail closed")
	}
	tagged := artifact
	tagged.DeploymentID = request.SessionID
	t.Setenv("RMM_BASE_URL", "http://127.0.0.1:8787")
	t.Setenv("RMM_BEACON_SECRET", "fixture-beacon-secret")
	tagged.OwnershipTag = lateralCleanupOwnershipTag("fixture-beacon-secret", tagged.DeploymentID,
		tagged.Host, tagged.ServiceName, tagged.BinaryName)
	if !lateralCleanupRequestAuthorized(tagged) {
		t.Fatal("the signed deployment tag must authorize cleanup after a source restart")
	}
	wrongArtifacts := tagged
	wrongArtifacts.BinaryName = "another-deployment-" + suffix + ".exe"
	if lateralCleanupRequestAuthorized(wrongArtifacts) {
		t.Fatal("a deployment's tag must never authorize another deployment's artifacts")
	}
}

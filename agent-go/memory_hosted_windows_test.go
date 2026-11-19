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
// so an in-memory source must pass the hosting-mode guard: any later failure
// names the actual missing capability, never the PowerShell host binary.
func TestMemoryHostedSessionMayDeployLoaderMethods(t *testing.T) {
	t.Setenv(memoryHostedEnv, "1")
	payload := lateralPayload{Method: "wmiexec", Host: "fixture-host", SessionID: "srv-memory-123",
		Stage: "0123456789abcdef0123456789abcdef", PayloadSHA256: strings.Repeat("a", 64),
		loaderDeploy: true, Loader: "csharp"}
	if _, err := lateralMoveWithProgress(payload, nil); errors.Is(err, errMemoryHosted) {
		t.Fatalf("a loader deployment must not depend on the source hosting mode: %v", err)
	}
}

// The cleanup retry re-executes this binary in a bounded child; an owned
// artifact record alone must not authorize that from an in-memory session.
func TestMemoryHostedCleanupRetryRefusesEvenForOwnedArtifacts(t *testing.T) {
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
	command := "__LATERAL_CLEANUP__\n" + `{"host":"` + request.Host + `","service_name":"` +
		artifact.ServiceName + `","binary_name":"` + artifact.BinaryName + `"}`
	if _, err := lateralCleanupCommand(command); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("cleanup retry from an in-memory session: %v", err)
	}
}

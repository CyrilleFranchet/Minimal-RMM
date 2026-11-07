package main

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io"
	"os"
	"strings"
	"syscall"
	"testing"
)

func TestServiceLookupOnlyAcceptsMissingService(t *testing.T) {
	if err := serviceLookupOutcome(syscall.Errno(1060)); err != nil {
		t.Fatal(err)
	}
	for _, callErr := range []error{nil, syscall.Errno(0), syscall.Errno(5), syscall.Errno(1722)} {
		if err := serviceLookupOutcome(callErr); err == nil {
			t.Fatalf("lookup failure must remain an error: %v", callErr)
		}
	}
}

func TestCleanupOutcomePreservesPartialFailures(t *testing.T) {
	for _, pair := range [][2]error{{os.ErrPermission, nil}, {nil, os.ErrPermission}, {os.ErrPermission, os.ErrPermission}} {
		output, err := lateralCleanupOutcome("partial cleanup", pair[0], pair[1])
		if output != "" || !errors.Is(err, os.ErrPermission) {
			t.Fatalf("partial cleanup must fail: %q %v", output, err)
		}
	}
	for _, fileErr := range []error{nil, os.ErrNotExist} {
		output, err := lateralCleanupOutcome("already removed", nil, fileErr)
		if err != nil || !strings.HasPrefix(output, "lateral cleanup: ") {
			t.Fatalf("idempotent cleanup failed: %q %v", output, err)
		}
	}
}

func TestServiceStartOutcomeRequiresAcknowledgedStart(t *testing.T) {
	if err := serviceStartOutcome(true, syscall.Errno(0)); err != nil {
		t.Fatalf("acknowledged service start failed: %v", err)
	}
	for _, callErr := range []error{syscall.Errno(5), syscall.Errno(1053), syscall.Errno(1067), syscall.Errno(0), nil} {
		result := serviceStartOutcome(false, callErr)
		if result == nil {
			t.Fatalf("failed SCM start must not be accepted: %v", callErr)
		}
		if callErr != nil && !errors.Is(callErr, syscall.Errno(0)) && !errors.Is(result, callErr) {
			t.Fatalf("SCM error lost: %v / %v", callErr, result)
		}
	}
}

func TestLateralAgentCommandPreservesExplicitClearedOwner(t *testing.T) {
	owner := ""
	command := lateralAgentCommand(`C:\Windows\System32\agent.exe`, "srv-lateral-test", &owner, "")
	fields := strings.Fields(command)
	session, workspace, err := lateralStartupConfig(fields[1:])
	if err != nil || session != "srv-lateral-test" || workspace != "" {
		t.Fatalf("cleared owner lost: %q / %q / %v", command, workspace, err)
	}
	if strings.Contains(lateralAgentCommand("agent.exe", "srv-lateral-test", nil, ""), "--workspace-id") {
		t.Fatal("legacy payload must preserve embedded owner")
	}
	owner = "alice.workspace"
	if !strings.HasSuffix(lateralAgentCommand("agent.exe", "srv-lateral-test", &owner, ""), "--workspace-id alice.workspace") {
		t.Fatal("explicit workspace owner lost")
	}
}

func TestDeploymentNamesKeepServiceAndBinaryAssociated(t *testing.T) {
	service, binary, err := lateralDeploymentNames("lab-agent.exe", bytes.NewReader(bytes.Repeat([]byte{0xab}, 16)))
	suffix := strings.Repeat("ab", 16)
	if err != nil || service != "MinimalRmmAgent-"+suffix || binary != "lab-agent-"+suffix+".exe" {
		t.Fatalf("deployment names lost their shared suffix: %q %q %v", service, binary, err)
	}
	if !agentServiceNamePattern.MatchString(service) {
		t.Fatal("generated service name cannot be accepted by the target")
	}
	if _, _, err := lateralDeploymentNames("lab-agent.exe", bytes.NewReader(nil)); !errors.Is(err, io.EOF) {
		t.Fatalf("entropy failure must prevent deployment: %v", err)
	}
	if _, _, err := lateralDeploymentNames(`..\agent.exe`, rand.Reader); err == nil {
		t.Fatal("invalid remote path must be rejected")
	}
}

func TestDeploymentNamesDoNotReuseNames(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 100; i++ {
		service, binary, err := lateralDeploymentNames("minimal-rmm-agent.exe", rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		if seen[service] || seen[binary] {
			t.Fatalf("deployment names reused: %q %q", service, binary)
		}
		seen[service], seen[binary] = true, true
	}
}

func TestGeneratedServiceNameSurvivesTargetStartup(t *testing.T) {
	service, binary, err := lateralDeploymentNames("lab-agent.exe", rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	owner := "team.lab-1"
	command := lateralAgentCommand(binary, "srv-lateral-test", &owner, service)
	startup, err := parseLateralStartup(strings.Fields(command)[1:])
	if err != nil || startup.ServiceName != service || startup.WorkspaceID != owner || !startup.WorkspaceSet {
		t.Fatalf("target service identity changed: %+v %v", startup, err)
	}
	for _, flags := range [][]string{
		{"--service-name", "unexpected"},
		{"--service-name", service, "--service-name", service},
		{"--service-name", service, "--workspace-id", owner, "--workspace-id", owner},
	} {
		if _, err := parseLateralStartup(append([]string{"--lateral-session", "srv-lateral-test"}, flags...)); err == nil {
			t.Fatalf("invalid or duplicate identity flags accepted: %v", flags)
		}
	}
}

func TestLauncherCompletionRequiresSuccessfulStoppedState(t *testing.T) {
	for _, state := range []uint32{0, 2, 3, 4, 7} {
		ready, err := launcherServiceOutcome(state, 0, 0)
		if ready || err != nil {
			t.Fatalf("running service authorized cleanup: %d %t %v", state, ready, err)
		}
	}
	if ready, err := launcherServiceOutcome(1, 0, 0); !ready || err != nil {
		t.Fatal("successful stopped service was not accepted")
	}
	if ready, err := launcherServiceOutcome(1, 1066, 2); ready || err == nil {
		t.Fatal("failed launcher reported deployment success")
	} else {
		var failed *launcherFailure
		if !errors.As(err, &failed) || failed.specificCode != 2 {
			t.Fatal("completed failure must be distinguishable from a running or unreachable service")
		}
	}
}

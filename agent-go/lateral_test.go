package main

import (
	"bytes"
	"context"
	"errors"
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

func TestLateralHostClaimGuardsOneDeploymentAtATime(t *testing.T) {
	host := "busy-lateral-host"
	if err := claimLateralHost(host); err != nil {
		t.Fatal(err)
	}
	err := claimLateralHost(host)
	if !errors.Is(err, errLateralHostBusy) || !strings.Contains(err.Error(), host) {
		t.Fatalf("a retry while the first worker may still run must fail closed: %v", err)
	}
	if err := claimLateralHost("other-lateral-host"); err != nil {
		t.Fatalf("an independent host must not be blocked by another claim: %v", err)
	}
	releaseLateralHost(host)
	if err := claimLateralHost(host); err != nil {
		t.Fatalf("a completed worker must free its host claim: %v", err)
	}
	releaseLateralHost(host)
	// Release is idempotent and never strands a host in-flight.
	releaseLateralHost(host)
	if err := claimLateralHost(host); err != nil {
		t.Fatalf("releasing an unclaimed host broke later deployments: %v", err)
	}
	releaseLateralHost(host)
	releaseLateralHost("other-lateral-host")
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

// An explicit identity forces the deployment's network identity. It requires
// a user, applies to wmiexec and winrm only (psexec keeps ambient SCM/SMB),
// splits a combined DOMAIN\user value like make_token, and never leaks the
// password through validation errors.
func TestParseLateralIdentity(t *testing.T) {
	const password = "do-not-leak-me"
	for _, command := range []string{
		`__LATERAL__` + "\n" + `{"method":"psexec","host":"ws01","identity":{"user":"alice"}}`,
		`__LATERAL__` + "\n" + `{"method":"wmiexec","host":"ws01","identity":{}}`,
		`__LATERAL__` + "\n" + `{"method":"wmiexec","host":"ws01","identity":{"user":"  "}}`,
		`__LATERAL__` + "\n" + `{"method":"wmiexec","host":"ws01","identity":{"user":"LAB\\","password":"pw"}}`,
	} {
		_, err := parseLateralPayload(command)
		if err == nil {
			t.Fatalf("payload %q must be rejected", command)
		}
		if strings.Contains(err.Error(), password) {
			t.Fatalf("validation error leaked credential material: %v", err)
		}
	}
	payload, err := parseLateralPayload(`__LATERAL__` + "\n" +
		`{"method":"wmiexec_loader","host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` +
		strings.Repeat("a", 64) + `","identity":{"user":"LAB\\alice","password":"` + password + `"}}`)
	if err != nil {
		t.Fatalf("loader identity must parse: %v", err)
	}
	if payload.Identity == nil || payload.Identity.Domain != "LAB" || payload.Identity.User != "alice" {
		t.Fatalf("combined identity not normalized: %#v", payload.Identity)
	}
	if payload.Identity.Password != password {
		t.Fatalf("password with backslash escapes must survive parsing: %q", payload.Identity.Password)
	}
	payload, err = parseLateralPayload(`__LATERAL__` + "\n" +
		`{"method":"winrm","host":"ws01","identity":{"user":"alice","domain":"LAB","password":"pw"}}`)
	if err != nil {
		t.Fatalf("winrm identity must parse: %v", err)
	}
	if payload.Identity == nil || payload.Identity.Domain != "LAB" || payload.Identity.User != "alice" {
		t.Fatalf("explicit identity not preserved: %#v", payload.Identity)
	}
	// The password must never leave the payload in error text.
	payload, err = parseLateralPayload(`__LATERAL__` + "\n" +
		`{"method":"psexec","host":"ws01","identity":{"user":"alice","password":"` + password + `"}}`)
	if err == nil || strings.Contains(err.Error(), password) {
		t.Fatalf("psexec identity must be rejected without leaking: %v", err)
	}
}

// Loader deployments use their own wire-method names so an older agent must
// reject them instead of ignoring the new fields and deploying its own
// binary from an in-memory session.
func TestParseLateralLoaderWireMethods(t *testing.T) {
	digest := strings.Repeat("a", 64)
	valid := map[string]struct {
		method    string
		https     bool
		insecure  bool
		wireHTTPS bool
	}{
		"wmiexec_loader":              {"wmiexec", false, false, false},
		"winrm_loader":                {"winrm", false, false, false},
		"winrm_https_loader":          {"winrm", true, false, true},
		"winrm_https_insecure_loader": {"winrm", true, true, true},
	}
	for wire, want := range valid {
		body := `{"method":"` + wire + `","host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `"`
		if want.wireHTTPS {
			body += `,"https":true`
			if want.insecure {
				body += `,"insecure":true`
			}
		}
		body += `}`
		payload, err := parseLateralPayload("__LATERAL__\n" + body)
		if err != nil {
			t.Fatalf("%s must parse: %v", wire, err)
		}
		if payload.Method != want.method || payload.HTTPS != want.https || payload.Insecure != want.insecure || !payload.loaderDeploy {
			t.Fatalf("%s normalized to %#v", wire, payload)
		}
		if payload.Loader != "csharp" {
			t.Fatalf("loader must default to csharp, got %q", payload.Loader)
		}
	}
	for _, invalid := range []string{
		// Every loader deployment needs the staging reference and digest.
		`{"method":"wmiexec_loader","host":"ws01","payload_sha256":"` + digest + `"}`,
		`{"method":"winrm_loader","host":"ws01","stage":"0123456789abcdef0123456789abcdef"}`,
		`{"method":"winrm_loader","host":"ws01","stage":"short","payload_sha256":"` + digest + `"}`,
		`{"method":"winrm_loader","host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"zz"}`,
		// Transport flags must match the wire method exactly.
		`{"method":"winrm_loader","host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `","https":true}`,
		`{"method":"winrm_https_loader","host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `","insecure":true}`,
		// The digest belongs to loader deployments only.
		`{"method":"winrm","host":"ws01","payload_sha256":"` + digest + `"}`,
	} {
		if _, err := parseLateralPayload("__LATERAL__\n" + invalid); err == nil {
			t.Fatalf("invalid loader payload must be rejected: %s", invalid)
		}
	}
}

func TestParseLateralLoaderSelection(t *testing.T) {
	base := `"host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + strings.Repeat("a", 64) + `"`
	payload, err := parseLateralPayload(`__LATERAL__` + "\n" + `{"method":"wmiexec_loader",` + base + `,"loader":""}`)
	if err != nil || payload.Loader != "csharp" {
		t.Fatalf("empty loader must default to csharp: %v %#v", err, payload)
	}
	payload, err = parseLateralPayload(`__LATERAL__` + "\n" + `{"method":"wmiexec_loader",` + base + `,"loader":"CSHARP"}`)
	if err != nil || payload.Loader != "csharp" {
		t.Fatalf("loader must be case-insensitive: %v %#v", err, payload)
	}
	for _, command := range []string{
		`__LATERAL__` + "\n" + `{"method":"wmiexec_loader",` + base + `,"loader":"powershell"}`,
		`__LATERAL__` + "\n" + `{"method":"wmiexec_loader",` + base + `,"loader":"RustLoader"}`,
	} {
		_, err := parseLateralPayload(command)
		if err == nil {
			t.Fatalf("unsupported loader must be rejected: %s", command)
		}
		if !strings.Contains(err.Error(), "reserved") {
			t.Fatalf("reserved loader error must be explicit, got %v", err)
		}
	}
}

// The hosting-mode guard follows the real dependency: loader deployments
// stream a server-staged artifact and psexec stages the embedded lateral
// service with a dedicated cleanup worker, so neither touches this binary;
// the legacy non-loader wmiexec and winrm paths still copy or stream it and
// keep refusing to run from an in-memory host.
func TestLateralUsesSelfBinaryFollowsTheDeploymentChain(t *testing.T) {
	for _, freed := range []lateralPayload{
		{Method: "psexec", Stage: "0123456789abcdef0123456789abcdef"},
		{Method: "wmiexec", loaderDeploy: true},
		{Method: "winrm", loaderDeploy: true, HTTPS: true},
	} {
		if lateralUsesSelfBinary(freed) {
			t.Fatalf("method %s must not depend on the source binary", freed.Method)
		}
	}
	for _, legacy := range []lateralPayload{
		{Method: "wmiexec"},
		{Method: "winrm", HTTPS: true},
	} {
		if !lateralUsesSelfBinary(legacy) {
			t.Fatalf("legacy method %s must keep the self-binary dependency", legacy.Method)
		}
	}
}

// The COM security decision follows the source's hosting mode: a
// memory-hosted agent cannot pre-empt its PowerShell host's CLR, so
// RPC_E_TOO_LATE is acceptance there; an on-disk agent stays fail-closed
// because the foreign initializer is unknown.
func TestWMISecurityResultFollowsHostingMode(t *testing.T) {
	if err := wmiSecurityResult(0); err != nil {
		t.Fatalf("a successful initialization must pass: %v", err)
	}
	t.Setenv(memoryHostedEnv, "1")
	if err := wmiSecurityResult(rpcETooLate); err != nil {
		t.Fatalf("a memory-hosted agent must accept its host's COM security: %v", err)
	}
	t.Setenv(memoryHostedEnv, "0")
	err := wmiSecurityResult(rpcETooLate)
	if err == nil || !strings.Contains(err.Error(), "0x80010119") {
		t.Fatalf("an on-disk agent must fail closed on a foreign initialization: %v", err)
	}
	err = wmiSecurityResult(0x80010106)
	if err == nil || !strings.Contains(err.Error(), "thread cloaking failed") {
		t.Fatalf("other failures must keep the cloaking label: %v", err)
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

func TestLateralHostIsIPSelectsAuthenticationFamily(t *testing.T) {
	for _, host := range []string{"10.0.0.8", "192.168.1.20", "fd00::8"} {
		if !lateralHostIsIP(host) {
			t.Fatalf("%s must be treated as an IP literal (NTLM)", host)
		}
	}
	for _, host := range []string{"ws01", "ws01.lab.local", "WS01.LAB.LOCAL"} {
		if lateralHostIsIP(host) {
			t.Fatalf("%s must be treated as a DNS name (Kerberos)", host)
		}
	}
}

func TestDerivedSessionIDIsRandomForEveryCommand(t *testing.T) {
	for _, tag := range []string{"spawn", "spawnas", "lateral"} {
		id := derivedSessionID("go-1730000000000000000", tag)
		if !tokenSafeSessionID.MatchString(id) {
			t.Fatalf("derived %s session ID %q is not beacon token safe", tag, id)
		}
		if !randomSessionID.MatchString(id) {
			t.Fatalf("derived session ID %q must be random hex without command tags", id)
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
		`{"host":"\\\\ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"minimal-rmm-agent-0011223344556677.exe","deployment_id":"aabbccddeeff00112233","ownership_tag":"` +
		strings.Repeat("ab", 32) + `"}`)
	if err != nil {
		t.Fatalf("valid cleanup payload must parse: %v", err)
	}
	if payload.Host != "ws01" || payload.ServiceName == "" || payload.BinaryName == "" {
		t.Fatalf("cleanup payload not parsed: %#v", payload)
	}
	if payload.DeploymentID != "aabbccddeeff00112233" || payload.OwnershipTag != strings.Repeat("ab", 32) {
		t.Fatalf("deployment ownership fields not parsed: %#v", payload)
	}
	if _, err := parseLateralCleanupPayload(`__LATERAL_CLEANUP__` + "\n" +
		`{"host":"ws01","service_name":"bad service","binary_name":"a.exe"}`); err == nil {
		t.Fatal("invalid service names must be rejected")
	}
	for _, invalid := range []string{
		`{"host":"ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"a.exe","deployment_id":"bad id"}`,
		`{"host":"ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"a.exe","ownership_tag":"not-hex"}`,
	} {
		if _, err := parseLateralCleanupPayload(`__LATERAL_CLEANUP__` + "\n" + invalid); err == nil {
			t.Fatalf("invalid ownership fields must be rejected: %s", invalid)
		}
	}
	// Both ownership fields stay optional so an old control plane can keep
	// relying on the same-process ownership record.
	if _, err := parseLateralCleanupPayload(`__LATERAL_CLEANUP__` + "\n" +
		`{"host":"ws01","service_name":"MinimalRmmAgent-0011223344556677","binary_name":"minimal-rmm-agent-0011223344556677.exe"}`); err != nil {
		t.Fatalf("tag-free cleanup payload must still parse: %v", err)
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

func TestMemoryHostedDetectionAndSpawnGuard(t *testing.T) {
	for _, value := range []string{"", "0", "off"} {
		t.Setenv(memoryHostedEnv, value)
		if memoryHosted() {
			t.Fatalf("%q must not report an in-memory session", value)
		}
	}
	t.Setenv(memoryHostedEnv, "1")
	if !memoryHosted() {
		t.Fatal("RMM_IN_MEMORY=1 must report an in-memory session")
	}
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "go-test"}
	if _, err := spawnAgentWithIdentity(cfg, "go-child-123"); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("in-memory session must refuse to relaunch itself: %v", err)
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

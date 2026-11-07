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
		pid, _, _, err := dispatchSpawn(true, spawnMethods{original: func() (int, error) { return 0, launchErr }}, detail)
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
	t.Setenv("RMM_LOG_FILE", "")
	cfg := &config{
		BaseURL:       "http://127.0.0.1:8787",
		BeaconSecret:  "beacon-secret",
		SessionID:     "parent-session",
		HTTPProxy:     "socks5://127.0.0.1:1080",
		SleepSeconds:  45,
		JitterPercent: 20,
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

func TestSpawnUsesOnlyCreateProcessA(t *testing.T) {
	for _, active := range []bool{false, true} {
		for _, launchErr := range []error{nil, syscall.Errno(5), syscall.Errno(1314)} {
			calls := 0
			detail := &spawnDiagnostics{}
			pid, method, degraded, err := dispatchSpawn(active, spawnMethods{original: func() (int, error) {
				calls++
				if launchErr != nil {
					return 0, launchErr
				}
				return 42, nil
			}}, detail)
			if calls != 1 || len(detail.Attempts) != 1 || detail.Attempts[0].Method != "CreateProcessA" {
				t.Fatalf("spawn must make exactly one attempt: calls=%d diagnostics=%+v", calls, detail)
			}
			if launchErr != nil {
				if !errors.Is(err, launchErr) || pid != 0 {
					t.Fatalf("creation failure lost: %d %v", pid, err)
				}
			} else if err != nil || pid != 42 || method != "CreateProcessA" || degraded != active {
				t.Fatalf("unexpected spawn identity: %d %s %t %v", pid, method, degraded, err)
			}
		}
	}
}

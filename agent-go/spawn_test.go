package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The server accepts only these beacon session identifiers.
var tokenSafeSessionID = regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`)

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
		if !strings.HasPrefix(id, "go-1730000000000000000-spawn-") {
			t.Fatalf("spawn session ID %q lost its parent lineage", id)
		}
	}
}

func TestSpawnSessionIDSanitizesAndBoundsParent(t *testing.T) {
	long := strings.Repeat("a", 200)
	id := spawnSessionID(long)
	if !tokenSafeSessionID.MatchString(id) {
		t.Fatalf("long parent produced invalid spawn session ID %q", id)
	}
	if prefix := strings.TrimSuffix(id, strings.TrimPrefix(id, "a")); len(prefix) > 32 {
		t.Fatalf("parent prefix was not bounded: %q", id)
	}
	unsafe := spawnSessionID("../path/../like")
	if strings.Contains(unsafe, "/") || strings.Contains(unsafe, ".") {
		t.Fatalf("unsafe parent leaked into spawn session ID %q", unsafe)
	}
	if !tokenSafeSessionID.MatchString(unsafe) {
		t.Fatalf("unsafe parent produced invalid spawn session ID %q", unsafe)
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

func TestSpawnFallbackStopsAtFirstSuccess(t *testing.T) {
	names := []string{"CreateProcessAsUser", "CreateProcessWithTokenW", "CreateProcessWithLogonW", "CreateProcessA"}
	for success := range names {
		calls := []string{}
		launch := func(index int) func() (int, error) {
			return func() (int, error) {
				calls = append(calls, names[index])
				if index == success {
					return 42, nil
				}
				return 0, os.ErrPermission
			}
		}
		pid, method, degraded, err := dispatchSpawn(true, spawnMethods{launch(0), launch(1), launch(2), launch(3)})
		if err != nil || pid != 42 || method != names[success] || len(calls) != success+1 || degraded != (success == 3) {
			t.Fatalf("wrong fallback behavior: pid=%d method=%s calls=%v degraded=%t err=%v", pid, method, calls, degraded, err)
		}
		for i := range calls {
			if calls[i] != names[i] {
				t.Fatalf("wrong attempt order: %v", calls)
			}
		}
	}
}

func TestSpawnWithoutTokenSkipsIdentityMethods(t *testing.T) {
	forbidden := func() (int, error) { t.Fatal("identity method ran without a token"); return 0, nil }
	pid, method, degraded, err := dispatchSpawn(false, spawnMethods{forbidden, forbidden, forbidden, func() (int, error) { return 42, nil }})
	if err != nil || pid != 42 || method != "CreateProcessA" || degraded {
		t.Fatalf("unexpected default spawn: %d %s %t %v", pid, method, degraded, err)
	}
}

func TestSpawnFallbackReportsEveryFailure(t *testing.T) {
	attempts := []spawnAttempt{
		{"CreateProcessAsUser", func() (int, error) { return 0, os.ErrPermission }},
		{"CreateProcessA", func() (int, error) { return 0, os.ErrNotExist }},
	}
	pid, method, err := trySpawnAttempts(attempts)
	if pid != 0 || method != "" || err == nil || !strings.Contains(err.Error(), "CreateProcessAsUser") || !strings.Contains(err.Error(), "CreateProcessA") {
		t.Fatalf("missing failure details: pid=%d method=%s err=%v", pid, method, err)
	}
}

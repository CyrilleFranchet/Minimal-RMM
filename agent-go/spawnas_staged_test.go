package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The staged wire name and its stage/digest requirements keep every older
// agent rejecting the request instead of relaunching its own image.
func TestParseSpawnasStagedPayload(t *testing.T) {
	digest := strings.Repeat("a", 64)
	command := SPAWNAS_STAGED_COMMAND + "\n" + `{"session_id":"srv-staged-00001","domain":"",` +
		`"user":"LAB\\alice","password":"pw","stage":"0123456789abcdef0123456789abcdef",` +
		`"payload_sha256":"` + digest + `","timeout_seconds":90}`
	payload, err := parseSpawnasStagedPayload(command)
	if err != nil {
		t.Fatalf("valid staged payload must parse: %v", err)
	}
	if payload.Domain != "LAB" || payload.User != "alice" || payload.Password != "pw" {
		t.Fatalf("identity fields not parsed: %#v", payload)
	}
	if payload.SessionID != "srv-staged-00001" || payload.Stage != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("deployment fields not parsed: %#v", payload)
	}
	if payload.PayloadSHA256 != digest || payload.TimeoutSeconds != 90 {
		t.Fatalf("staging fields not parsed: %#v", payload)
	}
	invalid := map[string]string{
		"missing JSON":     SPAWNAS_STAGED_COMMAND,
		"missing stage":    SPAWNAS_STAGED_COMMAND + "\n" + `{"user":"alice","payload_sha256":"` + digest + `"}`,
		"missing digest":   SPAWNAS_STAGED_COMMAND + "\n" + `{"user":"alice","stage":"0123456789abcdef0123456789abcdef"}`,
		"short stage":      SPAWNAS_STAGED_COMMAND + "\n" + `{"user":"alice","stage":"short","payload_sha256":"` + digest + `"}`,
		"bad digest":       SPAWNAS_STAGED_COMMAND + "\n" + `{"user":"alice","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"nothex"}`,
		"debug rejected":   SPAWNAS_STAGED_COMMAND + "\n" + `{"user":"alice","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `","debug":true}`,
		"user is required": SPAWNAS_STAGED_COMMAND + "\n" + `{"stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `"}`,
	}
	for name, command := range invalid {
		if _, err := parseSpawnasStagedPayload(command); err == nil {
			t.Fatalf("%s must be rejected: %s", name, command)
		}
	}
	// Timeout bounds mirror the lateral worker budget rules.
	payload, err = parseSpawnasStagedPayload(SPAWNAS_STAGED_COMMAND + "\n" +
		`{"user":"alice","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `"}`)
	if err != nil {
		t.Fatalf("default timeout must parse: %v", err)
	}
	if payload.TimeoutSeconds != 120 {
		t.Fatalf("timeout must default to 120, got %v", payload.TimeoutSeconds)
	}
	payload, err = parseSpawnasStagedPayload(SPAWNAS_STAGED_COMMAND + "\n" +
		`{"user":"alice","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `","timeout_seconds":9999}`)
	if err != nil || payload.TimeoutSeconds != 3600 {
		t.Fatalf("timeout must clamp to 3600: %v %v", payload.TimeoutSeconds, err)
	}
}

// The spawned image path is session-scoped under the temp directory, so two
// deployments can never collide and an existing image is never overwritten.
func TestSpawnasStagedAgentPathAndExclusiveWrite(t *testing.T) {
	path, err := spawnasStagedAgentPath("srv-staged-00001")
	if err != nil {
		t.Fatalf("valid session id must resolve: %v", err)
	}
	if filepath.Dir(path) != filepath.Clean(os.TempDir()) || filepath.Base(path) != "minimal-rmm-agent-srv-staged-00001.exe" {
		t.Fatalf("unexpected staged image path: %s", path)
	}
	if _, err := spawnasStagedAgentPath("bad session id"); err == nil {
		t.Fatal("invalid session ids must be rejected")
	}
	if err := writeSpawnasStagedAgent(path, []byte("MZfake-staged-agent")); err != nil {
		t.Fatalf("first write must succeed: %v", err)
	}
	defer os.Remove(path)
	if err := writeSpawnasStagedAgent(path, []byte("MZanother")); err == nil {
		t.Fatal("an existing image must never be overwritten (O_EXCL)")
	}
	if err := writeSpawnasStagedAgent(path+".notepad", []byte("not-pe")); err == nil {
		t.Fatal("a non-executable payload must be rejected")
	} else if !strings.Contains(err.Error(), "not a Windows executable") {
		t.Fatalf("unexpected payload error: %v", err)
	}
}

// The staged command never refuses because of the hosting mode: the guard
// that matters is the staging reference plus the digest, not the binary this
// process happens to run from. On non-Windows hosts the only expected
// refusal is the platform guard.
func TestSpawnasStagedCommandGuardsTheDependencyNotTheHostingMode(t *testing.T) {
	t.Setenv(memoryHostedEnv, "1")
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "go-test"}
	command := SPAWNAS_STAGED_COMMAND + "\n" +
		`{"user":"LAB\\alice","password":"pw","session_id":"srv-staged-00002",` +
		`"stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + strings.Repeat("a", 64) + `"}`
	if _, err := parseSpawnasStagedPayload(command); err != nil {
		t.Fatalf("in-memory sources accept the staged request: %v", err)
	}
	_, err := spawnasStagedCommand(command, cfg)
	if errors.Is(err, errMemoryHosted) {
		t.Fatal("the staged command must not refuse from a memory-hosted session")
	}
	if err == nil {
		t.Skip("host test reached the platform boundary only when not on Windows")
	}
	if !strings.Contains(err.Error(), "Windows agent build") && !strings.Contains(err.Error(), "staged") {
		t.Fatalf("unexpected staged spawnas failure: %v", err)
	}
}

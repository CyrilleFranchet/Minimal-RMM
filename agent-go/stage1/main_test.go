package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestStagePayloadDeploymentsDoNotOverwriteEachOther(t *testing.T) {
	directory := t.TempDir()
	legacy := filepath.Join(directory, "minimal-rmm-agent.exe")
	if err := os.WriteFile(legacy, []byte("existing agent"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, session := range []string{"first-session", "second-session"} {
		payload := []byte("MZ payload for " + session)
		path, err := writeStagePayload(directory, session, payload)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := writeStagePayload(directory, session, []byte("replacement")); !os.IsExist(err) {
			t.Fatalf("repeat deployment overwrote an existing executable: %v", err)
		}
		data, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(data, payload) {
			t.Fatalf("original payload was changed: %q %v", data, err)
		}
	}
	data, err := os.ReadFile(legacy)
	if err != nil || string(data) != "existing agent" {
		t.Fatalf("legacy agent was overwritten: %q %v", data, err)
	}
	if _, err := writeStagePayload(directory, "../escape", nil); err == nil {
		t.Fatal("invalid session path accepted")
	}
}

func TestStageMaskRoundTrip(t *testing.T) {
	payload := append([]byte("MZ\x90\x00"), bytes.Repeat([]byte("stage2 payload bytes"), 512)...)
	stagingID := "0123456789abcdef0123456789abcdef"
	masked := stageMask(payload, stagingID)
	if bytes.Equal(masked, payload) {
		t.Fatal("masking must change the payload")
	}
	if bytes.HasPrefix(masked, []byte("MZ")) {
		t.Fatal("masked payload must not look like an executable")
	}
	restored := stageMask(masked, stagingID)
	if !bytes.Equal(restored, payload) {
		t.Fatal("mask must be symmetric")
	}
	other := stageMask(masked, "ffffffffffffffffffffffffffffffff")
	if bytes.Equal(other, payload) {
		t.Fatal("a different staging id must not decode the payload")
	}
}

func TestParseStageArgs(t *testing.T) {
	if _, err := parseStageArgs([]string{"--stage", "short"}); err == nil {
		t.Fatal("short staging ids must be rejected")
	}
	if _, err := parseStageArgs([]string{"--stage", "0123456789abcdef"}); err == nil {
		t.Fatal("missing session must be rejected")
	}
	if _, err := parseStageArgs([]string{"--unknown", "x"}); err == nil {
		t.Fatal("unknown arguments must be rejected")
	}
	if _, err := parseStageArgs([]string{"--stage"}); err == nil {
		t.Fatal("missing argument values must be rejected")
	}
	if _, err := parseStageArgs([]string{
		"--stage", "0123456789abcdef", "--session", "go-target-lateral-1",
		"--base-url", "http://127.0.0.1:8787/",
	}); err != nil {
		t.Fatalf("valid arguments must parse: %v", err)
	}
}

package main

import (
	"bytes"
	"testing"
)

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

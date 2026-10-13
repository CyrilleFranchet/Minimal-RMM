package main

import (
	"encoding/json"
	"testing"
)

func TestPluginCommandEnvelope(t *testing.T) {
	data, err := pluginCommandEnvelope("scan", `{"target":"10.0.0.0/8"}`)
	if err != nil {
		t.Fatalf("pluginCommandEnvelope returned error: %v", err)
	}
	var envelope map[string]string
	if err := json.Unmarshal([]byte(data), &envelope); err != nil {
		t.Fatalf("invalid envelope: %v", err)
	}
	if envelope["command"] != "scan" {
		t.Fatalf("command = %q, want scan", envelope["command"])
	}
	if envelope["input"] != `{"target":"10.0.0.0/8"}` {
		t.Fatalf("input = %q", envelope["input"])
	}
}

func TestPluginCommandEnvelopeRejectsOversizeInput(t *testing.T) {
	if _, err := pluginCommandEnvelope("scan", string(make([]byte, 4096))); err == nil {
		t.Fatal("expected oversized plugin command input to fail")
	}
}

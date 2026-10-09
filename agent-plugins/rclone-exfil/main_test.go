package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func runExfil(t *testing.T, job map[string]any) result {
	t.Helper()
	data, err := json.Marshal(job)
	if err != nil {
		t.Fatalf("marshal job: %v", err)
	}
	payload, err := exfil(string(data))
	if err != nil {
		t.Fatalf("exfil failed: %v", err)
	}
	return payload
}

func TestExfilCopiesSingleFileToLocalBackend(t *testing.T) {
	tmp := t.TempDir()
	source := filepath.Join(tmp, "loot.zip")
	if err := os.WriteFile(source, []byte("lab payload"), 0600); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(tmp, "loot")

	payload := runExfil(t, map[string]any{
		"source":  source,
		"backend": "local",
		"target":  target,
	})
	if !payload.Success {
		t.Fatalf("expected success, got %+v", payload)
	}
	copied, err := os.ReadFile(filepath.Join(target, "loot.zip"))
	if err != nil {
		t.Fatalf("expected copied file on the target remote: %v", err)
	}
	if string(copied) != "lab payload" {
		t.Fatalf("unexpected copied content: %q", copied)
	}
}

func TestExfilSyncsDirectory(t *testing.T) {
	tmp := t.TempDir()
	sourceDir := filepath.Join(tmp, "source")
	if err := os.MkdirAll(filepath.Join(sourceDir, "sub"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sourceDir, "sub", "a.txt"), []byte("A"), 0600); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(tmp, "synced")

	payload := runExfil(t, map[string]any{
		"source":  sourceDir,
		"backend": "local",
		"target":  target,
	})
	if !payload.Success {
		t.Fatalf("expected success, got %+v", payload)
	}
	if _, err := os.Stat(filepath.Join(target, "sub", "a.txt")); err != nil {
		t.Fatalf("expected synced tree on the target remote: %v", err)
	}
}

func TestExfilRevealsObscuredSettings(t *testing.T) {
	tmp := t.TempDir()
	source := filepath.Join(tmp, "file.bin")
	if err := os.WriteFile(source, []byte("x"), 0600); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(tmp, "out")

	// Plain values pass through; obscured-looking values must not break
	// the job (Reveal fails on plaintext, which the plugin tolerates).
	payload := runExfil(t, map[string]any{
		"source":   source,
		"backend":  "local",
		"target":   target,
		"settings": map[string]string{"note": "plain stays plain"},
	})
	if !payload.Success {
		t.Fatalf("expected success, got %+v", payload)
	}
}

func TestExfilRejectsMissingFields(t *testing.T) {
	if _, err := exfil(`{}`); err == nil {
		t.Fatal("expected an error for an empty job")
	}
	if _, err := exfil(`{"source":"C:\\labs\\x"}`); err == nil {
		t.Fatal("expected an error when backend is missing")
	}
	if _, err := exfil(`not json`); err == nil {
		t.Fatal("expected an error for invalid JSON")
	}
}

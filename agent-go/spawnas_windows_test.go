//go:build windows

package main

import (
	"os"
	"strings"
	"testing"
)

func TestRunasEnvironmentBlocksRMMKeys(t *testing.T) {
	// Set several RMM_* keys that must NOT leak to a runas child.
	t.Setenv("RMM_BEACON_SECRET", "super-secret-token")
	t.Setenv("RMM_SESSION_ID", "go-parent-session")
	t.Setenv("RMM_BASE_URL", "http://127.0.0.1:8787")
	t.Setenv("RMM_LOG_FILE", "/tmp/agent.log")
	// Set an allowed key that must pass through.
	t.Setenv("SYSTEMROOT", `C:\Windows`)

	env := runasEnvironment()
	lookup := make(map[string]string)
	for _, entry := range env {
		key, value, _ := strings.Cut(entry, "=")
		lookup[strings.ToUpper(key)] = value
	}

	// Verify no RMM_* keys leaked.
	for key := range lookup {
		if strings.HasPrefix(key, "RMM_") {
			t.Fatalf("runas environment must not contain %s", key)
		}
	}

	// Verify allowlisted keys are present.
	for _, required := range []string{"SYSTEMROOT", "PATH", "COMSPEC"} {
		if _, ok := lookup[required]; !ok {
			// PATH and COMSPEC should exist in the test process environment.
			if os.Getenv(required) != "" {
				t.Fatalf("runas environment must contain %s", required)
			}
		}
	}
}

func TestRunasEnvironmentOnlyContainsAllowlistedKeys(t *testing.T) {
	// Inject a non-allowlisted key.
	t.Setenv("MY_CUSTOM_VAR", "should-not-appear")
	t.Setenv("HOME", "/root")

	env := runasEnvironment()
	allowlist := map[string]bool{
		"SYSTEMROOT": true, "SYSTEMDRIVE": true, "PATH": true,
		"COMSPEC": true, "WINDIR": true, "PATHEXT": true,
	}
	for _, entry := range env {
		key, _, _ := strings.Cut(entry, "=")
		if !allowlist[strings.ToUpper(key)] {
			t.Fatalf("runas environment contains non-allowlisted key %s", key)
		}
	}
}

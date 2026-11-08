//go:build windows

package main

import (
	"errors"
	"testing"
)

// An agent loaded in memory runs inside its PowerShell host process, so
// every operation that relaunches or copies os.Executable() must refuse
// with a clear error instead of deploying the host binary.
func TestMemoryHostedGuardsWindowsOperations(t *testing.T) {
	t.Setenv(memoryHostedEnv, "1")
	payload := lateralPayload{Method: "winrm", Host: "fixture-host", SessionID: "srv-memory-123"}
	if _, err := lateralMoveWithProgress(payload, nil); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("lateral move from an in-memory session: %v", err)
	}
	if _, err := spawnasCommand("__SPAWNAS__\n"+`{"user":"fixture","password":"fixture"}`, &config{}); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("spawnas from an in-memory session: %v", err)
	}
	if _, err := persistenceAction(true, &config{}); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("persistence install from an in-memory session: %v", err)
	}
	if _, err := loadPluginPEContext(nil, nil, &config{}, "http://fixture/plugin.dll", "Run", ""); !errors.Is(err, errMemoryHosted) {
		t.Fatalf("PE plugin from an in-memory session: %v", err)
	}
}

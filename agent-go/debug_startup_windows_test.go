//go:build windows

package main

import (
	"strings"
	"testing"
)

func TestWinRMDetachUsesBuildModeWithoutRuntimeFlags(t *testing.T) {
	args, err := lateralDetachArguments("session-fixture", nil, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(args) < 1 || args[0] != "--lateral-detach" || strings.Contains(strings.Join(args, " "), "debug") {
		t.Fatalf("detach arguments retained runtime debug controls: %q", args)
	}
	if _, err := parseLateralStartup(args[1:]); err != nil {
		t.Fatal(err)
	}
}

func TestDebugSpawnasRejectsRemovedCommandOption(t *testing.T) {
	if _, err := parseSpawnasPayload("__SPAWNAS__\n" + `{"user":"fixture-user"}`); err != nil {
		t.Fatalf("build-only spawnas rejected: %v", err)
	}
	for _, value := range []string{"false", "true", "null"} {
		if _, err := parseSpawnasPayload("__SPAWNAS__\n" + `{"user":"fixture-user","debug":` + value + `}`); err == nil {
			t.Fatalf("removed spawnas debug option %s accepted", value)
		}
	}
}

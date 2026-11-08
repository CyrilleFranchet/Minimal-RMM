//go:build windows

package main

import (
	"testing"
)

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

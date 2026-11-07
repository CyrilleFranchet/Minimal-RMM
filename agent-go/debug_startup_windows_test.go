//go:build windows

package main

import "testing"

func TestWinRMDetachPreservesExplicitDebugSelection(t *testing.T) {
	for _, debug := range []bool{false, true} {
		args, err := lateralDetachArguments("session-fixture", nil, "", debug)
		if err != nil {
			t.Fatal(err)
		}
		if len(args) < 1 || args[0] != "--lateral-detach" {
			t.Fatalf("detach dispatch lost: %q", args)
		}
		startup, err := parseLateralStartup(args[1:])
		if err != nil || !startup.DebugSet || startup.Debug != debug {
			t.Fatalf("WinRM service debug selection lost: %q %+v %v", args, startup, err)
		}
	}
}

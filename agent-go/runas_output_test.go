package main

import (
	"context"
	"strings"
	"testing"
	"time"
)

// Model a pipe that never becomes empty after an immediate child exits. The
// collector must observe exit while draining and must not refresh the snapshot.
func TestRunasOutputSnapshot(t *testing.T) {
	for _, exitCode := range []uint32{0, 7} {
		polls, peeks, reads := 0, 0, 0
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		ops := runasOutputOps{
			stopped:   func() (bool, error) { polls++; return polls >= 3, nil },
			available: func() (uint32, error) { peeks++; return 8192 + 137, nil },
			read: func(buffer []byte) (uint32, error) {
				reads++
				if reads > 5 {
					t.Fatal("collector refreshed bytes from the continuously writing descendant")
				}
				for i := range buffer {
					buffer[i] = 'x'
				}
				return uint32(len(buffer)), nil
			},
			exitCode:  func() (uint32, error) { return exitCode, nil },
			terminate: func() error { t.Fatal("completed child must not need cancellation"); return nil },
		}
		output, err := collectRunasOutputWithOps(ctx, ops)
		cancel()
		if polls != 3 || peeks != 3 || reads != 5 || len(output) != 2*4096+8192+137 {
			t.Fatalf("unbounded final drain: polls=%d peeks=%d reads=%d output=%d", polls, peeks, reads, len(output))
		}
		if exitCode == 0 && err != nil || exitCode != 0 && (err == nil || !strings.Contains(err.Error(), "code 7")) {
			t.Fatalf("exit status lost: code=%d err=%v", exitCode, err)
		}
	}
}

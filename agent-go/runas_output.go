package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"
)

// runasOutputOps isolates the native pipe/process operations for deterministic
// lifecycle tests; production uses one reader and owns every handle until return.
type runasOutputOps struct {
	stopped   func() (bool, error)
	available func() (uint32, error)
	read      func([]byte) (uint32, error)
	exitCode  func() (uint32, error)
	terminate func() error
}

// collectRunasOutputWithOps bounds the final drain to bytes queued when the
// immediate child is observed stopped, even if another writer never goes idle.
func collectRunasOutputWithOps(ctx context.Context, ops runasOutputOps) (string, error) {
	var buf bytes.Buffer
	tmp := make([]byte, 4096)
	var stopped bool
	var remaining, exitCode uint32
	for {
		if err := ctx.Err(); err != nil {
			return buf.String(), errors.Join(err, ops.terminate())
		}
		var available uint32
		if !stopped {
			var err error
			stopped, err = ops.stopped()
			if err != nil {
				return buf.String(), err
			}
			available, err = ops.available()
			if err != nil {
				return buf.String(), err
			}
			if stopped {
				remaining = available
				exitCode, err = ops.exitCode()
				if err != nil {
					return buf.String(), err
				}
			}
		} else {
			available = remaining
		}
		if stopped && remaining == 0 {
			if exitCode != 0 {
				return buf.String(), fmt.Errorf("process exited with code %d", exitCode)
			}
			return buf.String(), nil
		}
		if available != 0 {
			if available > uint32(len(tmp)) {
				available = uint32(len(tmp))
			}
			n, err := ops.read(tmp[:available])
			if err != nil {
				return buf.String(), err
			}
			if n == 0 || n > available {
				return buf.String(), errors.New("ReadFile returned an invalid byte count")
			}
			buf.Write(tmp[:n])
			if stopped {
				remaining -= n
			}
			continue
		}
		select {
		case <-ctx.Done():
		case <-time.After(10 * time.Millisecond):
		}
	}
}

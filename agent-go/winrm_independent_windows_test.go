//go:build windows

package main

import (
	"bytes"
	"context"
	"io"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// End-to-end regression for the independence transition on a real Windows
// host: the shell receiver runs the actual generated script, spawns the
// independent host through a local WMI process create, relays the payload
// over the local pipe, and surfaces the independent host's failure through
// its own stdout. The streamed artifact is deliberately not a PE, so the
// full chain runs without loading an agent image and the relayed mapping
// failure proves every hop: stream, verify, create, connect, relay, and
// reply propagation.
func TestWinrmIndependentReceiverRunsTheTransition(t *testing.T) {
	powershell := stage1Powershell(t)
	agent := []byte("not a valid PE image bytes")
	transfer, _ := independentTransfer(t, agent)
	script := winrmIndependentScript("srv-independent-e2e-00001", transfer)
	stream := make([]byte, transfer.size)
	if _, err := io.ReadFull(transfer.source, stream); err != nil {
		t.Fatalf("assemble the independent stream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	receiver := exec.CommandContext(ctx, powershell, winrmIndependentReceiverArguments(script)...)
	receiver.Stdin = bytes.NewReader(stream)
	output, err := receiver.CombinedOutput()
	if err == nil {
		t.Fatalf("a non-PE payload must fail the transition, got: %s", output)
	}
	text := string(output)
	if !strings.Contains(text, wmiLoaderErrorPrefix+"agent image mapping failed") {
		t.Fatalf("the independent host's failure must be relayed to the shell stdout, got: %s", text)
	}
	if strings.Contains(text, winrmLoaderReadyMarker) {
		t.Fatalf("a failed transition must never print the readiness marker: %s", text)
	}
}

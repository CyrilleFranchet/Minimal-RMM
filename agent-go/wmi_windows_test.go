//go:build windows

package main

import (
	"strings"
	"testing"
)

// TestWMICreateProcess exercises the real local WMI provider and COM ABI.
func TestWMICreateProcess(t *testing.T) {
	if err := wmiCreateProcess("localhost", `cmd.exe /c exit 0`); err != nil {
		t.Fatalf("local WMI process creation: %v", err)
	}
	// A nonexistent executable must reach ExecMethod and return provider failure.
	err := wmiCreateProcess("localhost", `C:\__rmm_wmi_nonexistent__\missing.exe`)
	if err == nil || !strings.Contains(err.Error(), "WMI Win32_Process.Create returned ") {
		t.Fatalf("expected provider ReturnValue failure, got %v", err)
	}
}

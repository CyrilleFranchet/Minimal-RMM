package main

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

func TestBuildCommandInvocationWindows(t *testing.T) {
	tests := []struct {
		name           string
		command        string
		pwshAvailable  bool
		wantExecutable string
		wantArgs       []string
	}{
		{
			name:           "bare ls maps to dir",
			command:        "ls",
			pwshAvailable:  true,
			wantExecutable: "cmd.exe",
			wantArgs:       []string{"/d", "/c", "dir"},
		},
		{
			name:           "explicit cmd maps to dir",
			command:        "cmd: ls",
			pwshAvailable:  true,
			wantExecutable: "cmd.exe",
			wantArgs:       []string{"/d", "/c", "dir"},
		},
		{
			name:           "powershell prefix without space",
			command:        "PS:Get-Process",
			pwshAvailable:  true,
			wantExecutable: "powershell.exe",
			wantArgs:       []string{"-NoProfile", "-NonInteractive", "-Command", "Get-Process"},
		},
		{
			name:           "powershell prefix with whitespace before colon",
			command:        "powershell : Get-Process",
			pwshAvailable:  true,
			wantExecutable: "powershell.exe",
			wantArgs:       []string{"-NoProfile", "-NonInteractive", "-Command", "Get-Process"},
		},
		{
			name:           "pwsh prefix",
			command:        "pwsh:Get-Date",
			pwshAvailable:  true,
			wantExecutable: "pwsh",
			wantArgs:       []string{"-NoProfile", "-NonInteractive", "-Command", "Get-Date"},
		},
		{
			name:           "pwsh falls back to Windows PowerShell",
			command:        "pwsh: Get-Date",
			pwshAvailable:  false,
			wantExecutable: "powershell.exe",
			wantArgs:       []string{"-NoProfile", "-NonInteractive", "-Command", "Get-Date"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotExecutable, gotArgs, err := buildCommandInvocation(tt.command, "windows", tt.pwshAvailable)
			if err != nil {
				t.Fatalf("buildCommandInvocation returned error: %v", err)
			}
			if gotExecutable != tt.wantExecutable {
				t.Fatalf("executable = %q, want %q", gotExecutable, tt.wantExecutable)
			}
			if !reflect.DeepEqual(gotArgs, tt.wantArgs) {
				t.Fatalf("args = %#v, want %#v", gotArgs, tt.wantArgs)
			}
		})
	}
}

func TestBuildCommandInvocationPlatformLimits(t *testing.T) {
	if _, _, err := buildCommandInvocation("PS: Get-Date", "linux", true); err == nil {
		t.Fatal("expected PS: to be rejected on non-Windows")
	}
	if _, _, err := buildCommandInvocation("cmd: dir", "linux", true); err == nil {
		t.Fatal("expected cmd: to be rejected on non-Windows")
	}
	name, args, err := buildCommandInvocation("ls", "linux", true)
	if err != nil {
		t.Fatalf("buildCommandInvocation returned error: %v", err)
	}
	if name != "/bin/sh" || !reflect.DeepEqual(args, []string{"-c", "ls"}) {
		t.Fatalf("Linux invocation = %q %#v, want /bin/sh %#v", name, args, []string{"-c", "ls"})
	}
}

func TestBuildCommandInvocationRejectsEmptyPrefixedCommand(t *testing.T) {
	if _, _, err := buildCommandInvocation("pwsh:", "windows", true); err == nil {
		t.Fatal("expected an empty pwsh command to fail")
	}
}

func TestPluginCommandEnvelope(t *testing.T) {
	data, err := pluginCommandEnvelope("scan", `{"target":"10.0.0.0/8"}`)
	if err != nil {
		t.Fatalf("pluginCommandEnvelope returned error: %v", err)
	}
	var envelope map[string]string
	if err := json.Unmarshal([]byte(data), &envelope); err != nil {
		t.Fatalf("invalid envelope: %v", err)
	}
	if envelope["command"] != "scan" {
		t.Fatalf("command = %q, want scan", envelope["command"])
	}
	if envelope["input"] != `{"target":"10.0.0.0/8"}` {
		t.Fatalf("input = %q", envelope["input"])
	}
}

func TestPluginCommandEnvelopeRejectsOversizeInput(t *testing.T) {
	if _, err := pluginCommandEnvelope("scan", string(make([]byte, 4096))); err == nil {
		t.Fatal("expected oversized plugin command input to fail")
	}
}

func TestPluginControllerStopWaitsForWorker(t *testing.T) {
	controller := newPluginController("/tools/plugins/tun.dll")
	done := make(chan struct{})
	cancelled := make(chan struct{})
	controller.mu.Lock()
	controller.cancel = func() {
		close(cancelled)
	}
	controller.done = done
	controller.mu.Unlock()

	go func() {
		<-cancelled
		close(done)
	}()

	if !controller.stopAndWait(time.Second) {
		t.Fatal("stopAndWait timed out while the worker was shutting down")
	}
}

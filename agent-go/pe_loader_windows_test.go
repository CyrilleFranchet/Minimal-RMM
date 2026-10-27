//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"testing"
	"time"
)

// TestMSVCPluginChild verifies that a statically linked MSVC DLL executes
// through the same framed child protocol as an operator-loaded plugin.
func TestMSVCPluginChild(t *testing.T) {
	pluginPath := os.Getenv("RMM_MSVC_TEST_DLL")
	agentPath := os.Getenv("RMM_MSVC_TEST_AGENT")
	if pluginPath == "" || agentPath == "" {
		t.Skip("set RMM_MSVC_TEST_DLL and RMM_MSVC_TEST_AGENT to run the MSVC loader test")
	}
	plugin, err := os.ReadFile(pluginPath)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name, input, want string
	}{
		{"constructor", "hello", "msvc-ready"},
		{"unwind", "unwind", "registered"},
		{"module", "module", "unregistered"},
		{"exception", "throw", "caught"},
	}
	if expected := os.Getenv("RMM_MSVC_TEST_EXPECT"); expected != "" {
		cases = []struct{ name, input, want string }{{"runtime", os.Getenv("RMM_MSVC_TEST_INPUT"), expected}}
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var frame bytes.Buffer
			if err := binary.Write(&frame, binary.LittleEndian, uint32(len(plugin))); err != nil {
				t.Fatal(err)
			}
			if err := binary.Write(&frame, binary.LittleEndian, uint32(len(tc.input))); err != nil {
				t.Fatal(err)
			}
			frame.Write(plugin)
			frame.WriteString(tc.input)
			timeout := 30 * time.Second
			if os.Getenv("RMM_MSVC_TEST_CONTAINS") != "" {
				timeout = 3 * time.Minute
			}
			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()
			cmd := exec.CommandContext(ctx, agentPath, "--plugin-child", "Run")
			cmd.Stdin = &frame
			output, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("plugin child failed: %v: %s", err, output)
			}
			if expected := os.Getenv("RMM_MSVC_TEST_CONTAINS"); expected != "" {
				if !strings.Contains(string(output), expected) {
					t.Fatalf("plugin output = %q, missing %q", output, expected)
				}
				return
			}
			if got := strings.TrimSpace(string(output)); got != tc.want {
				t.Fatalf("plugin output = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestMappedPluginDiagnostic prints the mapped base before exercising an
// already-built DLL, so a native crash address can be converted to an RVA.
func TestMappedPluginDiagnostic(t *testing.T) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	path := os.Getenv("RMM_MSVC_TEST_DLL")
	if path == "" {
		t.Skip("set RMM_MSVC_TEST_DLL for the mapped plugin diagnostic")
	}
	plugin, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	base, err := mapPluginPE(plugin)
	if err != nil {
		t.Fatal(err)
	}
	address, err := findPluginExport(base, "Run")
	if err != nil {
		t.Fatal(err)
	}
	fmt.Fprintf(os.Stdout, "mapped base=%#x Run=%#x\n", base, address)
	result, err := callPluginExport(base, "Run", "{}")
	fmt.Fprintf(os.Stdout, "Run result=%q error=%v\n", result, err)
}

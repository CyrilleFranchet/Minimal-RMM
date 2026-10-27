//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"os"
	"os/exec"
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
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, agentPath, "--plugin-child", "Run")
			cmd.Stdin = &frame
			output, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("plugin child failed: %v: %s", err, output)
			}
			if got := strings.TrimSpace(string(output)); got != tc.want {
				t.Fatalf("plugin output = %q, want %q", got, tc.want)
			}
		})
	}
}

//go:build rmmdebug

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestSpawnasDebugEnvironmentIsExplicit(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		env := spawnasDebugEnvironment([]string{"RMM_DEBUG=1", "rmm_debug=1", "KEEP=value"}, enabled)
		want := "RMM_DEBUG=0"
		if enabled {
			want = "RMM_DEBUG=1"
		}
		if len(env) != 2 || env[0] != "KEEP=value" || env[1] != want {
			t.Fatalf("debug choice %t produced %q", enabled, env)
		}
	}
}

func TestSpawnEnvironmentDoesNotInheritDebug(t *testing.T) {
	t.Setenv("RMM_DEBUG", "1")
	t.Setenv("rmm_debug", "1")
	base := spawnEnvironment(&config{}, "debug-fixture-session")
	for _, enabled := range []bool{false, true} {
		env := base
		if enabled {
			env = spawnasDebugEnvironment(base, true)
		}
		count := 0
		for _, entry := range env {
			key, value, _ := strings.Cut(entry, "=")
			if strings.EqualFold(key, "RMM_DEBUG") {
				count++
				if (value == "1") != enabled || (value != "0" && value != "1") {
					t.Fatalf("debug=%t inherited wrong value %q", enabled, value)
				}
			}
		}
		if count != 1 {
			t.Fatalf("debug=%t has %d environment definitions", enabled, count)
		}
	}
}

func TestDebugEventsExcludeSensitiveErrorDetails(t *testing.T) {
	path := filepath.Join(t.TempDir(), "trace.log")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	debugLog.Lock()
	debugLog.file = file
	debugLog.Unlock()
	t.Cleanup(closeAgentDebug)
	debugEvent("fixture.failure", fmt.Errorf("password=secret https://private.invalid/path: %w", syscall.Errno(5)))
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	if !strings.Contains(text, "pid=") || !strings.Contains(text, "event=fixture.failure result=win32:5") {
		t.Fatalf("missing checkpoint or native code: %q", text)
	}
	for _, forbidden := range []string{"password", "secret", "https://", "private.invalid"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("sensitive error content leaked: %q", text)
		}
	}
}

func TestDebugDisabledDoesNotCreateLog(t *testing.T) {
	t.Setenv("RMM_DEBUG", "0")
	initAgentDebug(true)
	debugLog.Lock()
	defer debugLog.Unlock()
	if debugLog.file != nil {
		t.Fatal("debug writer enabled without explicit opt-in")
	}
}

func TestDebugLifecycleSubprocess(t *testing.T) {
	if mode := os.Getenv("RMM_DEBUG_TEST_MODE"); mode != "" {
		initAgentDebug(mode != "quiet")
		defer closeAgentDebug()
		debugLog.Lock()
		if debugLog.file != nil {
			_ = os.WriteFile(os.Getenv("RMM_DEBUG_TEST_PATHFILE"), []byte(debugLog.file.Name()), 0600)
		}
		debugLog.Unlock()
		debugEvent("fixture.begin", nil)
		if mode == "panic" {
			panic("fixture panic diagnostic")
		}
		debugEvent("process.exit.graceful", nil)
		return
	}
	for _, mode := range []string{"graceful", "panic", "quiet", "default"} {
		t.Run(mode, func(t *testing.T) {
			cmd := exec.Command(os.Args[0], "-test.run=^TestDebugLifecycleSubprocess$")
			directory := t.TempDir()
			pathFile := filepath.Join(directory, "log-path")
			cmd.Env = append(os.Environ(), "RMM_DEBUG=1", "RMM_DEBUG_TEST_MODE="+mode,
				"RMM_DEBUG_TEST_PATHFILE="+pathFile, "TMPDIR="+directory)
			if mode == "default" {
				filtered := cmd.Env[:0]
				for _, entry := range cmd.Env {
					if !strings.HasPrefix(strings.ToUpper(entry), "RMM_DEBUG=") {
						filtered = append(filtered, entry)
					}
				}
				cmd.Env = filtered
			}
			output, err := cmd.CombinedOutput()
			if (err != nil) != (mode == "panic") {
				t.Fatalf("unexpected process outcome: %v: %s", err, output)
			}
			if mode == "quiet" && strings.Contains(string(output), "debug diagnostics:") {
				t.Fatalf("helper debug path contaminated captured output: %s", output)
			}
			pathData, pathErr := os.ReadFile(pathFile)
			if pathErr != nil {
				t.Fatalf("debug initialization failed: %v: %s", pathErr, output)
			}
			path := string(pathData)
			t.Cleanup(func() { os.Remove(path) })
			data, readErr := os.ReadFile(path)
			if readErr != nil {
				t.Fatal(readErr)
			}
			text := string(data)
			if debugCrashOutputSupported && !strings.Contains(text, "event=crash_capture.enabled") {
				t.Fatalf("supported runtime did not install crash capture: %s", text)
			}
			if !strings.Contains(text, "event=fixture.begin") || !strings.Contains(text, "event=diagnostics.close") {
				t.Fatalf("missing flushed lifecycle events: %s", text)
			}
			if mode == "graceful" && !strings.Contains(text, "event=process.exit.graceful") {
				t.Fatal("normal exit was not recorded")
			}
			if mode == "panic" {
				if strings.Contains(text, "event=process.exit.graceful") {
					t.Fatal("panic incorrectly marked graceful")
				}
				if strings.Contains(text, "event=crash_capture.enabled") && !strings.Contains(text, "panic: fixture panic diagnostic") {
					t.Fatalf("panic lost during deferred file close: %s", text)
				}
			}
		})
	}
}

func TestDebugMainStartupOverridesAmbientSelection(t *testing.T) {
	executable := filepath.Join(t.TempDir(), "diagnostic-agent.exe")
	build := exec.Command("go", "build", "-buildvcs=false", "-tags=rmmdebug", "-o", executable, ".")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build diagnostic agent: %v: %s", err, output)
	}
	for _, selected := range []string{"1", "0"} {
		t.Run(selected, func(t *testing.T) {
			ambient := "1"
			if selected == "1" {
				ambient = "0"
			}
			env := make([]string, 0, len(os.Environ())+5)
			for _, entry := range os.Environ() {
				key, _, _ := strings.Cut(entry, "=")
				switch strings.ToUpper(key) {
				case "RMM_DEBUG", "RMM_BASE_URL", "RMM_BEACON_SECRET", "RMM_AGENT_WORKSPACE_ID", "RMM_LOG_FILE":
					continue
				}
				env = append(env, entry)
			}
			env = append(env, "RMM_DEBUG="+ambient, "RMM_BASE_URL=", "RMM_BEACON_SECRET=",
				"RMM_AGENT_WORKSPACE_ID=invalid/workspace", "RMM_LOG_FILE="+filepath.Join(t.TempDir(), "agent.log"))
			cmd := exec.Command(executable, "--lateral-session", "fixture-child", "--debug-agent", selected)
			cmd.Env = env
			output, err := cmd.CombinedOutput()
			if exitErr, ok := err.(*exec.ExitError); !ok || exitErr.ExitCode() != 2 {
				t.Fatalf("expected configuration exit before network: %v: %s", err, output)
			}
			path := filepath.Join(debugDirectory(), fmt.Sprintf("minimal-rmm-debug-%d.log", cmd.Process.Pid))
			t.Cleanup(func() { os.Remove(path) })
			data, readErr := os.ReadFile(path)
			if selected == "0" {
				if !os.IsNotExist(readErr) {
					t.Fatalf("disabled main startup created a trace: %v: %s", readErr, data)
				}
				return
			}
			if readErr != nil {
				t.Fatalf("explicit main startup did not enable diagnostics: %v: %s", readErr, output)
			}
			if !strings.Contains(string(data), "event=configuration.invalid") {
				t.Fatalf("main did not record its configuration exit: %s", data)
			}
		})
	}
}

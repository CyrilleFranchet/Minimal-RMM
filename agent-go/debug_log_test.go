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
	"time"
)

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
				"RMM_DEBUG_TEST_PATHFILE="+pathFile, "TMPDIR="+directory, "RMM_SESSION_ID=fixture-"+mode)
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

func TestDebugMainStartupUsesCompiledMode(t *testing.T) {
	for _, diagnostic := range []bool{false, true} {
		tags := ""
		if diagnostic {
			tags = "rmmdebug"
		}
		executable := filepath.Join(t.TempDir(), "renamed-agent.exe")
		build := exec.Command("go", "build", "-buildvcs=false", "-tags="+tags, "-o", executable, ".")
		if output, err := build.CombinedOutput(); err != nil {
			t.Fatalf("build diagnostic=%t: %v: %s", diagnostic, err, output)
		}
		for _, ambient := range []string{"1", "0"} {
			t.Run(fmt.Sprintf("debug=%t/obsolete-env=%s", diagnostic, ambient), func(t *testing.T) {
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
				sessionID := "fixture-" + fmt.Sprint(diagnostic) + "-" + ambient
				cmd := exec.Command(executable, "--lateral-session", sessionID)
				cmd.Env = env
				output, err := cmd.CombinedOutput()
				if exitErr, ok := err.(*exec.ExitError); !ok || exitErr.ExitCode() != 2 {
					t.Fatalf("expected configuration exit before network: %v: %s", err, output)
				}
				path := filepath.Join(debugDirectory(), sessionID+".log")
				t.Cleanup(func() { os.Remove(path) })
				data, readErr := os.ReadFile(path)
				if !diagnostic {
					if !os.IsNotExist(readErr) {
						t.Fatalf("normal build created a trace: %v: %s", readErr, data)
					}
					return
				}
				if readErr != nil {
					t.Fatalf("debug build did not enable diagnostics: %v: %s", readErr, output)
				}
				if !strings.Contains(string(data), "event=configuration.invalid") {
					t.Fatalf("main did not record its configuration exit: %s", data)
				}
			})
		}
	}
}

func TestDebugSessionIDMatchesConfiguration(t *testing.T) {
	t.Setenv("RMM_SESSION_ID", "")
	t.Setenv("RMM_BASE_URL", "https://fixture.invalid")
	t.Setenv("RMM_BEACON_SECRET", "fixture-secret")
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "")
	original := defaultSessionIDB64
	defaultSessionIDB64 = ""
	t.Cleanup(func() { defaultSessionIDB64 = original })
	id, err := debugSessionID()
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := loadConfig()
	if err != nil || cfg.SessionID != id {
		t.Fatalf("log identity %q differs from configuration: %+v, %v", id, cfg, err)
	}
	for _, invalid := range []string{"../escape", `C:\escape`, "bad/name"} {
		t.Setenv("RMM_SESSION_ID", invalid)
		if _, err := debugSessionID(); err == nil {
			t.Fatalf("unsafe filename accepted: %q", invalid)
		}
	}
}

func TestDebugSessionLogAppends(t *testing.T) {
	if os.Getenv("RMM_DEBUG_APPEND_FIXTURE") == "1" {
		for i := 0; i < 2; i++ {
			initAgentDebug(false)
			debugEvent("fixture.append", nil)
			closeAgentDebug()
		}
		return
	}
	id := fmt.Sprintf("fixture-%d-%d", os.Getpid(), time.Now().UnixNano())
	path := filepath.Join(debugDirectory(), id+".log")
	t.Cleanup(func() { os.Remove(path) })
	cmd := exec.Command(os.Args[0], "-test.run=^TestDebugSessionLogAppends$")
	cmd.Env = append(os.Environ(), "RMM_DEBUG_APPEND_FIXTURE=1", "RMM_SESSION_ID="+id)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("append subprocess failed: %v: %s", err, output)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(data), "event=fixture.append") != 2 {
		t.Fatalf("session log did not append: %s", data)
	}
}

func TestDebugDetachUsesTargetSessionID(t *testing.T) {
	previous := os.Args
	os.Args = []string{"agent", "--lateral-detach", "--lateral-session", "target-session"}
	t.Cleanup(func() { os.Args = previous })
	t.Setenv("RMM_SESSION_ID", "source-session")
	if id, err := debugSessionID(); err != nil || id != "target-session" {
		t.Fatalf("detach log identity = %q, %v", id, err)
	}
}

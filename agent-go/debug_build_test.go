package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestSpawnasDebugCompanionSelection(t *testing.T) {
	directory := t.TempDir()
	self := filepath.Join(directory, "normal-agent.exe")
	if got, err := resolveDebugAgentExecutable(self, false); err != nil || got != self {
		t.Fatalf("normal spawn changed: %q %v", got, err)
	}
	if got, err := resolveDebugAgentExecutable(self, true); err == nil || got != "" {
		t.Fatal("missing debug companion fell back to source executable")
	}
	companion := filepath.Join(directory, "minimal-rmm-agent-debug-windows-"+runtime.GOARCH+".exe")
	if err := os.WriteFile(companion, []byte("invalid"), 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := resolveDebugAgentExecutable(self, true); err == nil {
		t.Fatal("non-executable companion accepted")
	}
	if err := os.WriteFile(companion, []byte("MZfixture"), 0700); err != nil {
		t.Fatal(err)
	}
	if got, err := resolveDebugAgentExecutable(self, true); err != nil || got != companion {
		t.Fatalf("debug companion not selected: %q %v", got, err)
	}
	if err := os.Remove(companion); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(companion, 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := resolveDebugAgentExecutable(self, true); err == nil {
		t.Fatal("directory accepted as debug companion")
	}
}

func TestSpawnDebugCommandPreservesExplicitSelection(t *testing.T) {
	cfg := &config{SessionID: "parent-fixture", SpawnDebug: true}
	for _, command := range []string{"__SPAWN__", "__SPAWN__\n" + `{"session_id":"child-fixture"}`} {
		child, _, err := parseSpawnCommand(command, cfg)
		if err != nil || child.SpawnDebug {
			t.Fatalf("debug inherited without selection: %v %#v", err, child)
		}
	}
	child, _, err := parseSpawnCommand("__SPAWN__\n"+`{"session_id":"child-fixture","debug":true}`, cfg)
	if err != nil || !child.SpawnDebug {
		t.Fatalf("explicit debug selection lost: %v %#v", err, child)
	}
	debugCount := 0
	for _, entry := range spawnEnvironment(child, "child-fixture") {
		if strings.HasPrefix(strings.ToUpper(entry), "RMM_DEBUG=") {
			debugCount++
			if entry != "RMM_DEBUG=1" {
				t.Fatalf("debug child environment disabled trace: %q", entry)
			}
		}
	}
	if debugCount != 1 {
		t.Fatalf("debug child has %d debug environment definitions", debugCount)
	}
	if _, _, err := parseSpawnCommand("__SPAWN__\n"+`{"session_id":"child-fixture","debug":"yes"}`, cfg); err == nil {
		t.Fatal("nonboolean debug selection accepted")
	}
}

func TestLateralDebugPayloadAndServiceArguments(t *testing.T) {
	for _, method := range []string{"psexec", "wmiexec", "winrm"} {
		command := "__LATERAL__\n" + `{"method":"` + method + `","host":"lab-host","stage":"fixture-stage","debug":true}`
		payload, err := parseLateralPayload(command)
		if err != nil || !payload.Debug {
			t.Fatalf("%s debug selection lost: %v %#v", method, err, payload)
		}
	}
	for _, debug := range []bool{false, true} {
		command := lateralAgentCommand("agent.exe", "session-fixture", nil, "", debug)
		startup, err := parseLateralStartup(strings.Fields(command)[1:])
		if err != nil || !startup.DebugSet || startup.Debug != debug {
			t.Fatalf("explicit debug startup selection not preserved: %q %+v %v", command, startup, err)
		}
		args := lateralServiceArguments("stage-fixture", "session-fixture", "https://listener.invalid/a path", debug)
		if len(args) != 6 && len(args) != 8 {
			t.Fatalf("invalid service argument count: %q", args)
		}
		if args[5] != "https://listener.invalid/a path" {
			t.Fatalf("listener URL split before Windows quoting: %q", args)
		}
		if debug && (len(args) != 8 || args[6] != "--debug-agent" || args[7] != "1") {
			t.Fatalf("debug stage2 filename selector missing: %q", args)
		}
		if !debug && len(args) != 6 {
			t.Fatalf("normal stage2 unexpectedly selects debug: %q", args)
		}
	}
	for _, value := range []string{"true", "2", ""} {
		if _, err := parseLateralStartup([]string{"--lateral-session", "session-fixture", "--debug-agent", value}); err == nil {
			t.Fatalf("invalid debug selector %q accepted", value)
		}
	}
	if _, err := parseLateralStartup([]string{"--lateral-session", "session-fixture", "--debug-agent", "1", "--debug-agent", "0"}); err == nil {
		t.Fatal("duplicate debug selector accepted")
	}
}

func TestNormalBuildExcludesDebugImplementation(t *testing.T) {
	if testing.Short() {
		t.Skip("binary build boundary check")
	}
	for _, debug := range []bool{false, true} {
		name, tags := "normal.exe", ""
		if debug {
			name, tags = "debug.exe", "rmmdebug"
		}
		path := filepath.Join(t.TempDir(), name)
		cmd := exec.Command("go", "build", "-buildvcs=false", "-tags="+tags, "-o", path, ".")
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("build debug=%t: %v: %s", debug, err, output)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		markers := []string{"minimal-rmm-debug-%d.log", "identity_boundary.failed"}
		if !strings.HasPrefix(runtime.Version(), "go1.21") && !strings.HasPrefix(runtime.Version(), "go1.22") {
			markers = append(markers, "runtime/debug.SetCrashOutput")
		}
		for _, marker := range markers {
			if present := bytes.Contains(data, []byte(marker)); present != debug {
				t.Errorf("debug=%t binary marker %q present=%t", debug, marker, present)
			}
		}
	}
}

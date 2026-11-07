package main

import (
	"bytes"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

func TestDebugBuildReusesRenamedCurrentExecutable(t *testing.T) {
	if !agentDebugBuild {
		t.Skip("diagnostic image fixture")
	}
	current, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if expected := os.Getenv("RMM_DEBUG_REUSE_FIXTURE"); expected != "" {
		if current != expected {
			t.Fatalf("copied debug image did not preserve its executable identity: %q", current)
		}
		if _, _, err := parseSpawnCommand("__SPAWN__", &config{SessionID: "fixture-parent"}); err != nil {
			t.Fatalf("renamed diagnostic image could not prepare a legacy spawn: %v", err)
		}
		return
	}
	data, err := os.ReadFile(current)
	if err != nil {
		t.Fatal(err)
	}
	copyPath := filepath.Join(t.TempDir(), "renamed-debug.exe")
	if err := os.WriteFile(copyPath, data, 0700); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(copyPath, "-test.run=^TestDebugBuildReusesRenamedCurrentExecutable$")
	cmd.Env = append(os.Environ(), "RMM_DEBUG_REUSE_FIXTURE="+copyPath)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("copied diagnostic executable failed without a companion: %v: %s", err, output)
	}
}

func TestDebugRuntimeCommandOverridesRejected(t *testing.T) {
	cfg := &config{SessionID: "parent-fixture"}
	for _, command := range []string{"__SPAWN__", "__SPAWN__\n" + `{"session_id":"child-fixture"}`} {
		if _, _, err := parseSpawnCommand(command, cfg); err != nil {
			t.Fatalf("build-only spawn rejected: %v", err)
		}
	}
	for _, value := range []string{"true", "false", "null", "1", `"yes"`} {
		if _, _, err := parseSpawnCommand("__SPAWN__\n"+`{"session_id":"child-fixture","debug":`+value+`}`, cfg); err == nil {
			t.Fatalf("removed spawn debug option %s accepted", value)
		}
		for _, method := range []string{"psexec", "wmiexec", "winrm"} {
			command := "__LATERAL__\n" + `{"method":"` + method + `","host":"lab-host","stage":"fixture-stage","debug":` + value + `}`
			if _, err := parseLateralPayload(command); err == nil {
				t.Fatalf("removed %s debug option %s accepted", method, value)
			}
		}
	}
}

func TestLateralDebugPayloadAndServiceArguments(t *testing.T) {
	for _, method := range []string{"psexec", "wmiexec", "winrm"} {
		command := "__LATERAL__\n" + `{"method":"` + method + `","host":"lab-host","stage":"fixture-stage"}`
		if _, err := parseLateralPayload(command); err != nil {
			t.Fatalf("%s build-only deployment rejected: %v", method, err)
		}
	}
	command := lateralAgentCommand("agent.exe", "session-fixture", nil, "")
	if strings.Contains(command, "debug") {
		t.Fatalf("target startup still has runtime debug controls: %q", command)
	}
	if _, err := parseLateralStartup(strings.Fields(command)[1:]); err != nil {
		t.Fatal(err)
	}
	args := lateralServiceArguments("stage-fixture", "session-fixture", "https://listener.invalid/a path")
	if len(args) < 6 || args[5] != "https://listener.invalid/a path" {
		t.Fatalf("listener URL split before Windows quoting: %q", args)
	}
	if agentDebugBuild {
		if len(args) != 8 || args[6] != "--debug-stage-filename" || args[7] != "1" {
			t.Fatalf("compiled debug stage2 filename selector missing: %q", args)
		}
	} else if len(args) != 6 {
		t.Fatalf("normal build requested diagnostic stage2 filename: %q", args)
	}
	for _, flags := range [][]string{{"--debug-agent", "1"}, {"--debug-agent", "0"}, {"-d", "1"}, {"--debug-stage-filename", "1"}} {
		if _, err := parseLateralStartup(append([]string{"--lateral-session", "session-fixture"}, flags...)); err == nil {
			t.Fatalf("removed or worker-only debug flags accepted by agent startup: %q", flags)
		}
	}
}

func TestDebugRegistrationReportsCompiledMode(t *testing.T) {
	called := false
	client := &http.Client{Transport: workspaceBindingTransport(func(request *http.Request) (*http.Response, error) {
		called = true
		if got := request.URL.Query().Get("debug_build"); got != strconv.FormatBool(agentDebugBuild) {
			t.Errorf("registration lost compiled mode: %q", got)
		}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader("REGISTERED"))}, nil
	})}
	if err := register(client, &config{BaseURL: "https://fixture.invalid", SessionID: "fixture-session"}); err != nil || !called {
		t.Fatalf("registration failed: called=%t err=%v", called, err)
	}
}

func TestDebugChildEnvironmentOmitsRemovedSwitch(t *testing.T) {
	for _, ambient := range []string{"0", "1"} {
		t.Setenv("RMM_DEBUG", ambient)
		t.Setenv("rmm_debug", ambient)
		for _, entry := range spawnEnvironment(&config{}, "fixture-child") {
			key, _, _ := strings.Cut(entry, "=")
			if strings.EqualFold(key, "RMM_DEBUG") {
				t.Fatalf("removed debug environment switch propagated: %q", entry)
			}
		}
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

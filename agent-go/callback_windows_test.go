//go:build windows

package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

func TestWindowsRegistrationUsesProcessIdentityDespiteInheritedEnvironment(t *testing.T) {
	expected := processUserIdentity()
	t.Setenv("USERNAME", "fixture-parent-user")
	t.Setenv("USER", "fixture-parent-user")
	t.Setenv("USERDOMAIN", "fixture-parent-domain")
	if actual := processUserIdentity(); actual != expected {
		t.Fatalf("process identity depended on inherited environment: got %q want %q", actual, expected)
	}
	called := false
	client := &http.Client{Transport: workspaceBindingTransport(func(request *http.Request) (*http.Response, error) {
		called = true
		if user := request.URL.Query().Get("u"); user != expected || strings.Contains(user, "fixture-parent") {
			t.Fatalf("registration reported inherited parent identity: got %q want %q", user, expected)
		}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader("REGISTERED"))}, nil
	})}
	cfg := &config{BaseURL: "https://beacon.example.test", SessionID: "identity-fixture-session"}
	if err := register(client, cfg); err != nil || !called {
		t.Fatalf("registration failed: called=%t err=%v", called, err)
	}
}

// Resolve every SCM entry point before any service operation can trigger a lazy-load panic.
func TestWindowsSCMProceduresResolve(t *testing.T) {
	for _, proc := range []*syscall.LazyProc{
		procOpenSCManagerW, procOpenServiceW, procCreateServiceW,
		procStartServiceW, procDeleteService, procCloseServiceHandle, procQueryServiceStatus,
	} {
		t.Run(proc.Name, func(t *testing.T) {
			if err := proc.Find(); err != nil {
				t.Fatalf("SCM procedure is unavailable: %v", err)
			}
		})
	}
}

// Exercise the real dedicated C service without fetching or launching any agent.
// No listener URL is supplied, so the worker reports stage exit 2 through SCM.
func TestDedicatedLateralServiceProtocol(t *testing.T) {
	source := os.Getenv("RMM_LATERAL_SERVICE_TEST_EXE")
	if source == "" {
		t.Skip("dedicated C service fixture not configured")
	}
	name := fmt.Sprintf("MinimalRmmAgent-%032x", time.Now().UnixNano())
	path := filepath.Join(t.TempDir(), "service-fixture.exe")
	if err := copyFile(source, path); err != nil {
		t.Fatal(err)
	}
	defer scmDeleteService("", name)
	startErr := scmStartService("", name, buildWindowsCmdLine([]string{path}), false)
	if errors.Is(startErr, syscall.Errno(1053)) {
		t.Fatal("dedicated host did not perform the SCM handshake")
	}
	err := waitStage1Stopped("", name, 10*time.Second)
	if err == nil || !strings.Contains(err.Error(), "Windows 1066, stage 2") {
		t.Fatalf("worker result was not reported through SCM: start=%v stop=%v", startErr, err)
	}
	if _, err := scmDeleteService("", name); err != nil {
		t.Fatal(err)
	}
	if err := removeStoppedLauncher(path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("service executable still exists")
	}
}

// This smoke test uses the current account's token; no alternate credentials
// or remote authentication are needed. It must run on native Windows.
func TestCallbackClearsAndRestoresActualThreadToken(t *testing.T) {
	t.Cleanup(func() { identityBoundaryFault.Store(false) })
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var processToken syscall.Token
	ok, _, err := advapi32.NewProc("OpenProcessToken").Call(^uintptr(0), 0x0008|0x0002, uintptr(unsafe.Pointer(&processToken)))
	if ok == 0 {
		t.Fatal(err)
	}
	defer processToken.Close()
	if ok, _, err = procImpersonateLoggedOnUser.Call(uintptr(processToken)); ok == 0 {
		t.Fatal(err)
	}
	defer procRevertToSelf.Call()
	threadToken := func() (syscall.Token, error) {
		var token syscall.Token
		r, _, callErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
		if r == 0 {
			return 0, callErr
		}
		return token, nil
	}
	transportErr := errors.New("fixture transport failure")
	called := false
	client := &http.Client{Transport: workspaceBindingTransport(func(*http.Request) (*http.Response, error) {
		called = true
		if token, queryErr := threadToken(); queryErr != syscall.Errno(1008) {
			if token != 0 {
				token.Close()
			}
			t.Errorf("transport ran with thread token: %v", queryErr)
		}
		return nil, transportErr
	})}
	_, requestErr := request(client, &config{BaseURL: "http://callback.invalid", SessionID: "callback-test"}, http.MethodGet, "/cmd", nil, nil)
	if !called || !errors.Is(requestErr, transportErr) {
		t.Fatalf("unexpected request outcome: %v", requestErr)
	}
	token, queryErr := threadToken()
	if queryErr != nil {
		t.Fatalf("thread token was not restored: %v", queryErr)
	}
	token.Close()
}

// Revocation must survive every helper that used to reactivate retained tokens.
func TestExplicitRev2selfCannotBeReactivated(t *testing.T) {
	t.Cleanup(func() { identityBoundaryFault.Store(false) })
	if _, err := makeTokenAction(".", "callback-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	if _, err := rev2selfAction(); err != nil {
		t.Fatal(err)
	}
	reestablishThreadToken()
	tokenMu.Lock()
	retained := activeToken != 0 || tokenActive || tokenIdentity != "" || storedIdentity != "" || spawnCredentials.password != ""
	tokenMu.Unlock()
	if retained {
		t.Fatal("explicit rev2self retained identity state")
	}
	command := exec.Command("cmd.exe", "/c", "exit", "0")
	detachSpawnCommand(command)
	applyCommandToken(command)
	if command.SysProcAttr.Token != 0 {
		t.Fatal("revoked token applied to child")
	}
	if _, err := rev2selfAction(); err != nil {
		t.Fatal("revocation is not idempotent")
	}
}

func TestCallbackProxyChallengeDoesNotUseAmbientCredentials(t *testing.T) {
	t.Cleanup(func() { identityBoundaryFault.Store(false) })
	if _, err := makeTokenAction(".", "callback-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	before := callbackThreadAuthenticationID(t)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Proxy-Authorization") != "" {
			t.Error("unexpected ambient proxy authentication")
		}
		w.Header().Set("Proxy-Authenticate", "Negotiate")
		w.WriteHeader(http.StatusProxyAuthRequired)
	}))
	defer proxy.Close()
	cfg := &config{BaseURL: "http://callback.invalid", HTTPProxy: proxy.URL, SessionID: "callback-test"}
	client := newHTTPClient(cfg)
	defer client.CloseIdleConnections()
	_, err := request(client, cfg, http.MethodGet, "/cmd", nil, nil)
	if err == nil || !strings.HasPrefix(err.Error(), "HTTP 407:") {
		t.Fatalf("unexpected proxy response: %v", err)
	}
	if after := callbackThreadAuthenticationID(t); after != before {
		t.Fatal("callback restored a different logon session")
	}
}

// AuthenticationId distinguishes logon sessions even when netonly tokens have
// the same local account SID. TOKEN_STATISTICS starts with two LUID fields.
func callbackThreadAuthenticationID(t *testing.T) [8]byte {
	t.Helper()
	var token syscall.Token
	ok, _, err := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		t.Fatal(err)
	}
	defer token.Close()
	var statistics [56]byte
	var size uint32
	ok, _, err = advapi32.NewProc("GetTokenInformation").Call(uintptr(token), 10,
		uintptr(unsafe.Pointer(&statistics[0])), uintptr(len(statistics)), uintptr(unsafe.Pointer(&size)))
	if ok == 0 {
		t.Fatal(err)
	}
	var id [8]byte
	copy(id[:], statistics[8:16])
	return id
}

// The child is a short-lived test fixture, never a beaconing agent.
func TestWindowsDetachedSpawn(t *testing.T) {
	if marker := os.Getenv("RMM_SPAWN_TEST_MARKER"); marker != "" {
		file, err := os.OpenFile(marker, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			os.Exit(2)
		}
		_, err = file.WriteString("started\n")
		file.Close()
		if err != nil {
			os.Exit(2)
		}
		os.Exit(0)
	}
	t.Cleanup(func() { identityBoundaryFault.Store(false) })
	for _, active := range []bool{false, true} {
		if active {
			if _, err := makeTokenAction(".", "callback-fixture", "unused-fixture-password"); err != nil {
				t.Fatal(err)
			}
			defer rev2selfAction()
		}
		marker := filepath.Join(t.TempDir(), "started.txt")
		executable, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		command := exec.Command(executable, "-test.run=^TestWindowsDetachedSpawn$")
		command.Env = append(os.Environ(), "RMM_SPAWN_TEST_MARKER="+marker)
		detachSpawnCommand(command)
		pid, method, degraded, err := startDetachedAgent(command)
		if err != nil || pid <= 0 || method == "" {
			t.Fatalf("native detached spawn failed: pid=%d method=%s err=%v", pid, method, err)
		}
		if method != "CreateProcessA" || degraded != active {
			t.Fatalf("spawn changed API or failed to report thread-token isolation: %s degraded=%t", method, degraded)
		}
		deadline := time.Now().Add(10 * time.Second)
		for {
			data, readErr := os.ReadFile(marker)
			if readErr == nil && len(data) > 0 {
				if string(data) != "started\n" {
					t.Fatalf("duplicate child launches: %q", data)
				}
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("spawned fixture did not run")
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
}

// Invalid credentials must fail rather than launch a child under the caller.
func TestWindowsExplicitLogonHasNoFallback(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	user := "RMM-NoSuchUser-" + strings.Repeat("x", 64)
	pid, err := spawnWithLogonDetached(user, ".", "invalid-fixture-password", executable,
		[]string{executable, "-test.run=^$"}, os.Environ())
	if pid != 0 || err == nil || !strings.Contains(err.Error(), "CreateProcessWithLogonW:") {
		t.Fatalf("spawnas did not preserve the logon failure: pid=%d err=%v", pid, err)
	}
	output, err := runWithPipesLogonW(context.Background(), user, ".", "invalid-fixture-password",
		[]string{executable, "-test.run=^$"}, buildEnvBlock(os.Environ()))
	if output != "" || err == nil || !strings.Contains(err.Error(), "CreateProcessWithLogonW:") {
		t.Fatalf("runas did not preserve the logon failure: output=%q err=%v", output, err)
	}
}

func TestWindowsLogonCredentialEncoding(t *testing.T) {
	_, domain, _, err := logonCredentialPointers("fixture@example.invalid", "", "unused")
	if err != nil || domain != nil {
		t.Fatalf("UPN requires NULL domain: %p %v", domain, err)
	}
	for _, fields := range [][3]string{{"invalid\x00user", ".", "unused"}, {"fixture", "bad\x00domain", "unused"}, {"fixture", ".", "bad\x00password"}} {
		if _, _, _, err := logonCredentialPointers(fields[0], fields[1], fields[2]); err == nil {
			t.Fatal("embedded NUL was not rejected")
		}
	}
}

// Run the output collector against harmless child fixtures under our current
// account, without alternate credentials or a beaconing process.
func TestWindowsRunasOutputLifecycle(t *testing.T) {
	if mode := os.Getenv("RMM_RUNAS_OUTPUT_FIXTURE"); mode != "" {
		fmt.Fprint(os.Stdout, "fixture stdout\n")
		fmt.Fprint(os.Stderr, "fixture stderr\n")
		switch mode {
		case "nonzero":
			os.Exit(7)
		case "timeout":
			time.Sleep(30 * time.Second)
		case "descendant", "continuous-descendant":
			child := exec.Command(os.Args[0], "-test.run=^TestWindowsRunasOutputLifecycle$")
			childMode := "hold"
			if mode == "continuous-descendant" {
				childMode = "write"
			}
			child.Env = append(os.Environ(), "RMM_RUNAS_OUTPUT_FIXTURE="+childMode)
			child.Stdout, child.Stderr = os.Stdout, os.Stderr
			if err := child.Start(); err != nil {
				os.Exit(3)
			}
			if pidFile := os.Getenv("RMM_RUNAS_WRITER_PIDFILE"); pidFile != "" {
				if err := os.WriteFile(pidFile, []byte(fmt.Sprint(child.Process.Pid)), 0o600); err != nil {
					child.Process.Kill()
					os.Exit(4)
				}
				deadline := time.Now().Add(5 * time.Second)
				for {
					if _, err := os.Stat(pidFile + ".ready"); err == nil {
						break
					}
					if time.Now().After(deadline) {
						child.Process.Kill()
						os.Exit(5)
					}
					time.Sleep(10 * time.Millisecond)
				}
			}
		case "hold":
			time.Sleep(3 * time.Second)
		case "write":
			if err := os.WriteFile(os.Getenv("RMM_RUNAS_WRITER_PIDFILE")+".ready", nil, 0o600); err != nil {
				os.Exit(6)
			}
			chunk := []byte(strings.Repeat("descendant output\n", 1024))
			for {
				if _, err := os.Stdout.Write(chunk); err != nil {
					os.Exit(0)
				}
			}
		}
		os.Exit(0)
	}
	for _, mode := range []string{"success", "nonzero", "timeout", "descendant", "continuous-descendant"} {
		t.Run(mode, func(t *testing.T) {
			readH, writeH, err := createInheritablePipe()
			if err != nil {
				t.Fatal(err)
			}
			defer syscall.CloseHandle(readH)
			outputFile := os.NewFile(uintptr(writeH), "runas-fixture-output")
			defer outputFile.Close()
			command := exec.Command(os.Args[0], "-test.run=^TestWindowsRunasOutputLifecycle$")
			command.Env = append(os.Environ(), "RMM_RUNAS_OUTPUT_FIXTURE="+mode)
			if mode == "continuous-descendant" {
				pidFile := filepath.Join(t.TempDir(), "writer.pid")
				command.Env = append(command.Env, "RMM_RUNAS_WRITER_PIDFILE="+pidFile)
				t.Cleanup(func() {
					data, err := os.ReadFile(pidFile)
					if err != nil {
						return
					}
					var pid uint32
					if _, err := fmt.Sscan(string(data), &pid); err != nil {
						t.Error(err)
						return
					}
					process, err := syscall.OpenProcess(0x00100000|0x0001, false, pid)
					if err == nil {
						defer syscall.CloseHandle(process)
						procTerminateProcess.Call(uintptr(process), 1)
						procWaitSingleObject.Call(uintptr(process), 5000)
					}
				})
			}
			command.Stdout, command.Stderr = outputFile, outputFile
			if err := command.Start(); err != nil {
				t.Fatal(err)
			}
			defer command.Wait()
			defer command.Process.Kill()
			outputFile.Close()
			process, err := syscall.OpenProcess(0x00100000|0x1000|0x0001, false, uint32(command.Process.Pid))
			if err != nil {
				t.Fatal(err)
			}
			defer syscall.CloseHandle(process)
			timeout := 10 * time.Second
			if mode == "timeout" {
				timeout = time.Second
			}
			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()
			started := time.Now()
			output, err := collectRunasOutput(ctx, process, readH)
			if !strings.Contains(output, "fixture stdout") || !strings.Contains(output, "fixture stderr") {
				t.Fatalf("output lost: %q %v", output, err)
			}
			switch mode {
			case "nonzero":
				if err == nil || !strings.Contains(err.Error(), "code 7") {
					t.Fatalf("nonzero exit lost: %v", err)
				}
			case "timeout":
				if !errors.Is(err, context.DeadlineExceeded) {
					t.Fatalf("deadline lost: %v", err)
				}
				if state, _, _ := procWaitSingleObject.Call(uintptr(process), 0); state != 0 {
					t.Fatal("timed out child was not terminated")
				}
			case "descendant":
				if err != nil || time.Since(started) > 2*time.Second {
					t.Fatalf("collector waited for descendant pipe closure: %v", err)
				}
			case "continuous-descendant":
				if err != nil || time.Since(started) > 5*time.Second {
					t.Fatalf("collector followed continuously writing descendant: %v", err)
				}
			default:
				if err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestCallbackSpawnDiagnosticsReadOnly(t *testing.T) {
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		t.Fatal(err)
	}
	defer token.Close()
	if kind := spawnTokenType(token); kind != "primary" {
		t.Fatalf("unexpected process token type: %s", kind)
	}
	states := spawnTokenPrivileges(token)
	for _, name := range []string{"SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege", "SeImpersonatePrivilege"} {
		if states[name] != "enabled" && states[name] != "disabled" && states[name] != "absent" && states[name] != "removed" {
			t.Fatalf("privilege query failed for %s", name)
		}
	}
}

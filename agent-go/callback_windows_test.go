//go:build windows

package main

import (
	"errors"
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
		if !active && (method != "CreateProcessA" || degraded) {
			t.Fatalf("wrong no-token method: %s", method)
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

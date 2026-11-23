//go:build windows

package main

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

func resetPluginIdentitySeams(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		pluginThreadToken = openPluginThreadToken
		pluginPrimaryToken = duplicatePluginPrimaryToken
		pluginStart = (*exec.Cmd).Start
		pluginRestoreToken = restorePluginThreadToken
		identityBoundaryFault.Store(false)
	})
}

func TestPluginIdentityNoToken(t *testing.T) {
	resetPluginIdentitySeams(t)
	pluginThreadToken = func() (syscall.Token, error) { return 0, nil }
	pluginPrimaryToken = func(syscall.Token) (syscall.Token, error) {
		t.Fatal("duplicated a missing token")
		return 0, nil
	}
	cmd := exec.Command("cmd.exe", "/c", "echo", "plugin-process-identity")
	// A stale command token must not override the actual no-token decision.
	shared := &syscall.SysProcAttr{Token: syscall.Token(0xdeadbeef), HideWindow: true}
	cmd.SysProcAttr = shared
	pluginStart = func(cmd *exec.Cmd) error {
		if cmd.SysProcAttr.Token != 0 || !cmd.SysProcAttr.HideWindow {
			t.Fatal("selected identity or unrelated process attributes changed")
		}
		return cmd.Start()
	}
	var out bytes.Buffer
	cmd.Stdout = &out
	if err := runPluginCommand(cmd); err != nil || !strings.Contains(out.String(), "plugin-process-identity") {
		t.Fatalf("output=%q err=%v", out.String(), err)
	}
	if shared.Token != syscall.Token(0xdeadbeef) {
		t.Fatal("launcher mutated shared process attributes")
	}
}

func TestPluginIdentityCaptureFailure(t *testing.T) {
	resetPluginIdentitySeams(t)
	pluginThreadToken = func() (syscall.Token, error) { return 0, syscall.ERROR_ACCESS_DENIED }
	pluginStart = func(*exec.Cmd) error { t.Fatal("started despite capture failure"); return nil }
	if err := runPluginCommand(exec.Command("unused")); !errors.Is(err, syscall.ERROR_ACCESS_DENIED) {
		t.Fatalf("capture error=%v", err)
	}
}

func TestPluginIdentityGlobalStateDoesNotSupplyThreadToken(t *testing.T) {
	resetPluginIdentitySeams(t)
	if _, err := makeTokenAction(".", "plugin-fixture", "unused-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	restore, err := suspendServerIdentity()
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := restore(); err != nil {
			t.Fatal(err)
		}
	}()
	pluginStart = func(cmd *exec.Cmd) error {
		if cmd.SysProcAttr != nil && cmd.SysProcAttr.Token != 0 {
			t.Fatal("global make_token state supplied a missing thread token")
		}
		return cmd.Start()
	}
	if err := runPluginCommand(exec.Command("cmd.exe", "/c", "exit", "0")); err != nil {
		t.Fatal(err)
	}
}

func TestPluginIdentityDuplicateFailure(t *testing.T) {
	resetPluginIdentitySeams(t)
	if _, err := makeTokenAction(".", "plugin-fixture", "unused-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	pluginPrimaryToken = func(syscall.Token) (syscall.Token, error) { return 0, syscall.ERROR_ACCESS_DENIED }
	pluginStart = func(*exec.Cmd) error { t.Fatal("started despite duplication failure"); return nil }
	if err := runPluginCommand(exec.Command("unused")); !errors.Is(err, syscall.ERROR_ACCESS_DENIED) {
		t.Fatalf("duplication error=%v", err)
	}
}

func TestPluginIdentityStartFailureRestoresActualToken(t *testing.T) {
	resetPluginIdentitySeams(t)
	if _, err := makeTokenAction(".", "plugin-fixture", "unused-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	restored, starts := false, 0
	pluginStart = func(cmd *exec.Cmd) error {
		starts++
		if cmd.SysProcAttr == nil || cmd.SysProcAttr.Token == 0 {
			t.Fatal("child lacks primary token")
		}
		// Model an API which changes the calling thread's impersonation.
		if ok, _, err := procRevertToSelf.Call(); ok == 0 {
			t.Fatal(err)
		}
		return syscall.Errno(1314)
	}
	pluginRestoreToken = func(token syscall.Token) error {
		restored = true
		return restorePluginThreadToken(token)
	}
	if err := runPluginCommand(exec.Command("unused")); !errors.Is(err, syscall.Errno(1314)) || starts != 1 || !restored {
		t.Fatalf("err=%v starts=%d restored=%t", err, starts, restored)
	}
	thread, err := openPluginThreadToken()
	if err != nil || thread == 0 {
		t.Fatalf("parent token lost: token=%v err=%v", thread, err)
	}
	thread.Close()
}

// The authentication LUID distinguishes a netonly token from the process token
// even though both have the same local account SID.
func pluginTestAuthID(token syscall.Token) (string, error) {
	var stats [64]byte
	var size uint32
	if err := syscall.GetTokenInformation(token, 10, &stats[0], uint32(len(stats)), &size); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", stats[8:16]), nil
}

func TestPluginIdentityNativeChild(t *testing.T) {
	if os.Getenv("RMM_PLUGIN_IDENTITY_CHILD") == "1" {
		token, err := syscall.OpenCurrentProcessToken()
		if err != nil {
			t.Fatal(err)
		}
		defer token.Close()
		id, err := pluginTestAuthID(token)
		if err != nil {
			t.Fatal(err)
		}
		fmt.Print("plugin-auth-id=" + id)
		return
	}
	resetPluginIdentitySeams(t)
	if _, err := makeTokenAction(".", "plugin-fixture", "unused-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	thread, err := openPluginThreadToken()
	if err != nil {
		t.Fatal(err)
	}
	want, err := pluginTestAuthID(thread)
	thread.Close()
	if err != nil {
		t.Fatal(err)
	}
	// This test intentionally ignores global activeToken: launch must consult
	// the actual thread token rather than the operator-state variables.
	tokenMu.Lock()
	savedToken, savedActive := activeToken, tokenActive
	activeToken, tokenActive = 0, false
	tokenMu.Unlock()
	defer func() {
		tokenMu.Lock()
		activeToken, tokenActive = savedToken, savedActive
		tokenMu.Unlock()
	}()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(exe, "-test.run=^TestPluginIdentityNativeChild$")
	cmd.Env = append(os.Environ(), "RMM_PLUGIN_IDENTITY_CHILD=1")
	pluginStart = func(cmd *exec.Cmd) error {
		got, err := pluginTestAuthID(cmd.SysProcAttr.Token)
		if err != nil || got != want {
			t.Fatalf("prepared child token auth=%s want=%s err=%v", got, want, err)
		}
		return cmd.Start()
	}
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err = runPluginCommand(cmd)
	parent, parentErr := openPluginThreadToken()
	if parentErr != nil || parent == 0 {
		t.Fatalf("capture restored parent token: token=%v err=%v", parent, parentErr)
	}
	got, parentErr := pluginTestAuthID(parent)
	parent.Close()
	if parentErr != nil || got != want {
		t.Fatalf("restored parent auth=%s want=%s err=%v", got, want, parentErr)
	}
	if errors.Is(err, syscall.Errno(1314)) {
		if cmd.Process != nil {
			t.Fatal("child launched despite privilege failure")
		}
		t.Skip("CreateProcessAsUser denied required privilege; native inheritance requires a privileged Windows runner")
	}
	if err != nil || !strings.Contains(out.String(), "plugin-auth-id="+want) {
		t.Fatalf("child auth mismatch: output=%q want=%s err=%v", out.String(), want, err)
	}
}

func TestPluginIdentityRestoreFailureStopsAgent(t *testing.T) {
	if os.Getenv("RMM_PLUGIN_RESTORE_CHILD") == "1" {
		time.Sleep(time.Minute)
		return
	}
	resetPluginIdentitySeams(t)
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	// Keep uncertain-thread retirement confined to a short-lived goroutine.
	done := make(chan error, 1)
	go func() {
		runtime.LockOSThread()
		var source syscall.Token
		ok, _, err := advapi32.NewProc("OpenProcessToken").Call(
			^uintptr(0), 0x0008|0x0002, uintptr(unsafe.Pointer(&source)))
		if ok == 0 {
			done <- err
			return
		}
		pluginThreadToken = func() (syscall.Token, error) { return source, nil }
		pluginRestoreToken = func(syscall.Token) error { return syscall.ERROR_ACCESS_DENIED }
		cmd := exec.Command(exe, "-test.run=^TestPluginIdentityRestoreFailureStopsAgent$")
		cmd.Env = append(os.Environ(), "RMM_PLUGIN_RESTORE_CHILD=1")
		// Avoid real privilege dependencies while still exercising kill/wait.
		pluginStart = func(cmd *exec.Cmd) error { cmd.SysProcAttr.Token = 0; return cmd.Start() }
		err = runPluginCommand(cmd)
		if cmd.ProcessState == nil {
			done <- errors.New("child was not reaped")
			return
		}
		done <- err
	}()
	if err := <-done; !errors.Is(err, errIdentityUnsafe) || !identityBoundaryFault.Load() {
		t.Fatalf("restore failure did not stop agent: %v", err)
	}
}

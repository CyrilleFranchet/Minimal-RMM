//go:build windows

package main

import (
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"
)

// The dial must open a synchronous, exclusive pipe client: read and write
// access on an existing instance, with no FILE_FLAG_OVERLAPPED — Go's
// os.OpenFile adds that flag and then writes with a null OVERLAPPED, which
// named pipes reject with ERROR_INVALID_PARAMETER (the failure observed in
// the field). Read and write must stay on the same synchronous client, with
// pipe-close errors mapping to io.EOF.
func TestWmiLoaderDialParameters(t *testing.T) {
	for name, proc := range map[string]*syscall.LazyProc{
		"CreateFileW": procLoaderCreateFileW,
		"WriteFile":   procLoaderWriteFile,
		"ReadFile":    procLoaderReadFile,
		"CloseHandle": procLoaderCloseHandle,
	} {
		if err := proc.Find(); err != nil {
			t.Fatalf("%s must resolve: %v", name, err)
		}
	}
	if wmiLoaderGenericRead != 0x80000000 || wmiLoaderGenericWrite != 0x40000000 {
		t.Fatal("the pipe client must request read and write access")
	}
	if wmiLoaderOpenExisting != 3 {
		t.Fatal("the pipe client must never create a pipe instance")
	}
	if wmiLoaderFileAttributeNormal != 0x80 {
		t.Fatal("the pipe client must stay synchronous: no FILE_FLAG_OVERLAPPED")
	}
	if wmiLoaderErrorNotFound != syscall.Errno(2) || wmiLoaderErrorPipeBusy != syscall.Errno(231) {
		t.Fatal("the dial retry cases must match the documented pipe errors")
	}
	if wmiLoaderErrorBrokenPipe != syscall.Errno(109) || wmiLoaderErrorNoData != syscall.Errno(232) {
		t.Fatal("pipe closure must map the documented broken-pipe errors to io.EOF")
	}
}

// End-to-end channel regression on a real Windows host: dial the actual
// receiver script through the native CreateFileW client and stream a framed
// transfer whose payload is not a PE, so the receiver fails at mapping and
// reports the reason back through the pipe. This exercises the dial, the
// stream copy, the script runtime, Add-Type compilation, and the digest
// checks — the error reply path — without loading an agent image.
func TestWmiLoaderDialRunsLocalReceiver(t *testing.T) {
	powershell := stage1Powershell(t)
	transfer, err := prepareWinRMTransferFromBytes([]byte("not a valid PE image bytes"))
	if err != nil {
		t.Fatalf("prepare transfer: %v", err)
	}
	session := "srv-local-loader-dial-001"
	pipeName := wmiLoaderPipeName(session)
	script := wmiLoaderScript(pipeName, session, nil, transfer)
	receiver := exec.Command(powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
		"-EncodedCommand", winrmEncodedCommand(script))
	if err := receiver.Start(); err != nil {
		t.Fatalf("start the local receiver: %v", err)
	}
	deadline := time.Now().Add(120 * time.Second)
	// "." routes the pipe dial to the local machine, exercising the same
	// native CreateFileW client the remote deployment uses.
	conn, err := dialWmiLoaderPipe(".", pipeName, deadline)
	if err != nil {
		t.Fatalf("dial the local receiver pipe natively: %v", err)
	}
	if err := wmiCopyWithDeadline(conn, transfer.source, deadline); err != nil {
		t.Fatalf("stream the framed transfer to the receiver: %v", err)
	}
	replyErr := wmiReadLoaderReply(conn, deadline)
	conn.Close()
	if replyErr == nil {
		_ = receiver.Wait()
		t.Fatal("a non-PE payload must fail the receiver, not acknowledge readiness")
	}
	if !strings.Contains(replyErr.Error(), "agent image mapping failed") {
		t.Fatalf("unexpected receiver reply: %v", replyErr)
	}
	if waitErr := receiver.Wait(); waitErr != nil {
		t.Fatalf("the receiver must exit after the reported failure: %v", waitErr)
	}
}

// The dial must keep retrying while the receiver has not created the pipe
// yet, and the retry cases must be the documented pipe-only errors.
func TestWmiLoaderDialReportsUnreachablePipe(t *testing.T) {
	deadline := time.Now().Add(500 * time.Millisecond)
	_, err := dialWmiLoaderPipe(".", "rmm-loader-missing-pipe-001", deadline)
	if err == nil || !strings.Contains(err.Error(), "did not open before the deadline") {
		t.Fatalf("a missing pipe must fail against the dial deadline: %v", err)
	}
}

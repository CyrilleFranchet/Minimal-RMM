package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"io"
	"net"
	"strings"
	"testing"
	"time"
)

func wmiTestTransfer(t *testing.T, agent []byte) *winrmTransfer {
	t.Helper()
	transfer, err := prepareWinRMTransferFromBytes(agent)
	if err != nil {
		t.Fatalf("prepare transfer: %v", err)
	}
	return transfer
}

// The WMI receiver must reproduce the WinRM receiver contract: framed loader
// half, digest check, framed agent half, readiness marker, and the same
// PowerShell hosting and environment setup.
func TestWmiLoaderScriptComposition(t *testing.T) {
	agent := []byte("MZ fake agent payload")
	transfer := wmiTestTransfer(t, agent)
	session := "srv-wmi-target-001"
	workspace := "alice-ws"
	pipeName := wmiLoaderPipeName(session)
	script := wmiLoaderScript(pipeName, session, &workspace, transfer)
	if !strings.Contains(script, winrmPowerShellLiteral(pipeName)) {
		t.Fatal("the script must name the session-specific pipe")
	}
	if !strings.Contains(script,
		fmt.Sprintf("[System.IO.Pipes.PipeOptions]::None,%d,%d)", wmiLoaderPipeBufferSize, wmiLoaderPipeBufferSize)) {
		t.Fatal("the receiver must create the pipe with explicit byte-mode buffers")
	}
	if !strings.Contains(script, fmt.Sprintf("[L]::Setup(%s,%s)",
		winrmPowerShellLiteral(session), winrmPowerShellLiteral(workspace))) {
		t.Fatal("the script must set the target session and workspace")
	}
	if !strings.Contains(script, "Add-Type -TypeDefinition") {
		t.Fatal("the script must compile the loader with Add-Type")
	}
	if !strings.Contains(script, "[L]::ReceiveDuplex(") {
		t.Fatal("the script must receive the agent half over the duplex pipe")
	}
	if !strings.Contains(script, winrmPowerShellLiteral(winrmLoaderReadyMarker)) {
		t.Fatal("the script must write the loader readiness marker")
	}
	if !strings.Contains(script, "[L]::Run($e)") {
		t.Fatal("the script must block on the deployed agent thread")
	}
	if !strings.Contains(script, "WaitForConnectionAsync") {
		t.Fatal("the receiver must bound its pipe wait")
	}
	if strings.Contains(script, "`") {
		t.Fatal("the script must stay compatible with a Go raw string")
	}
	if len(strings.Split(script, "\n")) > 60 {
		t.Fatalf("the bootstrap must stay small, got %d lines", len(strings.Split(script, "\n")))
	}
}

// wmiLoaderArguments must produce the encoded-command shape the receiver
// expects and stay far below the Win32_Process.Create command-line budget.
func TestWmiLoaderArgumentsAndCommandLength(t *testing.T) {
	transfer := wmiTestTransfer(t, []byte("MZ fake agent payload"))
	script := wmiLoaderScript("rmm-loader-srv-wmi-target-001", "srv-wmi-target-001", nil, transfer)
	arguments := wmiLoaderArguments(script)
	if arguments[0] != "-NoLogo" || arguments[3] != "-EncodedCommand" {
		t.Fatalf("unexpected receiver arguments: %#v", arguments)
	}
	if len(arguments[4]) < 100 {
		t.Fatal("the encoded command must carry the bootstrap")
	}
	// Estimate the full command line including quoting and separators.
	estimated := len(winrmReceiverCommandPath) + 2
	for _, argument := range append([]string{winrmReceiverCommandPath}, arguments...) {
		estimated += len(argument) + 1
	}
	if estimated > 16000 {
		t.Fatalf("the receiver command must stay well below the 32767-character budget: %d", estimated)
	}
}

func TestWmiLoaderPipeNameIsSessionScoped(t *testing.T) {
	for _, session := range []string{"srv-target-001", "srv-target-002"} {
		name := wmiLoaderPipeName(session)
		if !strings.HasPrefix(name, "rmm-loader-"+session) {
			t.Fatalf("pipe name must derive from the session, got %q", name)
		}
	}
}

// The receiver contract over a duplex channel: the sender streams the exact
// framed halves, the receiver verifies digests, and only the marker accepts
// the deployment.
func TestWmiLoaderChannelContract(t *testing.T) {
	agent := []byte("MZ fake agent payload for the channel contract")
	transfer := wmiTestTransfer(t, agent)
	client, serverConn := net.Pipe()
	defer client.Close()
	receiver := func(conn net.Conn) {
		defer conn.Close()
		loader := make([]byte, transfer.loaderSize)
		if _, err := io.ReadFull(conn, loader); err != nil {
			return
		}
		agentHalf := make([]byte, transfer.binarySize)
		if _, err := io.ReadFull(conn, agentHalf); err != nil {
			return
		}
		want := sha256.Sum256(agentHalf)
		if fmt.Sprintf("%x", want) != transfer.binaryDigest ||
			!bytes.Equal(agentHalf, agent) ||
			!bytes.Equal(loader, []byte(winrmStage1CSharp)) {
			_, _ = conn.Write([]byte(wmiLoaderErrorPrefix + "channel digest mismatch\n"))
			return
		}
		_, _ = conn.Write([]byte(winrmLoaderReadyMarker + "\n"))
		blockConn := make(chan struct{})
		<-blockConn
	}
	go receiver(serverConn)
	deadline := time.Now().Add(5 * time.Second)
	if err := wmiCopyWithDeadline(client, transfer.source, deadline); err != nil {
		t.Fatalf("stream the transfer: %v", err)
	}
	if err := wmiReadLoaderReply(client, deadline); err != nil {
		t.Fatalf("wait for the readiness marker: %v", err)
	}
}

func TestWmiReadLoaderReplyRejectsFailures(t *testing.T) {
	marker := winrmLoaderReadyMarker + "\n"
	cases := []struct {
		name  string
		write string
		close bool
		want  string
	}{
		{"marker", marker, false, ""},
		{"error line", wmiLoaderErrorPrefix + "loader SHA256 mismatch\n", true, "the target loader failed: loader SHA256 mismatch"},
		{"bare error", wmiLoaderErrorPrefix + "\n", true, "failed without a reason"},
		{"garbage", "unexpected output\n", true, "unexpected loader reply"},
		{"early close", "", true, "closed the loader channel before readiness"},
	}
	for _, testCase := range cases {
		client, serverConn := net.Pipe()
		go func(testCase struct {
			name  string
			write string
			close bool
			want  string
		}) {
			if testCase.write != "" {
				_, _ = serverConn.Write([]byte(testCase.write))
			}
			if testCase.close {
				serverConn.Close()
			} else {
				block := make(chan struct{})
				<-block
			}
		}(testCase)
		err := wmiReadLoaderReply(client, time.Now().Add(2*time.Second))
		client.Close()
		if testCase.want == "" {
			if err != nil {
				t.Fatalf("%s: unexpected error: %v", testCase.name, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), testCase.want) {
			t.Fatalf("%s: want %q, got %v", testCase.name, testCase.want, err)
		}
	}
}

// A stalled receiver must not pin the worker past the transfer deadline; the
// copy fails and the caller's close aborts the target reader.
func TestWmiCopyWithDeadlineBoundsStalledReceivers(t *testing.T) {
	client, serverConn := net.Pipe()
	defer client.Close()
	defer serverConn.Close()
	err := wmiCopyWithDeadline(client, strings.NewReader("payload"), time.Now().Add(100*time.Millisecond))
	if err == nil || !strings.Contains(err.Error(), "deadline") {
		t.Fatalf("a stalled channel must fail the deadline: %v", err)
	}
}

// Every native write must stay within one chunk: an oversized single write
// to a remote pipe is the ERROR_INVALID_PARAMETER the live deployment hit.
// The fragments must reassemble into the exact framed transfer.
func TestWmiCopyFragmentsWritesWithinTheChunkBudget(t *testing.T) {
	agent := bytes.Repeat([]byte("agent-image-bytes"), 2000) // ~30 KiB payload
	transfer := wmiTestTransfer(t, agent)
	recorder := &recordingWriter{}
	if err := wmiCopyWithDeadline(recorder, transfer.source, time.Now().Add(5*time.Second)); err != nil {
		t.Fatalf("bounded transfer must succeed: %v", err)
	}
	if recorder.largest > wmiLoaderPipeChunkSize {
		t.Fatalf("a single write reached %d bytes; every write must stay within %d",
			recorder.largest, wmiLoaderPipeChunkSize)
	}
	want := append(append([]byte(nil), []byte(winrmStage1CSharp)...), agent...)
	if !bytes.Equal(recorder.buf, want) {
		t.Fatal("fragmented writes must reassemble into the framed loader and artifact")
	}
}

type recordingWriter struct {
	buf     []byte
	largest int
}

func (r *recordingWriter) Write(p []byte) (int, error) {
	if len(p) > r.largest {
		r.largest = len(p)
	}
	r.buf = append(r.buf, p...)
	return len(p), nil
}

func TestWmiReadLoaderReplyTimesOut(t *testing.T) {
	client, serverConn := net.Pipe()
	defer client.Close()
	defer serverConn.Close()
	err := wmiReadLoaderReply(client, time.Now().Add(100*time.Millisecond))
	if err == nil || !strings.Contains(err.Error(), "exceeded the deployment deadline") {
		t.Fatalf("a silent receiver must hit the deadline: %v", err)
	}
}

package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

// wmiLoaderErrorPrefix introduces the receiver's failure line on the loader
// channel. The WMI-started PowerShell process has no interactive stdout, so
// the duplex pipe doubles as the error path: a receiver that fails before
// readiness reports why and exits, and the sender surfaces the reason.
const wmiLoaderErrorPrefix = "RMM_LOADER_ERROR: "

// wmiLoaderConnectBudget bounds how long the target's receiver script waits
// for the sending agent to connect. The sender's own transfer deadline keeps
// the operator side bounded; this keeps a disconnected deployment from
// leaving a stuck listener behind on the target.
const wmiLoaderConnectBudget = 120 * time.Second

// wmiLoaderReplyLimit bounds a single receiver reply on the channel.
const wmiLoaderReplyLimit = 512

// wmiLoaderPipeChunkSize bounds each native WriteFile. io.Copy's default
// emits the first 32 KiB in one call, and remote named pipes reject single
// writes beyond the pipe's buffer with ERROR_INVALID_PARAMETER on the SMB
// redirector; local pipes split internally, which is why the local fixture
// passes where the live remote deployment failed. Wire protocols over SMB
// pipes use fragments of this order.
const wmiLoaderPipeChunkSize = 4 * 1024

// wmiLoaderPipeBufferSize is the explicit in/out buffer requested for the
// receiver's pipe instead of the three-argument constructor's defaults.
// A larger server buffer keeps the redirector's write window generous.
const wmiLoaderPipeBufferSize = 65536

// wmiLoaderPipeName derives the session-specific loader channel name. The
// reserved target session identifier makes concurrent deployments use
// distinct channels, mirroring the stage2 filename collision fix.
func wmiLoaderPipeName(sessionID string) string {
	return "rmm-loader-" + sessionID
}

// wmiLoaderScript builds the WMI receiver payload that runs as an
// -EncodedCommand of the target's Windows PowerShell. It performs the same
// contract as the WinRM receiver — framed loader half, digest checks,
// Add-Type compilation, PE mapping, readiness marker, and a block on the
// agent thread — but over a duplex named pipe instead of the shell stdin:
// the pipe is a kernel object, so the deployment writes nothing to any disk.
// The receiver reports failures through the pipe instead of stdout because
// WMI execution offers no output channel.
func wmiLoaderScript(pipeName, sessionID string, workspaceID *string, transfer *winrmTransfer) string {
	workspace := "$null"
	if workspaceID != nil {
		workspace = winrmPowerShellLiteral(*workspaceID)
	}
	connectBudget := int(wmiLoaderConnectBudget.Milliseconds())
	return fmt.Sprintf(`$ErrorActionPreference='Stop'
$p=New-Object System.IO.Pipes.NamedPipeServerStream(%s,[System.IO.Pipes.PipeDirection]::InOut,1,[System.IO.Pipes.PipeTransmissionMode]::Byte,[System.IO.Pipes.PipeOptions]::None,%d,%d)
try{
$w=$p.WaitForConnectionAsync()
if(-not $w.Wait(%d)){throw 'loader pipe connection timeout'}
$l=New-Object byte[] %d
$o=0
while($o -lt %d){$n=$p.Read($l,$o,%d-$o);if($n -le 0){throw 'loader input is truncated'};$o+=$n}
$h=[Security.Cryptography.SHA256]::Create()
try{$a=[BitConverter]::ToString($h.ComputeHash($l)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
if($a -ne %s){throw 'loader SHA256 mismatch'}
Add-Type -TypeDefinition ([Text.Encoding]::UTF8.GetString($l))
[L]::Setup(%s,%s)
$b=[L]::ReceiveDuplex($p,%d,%s)
$e=[L]::Map($b)
if($e -eq [IntPtr]::Zero){throw 'agent image mapping failed'}
$m=[Text.Encoding]::ASCII.GetBytes(%s)
$p.Write($m,0,$m.Length)
$p.WriteByte(10)
$p.Flush()
[L]::Run($e)
} catch {
$t=[regex]::Replace(([string]$_.Exception.Message),'[\r\n]+',' ').Trim()
if($t.Length -gt 200){$t=$t.Substring(0,200)}
try{
if($p.IsConnected){
$x=[Text.Encoding]::ASCII.GetBytes('%s'+$t+([char]10))
$p.Write($x,0,$x.Length)
}
} catch {}
exit 1
}`,
		winrmPowerShellLiteral(pipeName), wmiLoaderPipeBufferSize, wmiLoaderPipeBufferSize, connectBudget,
		transfer.loaderSize,
		transfer.loaderSize,
		transfer.loaderSize,
		winrmPowerShellLiteral(transfer.loaderDigest),
		winrmPowerShellLiteral(sessionID), workspace,
		transfer.binarySize, winrmPowerShellLiteral(transfer.binaryDigest),
		winrmPowerShellLiteral(winrmLoaderReadyMarker),
		wmiLoaderErrorPrefix)
}

// wmiLoaderArguments wraps a receiver script for PowerShell's
// -EncodedCommand parameter, matching the WinRM receiver's command shape.
func wmiLoaderArguments(script string) []string {
	return []string{"-NoLogo", "-NoProfile", "-NonInteractive",
		"-EncodedCommand", winrmEncodedCommand(script)}
}

// chunkedPipeWriter bounds every native WriteFile to wmiLoaderPipeChunkSize:
// one oversized write to a remote pipe is what the SMB redirector rejects
// with ERROR_INVALID_PARAMETER, so the stream copy emits small fragments
// the way wire protocols over SMB pipes always have.
type chunkedPipeWriter struct {
	w io.Writer
}

func (c chunkedPipeWriter) Write(p []byte) (int, error) {
	total := 0
	for total < len(p) {
		end := total + wmiLoaderPipeChunkSize
		if end > len(p) {
			end = len(p)
		}
		n, err := c.w.Write(p[total:end])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

// wmiCopyWithDeadline streams the framed transfer into the loader channel,
// in bounded fragments, limiting the whole write sequence by the remaining
// transfer deadline. A stalled receiver cannot pin the pinned worker
// forever; the caller closes the pipe on failure, which also aborts the
// target's reader so no half-live host process survives an interrupted
// delivery.
func wmiCopyWithDeadline(conn io.Writer, source io.Reader, deadline time.Time) error {
	result := make(chan error, 1)
	go func() {
		_, err := io.Copy(chunkedPipeWriter{w: conn}, source)
		result <- err
	}()
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case err := <-result:
		if err != nil {
			return fmt.Errorf("stream loader payload to the target: %w", err)
		}
		return nil
	case <-timer.C:
		return errors.New("loader channel transfer exceeded the deployment deadline")
	}
}

// wmiReadLoaderReply reads the receiver's single readiness line. Only the
// marker accepts the deployment; a receiver error line carries the reason,
// and a closed channel before any reply is an interrupted delivery.
func wmiReadLoaderReply(conn io.Reader, deadline time.Time) error {
	type loaderReply struct {
		line string
		err  error
	}
	result := make(chan loaderReply, 1)
	go func() {
		reader := bufio.NewReader(io.LimitReader(conn, wmiLoaderReplyLimit))
		line, err := reader.ReadString('\n')
		result <- loaderReply{strings.TrimRight(line, "\r\n"), err}
	}()
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case reply := <-result:
		if reply.err != nil && reply.line == "" {
			return fmt.Errorf("the target closed the loader channel before readiness: %w", reply.err)
		}
		switch {
		case reply.line == winrmLoaderReadyMarker:
			return nil
		case strings.HasPrefix(reply.line, wmiLoaderErrorPrefix):
			detail := strings.TrimSpace(strings.TrimPrefix(reply.line, wmiLoaderErrorPrefix))
			if detail == "" {
				return errors.New("the target loader failed without a reason")
			}
			return fmt.Errorf("the target loader failed: %s", detail)
		default:
			return fmt.Errorf("unexpected loader reply %q", reply.line)
		}
	case <-timer.C:
		return errors.New("the loader readiness wait exceeded the deployment deadline")
	}
}

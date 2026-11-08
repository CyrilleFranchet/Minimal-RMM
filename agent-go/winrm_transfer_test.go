package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

// The original regression occurred in the dispatcher before WSMan was called.
// Keep SMB staging scoped to the WMI case, including the shared preamble.
func TestWinRMDispatchNeverStagesOverSMB(t *testing.T) {
	file, err := parser.ParseFile(token.NewFileSet(), "lateral_windows.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var dispatcher *ast.FuncDecl
	for _, declaration := range file.Decls {
		if function, ok := declaration.(*ast.FuncDecl); ok && function.Name.Name == "lateralMoveWithProgress" {
			dispatcher = function
		}
	}
	if dispatcher == nil {
		t.Fatal("missing lateral dispatcher")
	}
	var methodSwitch *ast.SwitchStmt
	ast.Inspect(dispatcher, func(node ast.Node) bool {
		if statement, ok := node.(*ast.SwitchStmt); ok {
			if selector, ok := statement.Tag.(*ast.SelectorExpr); ok && selector.Sel.Name == "Method" {
				methodSwitch = statement
				return false
			}
		}
		if call, ok := node.(*ast.CallExpr); ok {
			if name, ok := call.Fun.(*ast.Ident); ok && name.Name == "copyFile" {
				t.Error("SMB copy in shared lateral preamble")
			}
		}
		return true
	})
	if methodSwitch == nil {
		t.Fatal("missing method dispatch")
	}
	wmiCopies, winrmDeployments := 0, 0
	for _, statement := range methodSwitch.Body.List {
		branch := statement.(*ast.CaseClause)
		method := ""
		if len(branch.List) == 1 {
			if literal, ok := branch.List[0].(*ast.BasicLit); ok {
				method = strings.Trim(literal.Value, `"`)
			}
		}
		ast.Inspect(branch, func(node ast.Node) bool {
			if call, ok := node.(*ast.CallExpr); ok {
				if name, ok := call.Fun.(*ast.Ident); ok {
					switch name.Name {
					case "copyFile":
						if method != "wmiexec" {
							t.Error("SMB copy outside WMI branch")
						}
						wmiCopies++
					case "winrmDeployAgent":
						if method != "winrm" {
							t.Error("WinRM deployment routed to wrong method")
						}
						winrmDeployments++
					}
				}
			}
			return true
		})
	}
	if wmiCopies != 1 || winrmDeployments != 1 {
		t.Fatalf("expected one WMI copy and one WinRM deployment: %d %d", wmiCopies, winrmDeployments)
	}
}

func TestPrepareWinRMTransfer(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agent.exe")
	want := bytes.Repeat([]byte{0, 1, 127, 128, 255}, 10000)
	if err := os.WriteFile(path, want, 0600); err != nil {
		t.Fatal(err)
	}
	source, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer source.Close()
	transfer, err := prepareWinRMTransfer(source)
	if err != nil {
		t.Fatal(err)
	}
	binaryDigest := sha256.Sum256(want)
	loaderDigest := sha256.Sum256([]byte(winrmStage1CSharp))
	if transfer.binarySize != int64(len(want)) || transfer.binaryDigest != fmt.Sprintf("%x", binaryDigest) {
		t.Fatalf("wrong binary manifest: %+v", transfer)
	}
	if transfer.loaderSize != int64(len(winrmStage1CSharp)) || transfer.loaderDigest != fmt.Sprintf("%x", loaderDigest) {
		t.Fatalf("wrong loader manifest: %+v", transfer)
	}
	if transfer.size != transfer.loaderSize+transfer.binarySize {
		t.Fatalf("framed size %d must cover loader and binary", transfer.size)
	}
	got := make([]byte, transfer.size)
	if n, err := io.ReadFull(transfer.source, got); err != nil || n != int(transfer.size) {
		t.Fatal("framed source was not readable end to end")
	}
	if !bytes.Equal(got[:transfer.loaderSize], []byte(winrmStage1CSharp)) || !bytes.Equal(got[transfer.loaderSize:], want) {
		t.Fatal("framed source must be the loader followed by the executable")
	}
	for _, size := range []int64{0, winrmTransferMaxSize + 1} {
		file, err := os.Create(filepath.Join(t.TempDir(), "invalid.exe"))
		if err != nil {
			t.Fatal(err)
		}
		if err := file.Truncate(size); err != nil {
			t.Fatal(err)
		}
		_, err = prepareWinRMTransfer(file)
		file.Close()
		if err == nil {
			t.Fatalf("accepted size %d", size)
		}
	}
}

func TestWinRMEncodedCommand(t *testing.T) {
	want := "Write-Output 'hello 🔒'"
	encoded, err := base64.StdEncoding.DecodeString(winrmEncodedCommand(want))
	if err != nil {
		t.Fatal(err)
	}
	wide := make([]uint16, len(encoded)/2)
	for i := range wide {
		wide[i] = binary.LittleEndian.Uint16(encoded[2*i:])
	}
	if got := string(utf16.Decode(wide)); got != want {
		t.Fatalf("bad UTF-16LE encoding: %q", got)
	}
}

// The loader source travels as framed stdin data and is passed to Add-Type
// after UTF-8 decoding, so it must stay valid UTF-8 without a BOM.
func TestWinRMStage1CSharpIsAscii(t *testing.T) {
	for _, r := range winrmStage1CSharp {
		if r > 127 {
			t.Fatalf("stage1 C# contains non-ASCII rune %q", r)
		}
	}
	if strings.Contains(winrmStage1CSharp, "`") {
		t.Fatal("stage1 C# contains a backtick")
	}
}

func TestWinRMStage1ScriptComposition(t *testing.T) {
	owner := "alice.workspace"
	transfer := &winrmTransfer{
		loaderSize: 1234, loaderDigest: strings.Repeat("ab", 32),
		binarySize: 5678, binaryDigest: strings.Repeat("cd", 32),
	}
	script := winrmStage1Script("srv-target-1", &owner, transfer)
	for _, want := range []string{
		"$l=New-Object byte[] 1234",
		"if($a -ne '" + strings.Repeat("ab", 32) + "')",
		"Add-Type -TypeDefinition ([Text.Encoding]::UTF8.GetString($l))",
		"[L]::Setup('srv-target-1','alice.workspace')",
		"$b=[L]::Receive($s,5678,'" + strings.Repeat("cd", 32) + "')",
		"[L]::Map($b)",
		"[L]::Run($e)",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("stage1 script missing %q", want)
		}
	}
	cleared := ""
	if script := winrmStage1Script("srv-target-1", &cleared, transfer); !strings.Contains(script, "[L]::Setup('srv-target-1','')") {
		t.Fatal("cleared workspace binding must be passed as an empty string")
	}
	if script := winrmStage1Script("srv-target-1", nil, transfer); !strings.Contains(script, "[L]::Setup('srv-target-1',$null)") {
		t.Fatal("absent workspace binding must be passed as $null")
	}
}

// WinRS' cmd.exe launch path has the stricter 8191-character command limit.
// Assert the receiver command fits with worst-case literal lengths.
func TestWinRMReceiverCommandLength(t *testing.T) {
	transfer := &winrmTransfer{
		loaderSize: int64(len(winrmStage1CSharp)), loaderDigest: strings.Repeat("e", 64),
		binarySize: winrmTransferMaxSize, binaryDigest: strings.Repeat("f", 64),
	}
	arguments := winrmReceiverArguments(strings.Repeat("s", 128), nil, transfer)
	command := winrmReceiverCommandPath + " " + strings.Join(arguments, " ")
	if len(command) > 8191 {
		t.Fatalf("receiver command exceeds the WinRS command-line limit: %d", len(command))
	}
}

func stage1Powershell(t *testing.T) string {
	t.Helper()
	executable := "pwsh"
	if runtime.GOOS == "windows" {
		executable = "powershell.exe"
	}
	powershell, err := exec.LookPath(executable)
	if err != nil {
		t.Skip(executable + " unavailable; native Windows WinRM validation remains required")
	}
	return powershell
}

// Exercise the generated receiver against a harmless local payload. Only the
// failure paths are portable: a valid PE mapping needs Windows kernel calls.
func TestWinRMStage1RuntimeFailures(t *testing.T) {
	powershell := stage1Powershell(t)
	loader := []byte(winrmStage1CSharp)
	loaderDigest := fmt.Sprintf("%x", sha256.Sum256(loader))
	binary := bytes.Repeat([]byte{0x90}, 4096)
	for _, scenario := range []string{"loader_truncated", "loader_corrupted", "binary_truncated", "hash_mismatch", "oversized", "not_a_pe"} {
		t.Run(scenario, func(t *testing.T) {
			transfer := &winrmTransfer{
				loaderSize: int64(len(loader)), loaderDigest: loaderDigest,
				binarySize: int64(len(binary)), binaryDigest: fmt.Sprintf("%x", sha256.Sum256(binary)),
			}
			input := append(append([]byte(nil), loader...), binary...)
			switch scenario {
			case "loader_truncated":
				input = input[:len(loader)-100]
			case "loader_corrupted":
				transfer.loaderDigest = strings.Repeat("0", 64)
			case "binary_truncated":
				input = input[:len(input)-100]
			case "hash_mismatch":
				transfer.binaryDigest = strings.Repeat("0", 64)
			case "oversized":
				input = append(input, 1)
			case "not_a_pe":
				// Digest-matching non-PE bytes: the loader must reject the
				// image before any native allocation, which keeps this
				// scenario portable off Windows.
			}
			ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()
			command := exec.CommandContext(ctx, powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
				"-EncodedCommand", winrmEncodedCommand(winrmStage1Script("runtime-fixture", nil, transfer)))
			command.Stdin = bytes.NewReader(input)
			output, err := command.CombinedOutput()
			if err == nil {
				t.Fatalf("%s must fail: %s", scenario, output)
			}
			if scenario == "not_a_pe" {
				if !strings.Contains(string(output), "agent image mapping failed") {
					t.Fatalf("non-PE payload must fail at mapping, got: %s", output)
				}
			}
		})
	}
}

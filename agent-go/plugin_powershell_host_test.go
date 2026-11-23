package main

import (
	"errors"
	"net/http"
	"strings"
	"testing"
)

// loadPluginPE runs one-shot plugins in a sacrificial child from every
// hosting mode: an on-disk session keeps the copy-of-self child loader,
// while a memory-hosted session passes the hosting guard and reaches the
// PowerShell child loader. On the host, both paths end at the platform
// stub, which pins the decision without executing anything.
func TestLoadPluginPEChoosesTheChildHost(t *testing.T) {
	cfg := &config{BaseURL: "http://127.0.0.1:1", BeaconSecret: "s", SessionID: "go-test"}
	_, err := loadPluginPE(&http.Client{}, cfg, "/tools/plugins/test.dll", "Run", "")
	if err == nil {
		t.Skip("only the platform boundary ends the host run")
	}
	if errors.Is(err, errMemoryHosted) {
		t.Fatal("an on-disk session must never hit the memory-hosted guard")
	}
	if want := "PE plugins require the Windows agent build"; err.Error() != want {
		t.Fatalf("unexpected disk-path error: %v", err)
	}

	t.Setenv(memoryHostedEnv, "1")
	_, err = loadPluginPE(&http.Client{}, cfg, "/tools/plugins/test.dll", "Run", "")
	if err == nil || errors.Is(err, errMemoryHosted) {
		t.Fatalf("the memory-hosted path must exist without the hosting guard: %v", err)
	}
	if want := "PE plugins require the Windows agent build"; err.Error() != want {
		t.Fatalf("unexpected memory-path error: %v", err)
	}
}

// The PowerShell host child implements the standard plugin child contract
// with zero disk writes: the framing helpers and the Add-Type mapper mirror
// the copy-of-self child byte for byte, and the embedded export name cannot
// escape its single-quoted literal.
func TestPowerShellHostScriptMapsPluginBytesWithoutFiles(t *testing.T) {
	script := pluginPowershellHostScript("Run")
	for _, want := range []string{
		"$exportName = 'Run'",
		"Add-Type -TypeDefinition @'",
		"VirtualAlloc",
		"LoadLibraryA",
		"GetProcAddress",
		"DLL_PROCESS_ATTACH",
		"plugin PE must be PE32+ amd64",
		"forwarded plugin exports are unsupported",
		"invalid plugin frame sizes",
		"ReadUInt32",
		"[Console]::Out.Write([P]::Invoke($exportName, 16777216))",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("PowerShell host script missing %q", want)
		}
	}
	// No loading from any path: the plugin bytes come from the stdin frame.
	if strings.Contains(script, "LoadLibrary($") || strings.Contains(script, "LoadLibrary(\"") {
		t.Fatal("the host must map the streamed bytes, not load a file path")
	}
	// Quote doubling keeps the export literal closed.
	hostile := pluginPowershellHostScript("Run'; Remove-Item C:\\ -Recurse; #")
	if !strings.Contains(hostile, "$exportName = 'Run''; Remove-Item C:\\ -Recurse; #'") {
		t.Fatalf("single quotes must be doubled to keep the export literal: %s", hostile)
	}
	for _, forbidden := range []string{"stageWindowsPlugin", "plugin.dll", "WriteAllBytes", "[System.IO.File]"} {
		if strings.Contains(script, forbidden) {
			t.Fatalf("the host script must not touch the filesystem: found %q", forbidden)
		}
	}
}

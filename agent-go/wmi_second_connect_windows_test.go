//go:build windows

package main

import (
	"os"
	"strings"
	"testing"
)

// TestWMISecondConnectProbe is the standalone lab reproducer for the
// one-shot WMI failure: after make_token, the first agent-side wmiexec of a
// process succeeds and every later one fails at ConnectServer with
// 0x80070005. The probe performs two read-only WMI operations under one
// shared LOGON_NEW_CREDENTIALS logon session, recording the calling thread
// token's authentication LUID and each COM HRESULT, then one probe through
// the explicit credential plumbing in the same process. The shared
// wmiProbeExecute runs the same protocol inside the live agent through
// __WMI_PROBE__; this standalone variant creates its own logon session, so
// it also works without an active make_token.
//
// The probe never logs credentials. Build and run it on the lab source host:
//
//	GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go test -c -o wmi-second-connect-probe.exe .
//	set RMM_WMI_PROBE_TARGET=10.254.100.32
//	set RMM_WMI_PROBE_DOMAIN=ARCHER
//	set RMM_WMI_PROBE_USER=Administrator
//	set RMM_WMI_PROBE_PASSWORD=…
//	wmi-second-connect-probe.exe -test.v -test.run TestWMISecondConnectProbe
//
// Clear the password variable afterwards. Without RMM_WMI_PROBE_TARGET the
// test skips, so CI and plain `go test` never touch a remote host.
func TestWMISecondConnectProbe(t *testing.T) {
	target := strings.TrimSpace(os.Getenv("RMM_WMI_PROBE_TARGET"))
	if target == "" {
		t.Skip("set RMM_WMI_PROBE_TARGET and credential environment variables to run the remote two-shot probe")
	}
	domain := strings.TrimSpace(os.Getenv("RMM_WMI_PROBE_DOMAIN"))
	user := strings.TrimSpace(os.Getenv("RMM_WMI_PROBE_USER"))
	if user == "" {
		t.Fatal("RMM_WMI_PROBE_USER is required when RMM_WMI_PROBE_TARGET is set")
	}
	password := os.Getenv("RMM_WMI_PROBE_PASSWORD")
	label := user
	if domain != "" {
		label = domain + `\` + user
	}
	// One logon session shared by both ambient probes, mirroring one
	// make_token whose duplicated handles feed each lateral worker.
	token, credentials, err := captureLateralExplicitIdentity(winrmCredentials{
		domain: domain, user: user, password: password,
	})
	if err != nil {
		t.Fatalf("create the network-only logon: %v", err)
	}
	defer func() {
		if credentials != nil {
			*credentials = winrmCredentials{}
		}
		_ = token.Close()
	}()
	source := wmiProbeCaptureToken(token)
	t.Logf("target=%s identity=%s host process logon LUID=0x%016x",
		target, label, wmiProbeProcessAuthenticationID())
	t.Logf("source token authID=0x%016x modifiedID=0x%016x sid=%s",
		source.authID, source.modifiedID, source.sid)

	if strings.EqualFold(strings.TrimSpace(os.Getenv("RMM_WMI_PROBE_MODE")), "held") {
		held := wmiProbeHeld(target, token, source)
		t.Logf("[held COM lifetime: one continuous initialization, two connect sequences]\n%s", held.report())
		t.Log(wmiProbeHeldVerdict(target, held))
		t.Log(wmiProbeChecklist(target, held.StartUTC, held.EndUTC))
		return
	}

	first := wmiProbeRun(target, nil, token, source)
	t.Logf("[ambient one (first wmiexec)]\n%s", first.report())
	second := wmiProbeRun(target, nil, token, source)
	t.Logf("[ambient two (second wmiexec)]\n%s", second.report())
	fixed := wmiProbeRun(target, credentials, token, source)
	t.Logf("[explicit credentials (shipped fix)]\n%s", fixed.report())

	t.Log(wmiProbeVerdict(target, first, second))
	if fixed.Err == nil && fixed.ConnectHR == 0 {
		t.Log("The explicit-credential probe succeeded in the same process: the wmiexec+make_token fix works against this target.")
	} else {
		t.Logf("The explicit-credential probe did not succeed (err=%v connect=0x%08x) — investigate before relying on the fix here.",
			fixed.Err, fixed.ConnectHR)
	}
}

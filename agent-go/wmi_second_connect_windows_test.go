//go:build windows

package main

import (
	"fmt"
	"os"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"unsafe"
)

// TestWMISecondConnectProbe is the lab reproducer for the one-shot WMI
// failure: after make_token, the first agent-side wmiexec of a process
// succeeds and every later one fails at ConnectServer with 0x80070005.
//
// The probe performs two read-only WMI operations (ConnectServer plus
// GetObject on a WMI class path — no ExecMethod, no process created on the
// target, no deployment side effects) under one shared LOGON_NEW_CREDENTIALS
// logon session, recording the calling thread token's authentication LUID
// and each COM HRESULT. A third probe reuses the same credentials through
// the explicit ConnectServer plumbing (the shipped wmiexec fix) inside the
// same process. The recorded data separates a token-lifecycle defect (the
// thread token's logon session changes or dies between moves) from a
// connection-security issue (the token is unchanged and the second DCOM
// connect is still denied, pointing at per-process WMI client state).
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
	defer token.Close()
	t.Logf("target=%s identity=%s host process logon LUID=0x%016x",
		target, label, probeProcessAuthenticationID(t))

	first := probeSecondConnect(target, nil, token)
	probeSecondConnectReport(t, "ambient one (first wmiexec)", first)
	second := probeSecondConnect(target, nil, token)
	probeSecondConnectReport(t, "ambient two (second wmiexec)", second)
	fixed := probeSecondConnect(target, credentials, token)
	probeSecondConnectReport(t, "explicit credentials (shipped fix)", fixed)

	switch {
	case first.Err != nil || first.ConnectHR != 0:
		t.Logf("VERDICT: inconclusive — the FIRST probe did not succeed (err=%v connect=0x%08x)."+
			" Fix the probe environment (credentials, target reachability) before reading a verdict.",
			first.Err, first.ConnectHR)
	case second.Err != nil:
		t.Logf("VERDICT: inconclusive — the second probe failed in probe machinery (%v).", second.Err)
	case second.ConnectHR == 0:
		t.Log("VERDICT: the one-shot failure did not reproduce here — the second ambient connect succeeded.")
	case second.AuthenticationID != first.AuthenticationID:
		t.Logf("VERDICT: token lifecycle defect — the thread token's authentication ID changed between"+
			" moves (0x%016x → 0x%016x), so the identity itself did not survive the second move.",
			first.AuthenticationID, second.AuthenticationID)
	default:
		t.Logf("VERDICT: connection-security issue — the thread token kept authentication ID"+
			" 0x%016x (level %d) across both moves, yet ConnectServer went from 0x%08x to 0x%08x."+
			" The identity is sound; the in-process WMI client state from the first connection is the variable.",
			second.AuthenticationID, second.ImpersonationLevel, first.ConnectHR, second.ConnectHR)
	}
	if fixed.Err == nil && fixed.ConnectHR == 0 {
		t.Log("The explicit-credential probe succeeded in the same process: the wmiexec+make_token fix works against this target.")
	} else {
		t.Logf("The explicit-credential probe did not succeed (err=%v connect=0x%08x) — investigate before relying on the fix here.",
			fixed.Err, fixed.ConnectHR)
	}
}

// probeSecondConnectResult carries one probe's observations. HRESULTs are
// recorded even on failure; credentials are never included.
type probeSecondConnectResult struct {
	TokenID            uint64
	AuthenticationID   uint64
	ImpersonationLevel uint32
	ConnectHR          uintptr
	QueryHR            uintptr
	Err                error
}

// probeThreadTokenStatistics returns the calling thread's impersonation
// token identity through TOKEN_STATISTICS, mirroring how the lateral worker
// opens its thread token read-only.
func probeThreadTokenStatistics() (probeSecondConnectResult, error) {
	open := advapi32.NewProc("OpenThreadToken")
	var token syscall.Token
	ok, _, openErr := open.Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		return probeSecondConnectResult{}, fmt.Errorf("open thread token: %w", openErr)
	}
	defer token.Close()
	type tokenStatistics struct {
		TokenID            uint64
		AuthenticationID   uint64
		ExpirationTime     int64
		TokenType          uint32
		ImpersonationLevel uint32
		DynamicCharged     uint32
		DynamicAvailable   uint32
		GroupCount         uint32
		PrivilegeCount     uint32
		ModifiedID         uint64
	}
	var statistics tokenStatistics
	get := advapi32.NewProc("GetTokenInformation")
	var returned uint32
	ok, _, getErr := get.Call(uintptr(token), 10, uintptr(unsafe.Pointer(&statistics)),
		unsafe.Sizeof(statistics), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		return probeSecondConnectResult{}, fmt.Errorf("query thread token statistics: %w", getErr)
	}
	return probeSecondConnectResult{
		TokenID:            statistics.TokenID,
		AuthenticationID:   statistics.AuthenticationID,
		ImpersonationLevel: statistics.ImpersonationLevel,
	}, nil
}

// probeProcessAuthenticationID reports the host process logon session LUID
// for context: the identity an ambient fallback would authenticate with.
func probeProcessAuthenticationID(t *testing.T) uint64 {
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		t.Logf("open process token: %v (context unavailable)", err)
		return 0
	}
	defer token.Close()
	var statistics struct {
		TokenID          uint64
		AuthenticationID uint64
		Rest             [512 - 16]byte
	}
	var returned uint32
	ok, _, err := advapi32.NewProc("GetTokenInformation").Call(
		uintptr(token), 10, uintptr(unsafe.Pointer(&statistics)),
		unsafe.Sizeof(statistics), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		t.Logf("query process token statistics: %v (context unavailable)", err)
		return 0
	}
	return statistics.AuthenticationID
}

// probeSecondConnect duplicates the shared token, impersonates it on a
// locked OS thread, records the thread identity, and performs one
// read-only WMI round trip. A nil identity mirrors the ambient (cloaked)
// deployments; a non-nil identity takes the explicit ConnectServer path —
// the two legs under test, on the same code the lateral worker runs.
func probeSecondConnect(host string, identity *winrmCredentials, token syscall.Token) probeSecondConnectResult {
	result := probeSecondConnectResult{}
	var duplicate syscall.Handle
	process := syscall.Handle(^uintptr(0))
	if err := syscall.DuplicateHandle(process, syscall.Handle(token), process, &duplicate, 0, false, 2); err != nil {
		result.Err = fmt.Errorf("duplicate the network-only token: %w", err)
		return result
	}
	defer syscall.CloseHandle(duplicate)
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(duplicate)); ok == 0 {
		result.Err = fmt.Errorf("impersonate the network-only token: %w", err)
		return result
	}
	defer procRevertToSelf.Call()
	statistics, err := probeThreadTokenStatistics()
	if err != nil {
		result.Err = err
		return result
	}
	result.TokenID, result.AuthenticationID, result.ImpersonationLevel =
		statistics.TokenID, statistics.AuthenticationID, statistics.ImpersonationLevel

	result.ConnectHR, result.QueryHR, result.Err = probeReadonlyWMI(host, identity)
	return result
}

// probeComCall records the raw HRESULT of one COM dispatch through the
// shared comCallHR helper, so the report carries exact result codes.
func probeComCall(iface, slot uintptr, args ...uintptr) uintptr {
	return comCallHR(iface, slot, args...)
}

// probeReadonlyWMI performs ConnectServer followed by GetObject on
// "Win32_ComputerSystem" — a class metadata fetch that writes nothing and
// creates nothing on the target. It mirrors wmiCreateProcess's connection
// and blanket sequence, including the explicit identity plumbing, but stops
// before any method execution.
func probeReadonlyWMI(host string, identity *winrmCredentials) (connectHR, queryHR uintptr, err error) {
	authnService, ambientAuthority, serverPrincipal, err := wmiAuthentication(host)
	if err != nil {
		return 0, 0, err
	}
	defer sysFreeString(ambientAuthority)
	defer runtime.KeepAlive(serverPrincipal)
	var identityUser, identityPassword, identityAuthority uintptr
	var blanket *secWinNTAuthIdentity
	if identity != nil {
		userPtr, passwordPtr, authorityPtr, blanketPtr, credErr := wmiConnectIdentity(identity)
		if credErr != nil {
			return 0, 0, credErr
		}
		identityUser, identityPassword, identityAuthority, blanket = userPtr, passwordPtr, authorityPtr, blanketPtr
		defer sysFreeString(identityUser)
		defer sysFreeString(identityPassword)
		defer sysFreeString(identityAuthority)
		defer runtime.KeepAlive(blanket)
	}
	if hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded); hr != 0 && hr != 1 {
		return 0, 0, fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
	}
	defer procCoUninitialize.Call()
	if err := initializeWMISecurity(); err != nil {
		return 0, 0, err
	}
	var locator uintptr
	if hr, _, _ := procCoCreateInstance.Call(
		uintptr(unsafe.Pointer(&clsidWbemLocator)), 0, clsctxInprocServer,
		uintptr(unsafe.Pointer(&iidIWbemLocator)), uintptr(unsafe.Pointer(&locator)),
	); hr != 0 || locator == 0 {
		return 0, 0, fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x", hr)
	}
	defer comRelease(locator)
	namespace := sysAllocString(`\\` + host + `\root\cimv2`)
	defer sysFreeString(namespace)
	if namespace == 0 {
		return 0, 0, fmt.Errorf("allocate namespace string")
	}
	authority, authnService := ambientAuthority, authnService
	if identity != nil {
		authority, authnService = identityAuthority, rpcCAuthnWinnt
	}
	var services uintptr
	connectHR = probeComCall(locator, 3, namespace, identityUser, identityPassword, 0, 0,
		authority, 0, uintptr(unsafe.Pointer(&services)))
	if connectHR != 0 || services == 0 {
		return connectHR, 0, nil
	}
	defer comRelease(services)
	var blanketHR uintptr
	if identity != nil {
		blanketHR, _, _ = procCoSetProxyBlanket.Call(
			services, rpcCAuthnWinnt, rpcCAuthzNone, 0,
			rpcCAuthnLevelPktPrivacy, rpcCImpLevelImpersonate, uintptr(unsafe.Pointer(blanket)), 0)
	} else {
		blanketHR, _, _ = procCoSetProxyBlanket.Call(
			services, authnService, rpcCAuthzNone, uintptr(unsafe.Pointer(serverPrincipal)),
			rpcCAuthnLevelPktPrivacy, rpcCImpLevelImpersonate, 0, eoacDynamicCloaking)
	}
	if blanketHR != 0 {
		return connectHR, 0, fmt.Errorf("CoSetProxyBlanket failed: 0x%08x", blanketHR)
	}
	classPath := sysAllocString("Win32_ComputerSystem")
	defer sysFreeString(classPath)
	if classPath == 0 {
		return connectHR, 0, fmt.Errorf("allocate class path string")
	}
	var classObject uintptr
	queryHR = probeComCall(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0)
	if classObject != 0 {
		comRelease(classObject)
	}
	return connectHR, queryHR, nil
}

// probeSecondConnectReport logs one probe result without credentials.
func probeSecondConnectReport(t *testing.T, label string, result probeSecondConnectResult) {
	if result.Err == nil {
		t.Logf("[%s] thread auth LUID=0x%016x token LUID=0x%016x level=%d ConnectServer=0x%08x GetObject=0x%08x",
			label, result.AuthenticationID, result.TokenID, result.ImpersonationLevel,
			result.ConnectHR, result.QueryHR)
		return
	}
	t.Logf("[%s] machinery error before connect: %v", label, result.Err)
}

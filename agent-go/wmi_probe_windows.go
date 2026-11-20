//go:build windows

package main

import (
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

// wmiProbeResult carries one probe's observations. HRESULTs are recorded
// even on failure; credentials are never included.
type wmiProbeResult struct {
	TokenID            uint64
	AuthenticationID   uint64
	ImpersonationLevel uint32
	HaveThreadToken    bool
	ConnectHR          uintptr
	QueryHR            uintptr
	Err                error
}

// report renders one probe line without credential material.
func (r wmiProbeResult) report() string {
	if r.Err != nil {
		return "machinery error before connect: " + r.Err.Error()
	}
	if !r.HaveThreadToken {
		return fmt.Sprintf("no thread token ConnectServer=0x%08x GetObject=0x%08x", r.ConnectHR, r.QueryHR)
	}
	return fmt.Sprintf("thread LUID=0x%016x level=%d ConnectServer=0x%08x GetObject=0x%08x",
		r.AuthenticationID, r.ImpersonationLevel, r.ConnectHR, r.QueryHR)
}

// wmiProbeVerdict interprets the two ambient probes: an unchanged thread
// authentication ID with a denied second connect points at in-process WMI
// client state (connection security); a changed or vanished ID points at
// the token lifecycle itself.
func wmiProbeVerdict(first, second wmiProbeResult) string {
	switch {
	case first.Err != nil || first.ConnectHR != 0:
		return fmt.Sprintf("verdict inconclusive: the first probe did not succeed (err=%v connect=0x%08x); fix the probe environment (credentials, target reachability) before reading more",
			first.Err, first.ConnectHR)
	case second.Err != nil:
		return fmt.Sprintf("verdict inconclusive: the second probe failed in probe machinery (%v)", second.Err)
	case second.ConnectHR == 0:
		return "verdict no-repro: the second ambient connect succeeded here"
	case !second.HaveThreadToken || second.AuthenticationID != first.AuthenticationID:
		return fmt.Sprintf("verdict token-lifecycle: the thread token's identity changed between probes (0x%016x then 0x%016x), so the network identity did not survive",
			first.AuthenticationID, second.AuthenticationID)
	default:
		return fmt.Sprintf("verdict connection-security: the thread token kept authentication ID 0x%016x across both probes while ConnectServer went 0x%08x then 0x%08x; the identity is sound and the in-process WMI client state denied the second connection",
			second.AuthenticationID, first.ConnectHR, second.ConnectHR)
	}
}

// wmiProbeExecute runs the diagnostic against one host using the active
// make_token state. Unlike the deployed wmiexec path it deliberately
// exercises the ambient (cloaked) thread identity — the exact combination
// that failed in the field — twice, then once through the explicit
// credential plumbing, so a single command separates token lifecycle from
// connection security and validates the shipped fix in this process.
func wmiProbeExecute(host string) (string, error) {
	if identityBoundaryFault.Load() {
		return "", errIdentityUnsafe
	}
	token, credentials, err := captureLateralIdentity(true)
	if err != nil {
		return "", err
	}
	defer func() {
		if credentials != nil {
			*credentials = winrmCredentials{}
		}
		_ = token.Close()
	}()
	var report strings.Builder
	fmt.Fprintf(&report, "wmi probe host=%s; process logon LUID=0x%016x;",
		host, wmiProbeProcessAuthenticationID())
	if token == 0 {
		report.WriteString(" no make_token active: ambient probes authenticate as the agent identity\n")
	} else {
		report.WriteString("\n")
	}
	first := wmiProbeRun(host, nil, token)
	fmt.Fprintf(&report, "probe 1 ambient: %s\n", first.report())
	second := wmiProbeRun(host, nil, token)
	fmt.Fprintf(&report, "probe 2 ambient: %s\n", second.report())
	if credentials != nil {
		third := wmiProbeRun(host, credentials, token)
		fmt.Fprintf(&report, "probe 3 explicit: %s\n", third.report())
		if third.Err == nil && third.ConnectHR == 0 {
			report.WriteString("the explicit-credential path authenticated in the same process: wmiexec deployments are immune to the ambient failure\n")
		}
	}
	fmt.Fprintf(&report, "%s\n", wmiProbeVerdict(first, second))
	return report.String(), nil
}

// wmiProbeRun duplicates the supplied token, impersonates it on a locked OS
// thread, records the thread identity, and performs one read-only WMI round
// trip. A nil identity mirrors the ambient (cloaked) deployments; a non-nil
// identity takes the explicit ConnectServer path. A zero token runs the
// probes without impersonation, matching a token-less agent.
func wmiProbeRun(host string, identity *winrmCredentials, token syscall.Token) wmiProbeResult {
	result := wmiProbeResult{}
	if token != 0 {
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
	}
	statistics, err := wmiProbeThreadStatistics()
	if err != nil {
		result.Err = err
		return result
	}
	result.TokenID, result.AuthenticationID = statistics.TokenID, statistics.AuthenticationID
	result.ImpersonationLevel, result.HaveThreadToken = statistics.ImpersonationLevel, statistics.HaveThreadToken
	result.ConnectHR, result.QueryHR, result.Err = wmiProbeConnectAndQuery(host, identity)
	return result
}

// wmiProbeThreadStatistics returns the calling thread's token identity
// through TOKEN_STATISTICS, mirroring how the lateral worker opens its
// thread token read-only.
func wmiProbeThreadStatistics() (wmiProbeResult, error) {
	open := advapi32.NewProc("OpenThreadToken")
	var token syscall.Token
	ok, _, openErr := open.Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		if errno, valid := openErr.(syscall.Errno); valid && errno == syscall.Errno(1008) {
			// ERROR_NO_TOKEN: the thread runs without impersonation, which
			// is the expected state for a token-less probe.
			return wmiProbeResult{HaveThreadToken: false}, nil
		}
		return wmiProbeResult{}, fmt.Errorf("open thread token: %w", openErr)
	}
	defer token.Close()
	var statistics struct {
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
	get := advapi32.NewProc("GetTokenInformation")
	var returned uint32
	ok, _, getErr := get.Call(uintptr(token), 10, uintptr(unsafe.Pointer(&statistics)),
		unsafe.Sizeof(statistics), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		return wmiProbeResult{}, fmt.Errorf("query thread token statistics: %w", getErr)
	}
	return wmiProbeResult{
		TokenID:            statistics.TokenID,
		AuthenticationID:   statistics.AuthenticationID,
		ImpersonationLevel: statistics.ImpersonationLevel,
		HaveThreadToken:    true,
	}, nil
}

// wmiProbeProcessAuthenticationID reports the host process logon session
// LUID for context: the identity an ambient fallback would authenticate with.
func wmiProbeProcessAuthenticationID() uint64 {
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		return 0
	}
	defer token.Close()
	var statistics struct {
		TokenID          uint64
		AuthenticationID uint64
		Rest             [512 - 16]byte
	}
	var returned uint32
	ok, _, _ := advapi32.NewProc("GetTokenInformation").Call(
		uintptr(token), 10, uintptr(unsafe.Pointer(&statistics)),
		unsafe.Sizeof(statistics), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		return 0
	}
	return statistics.AuthenticationID
}

// wmiProbeConnectAndQuery performs ConnectServer followed by GetObject on
// "Win32_ComputerSystem" — a class metadata fetch that writes nothing and
// creates nothing on the target. It mirrors wmiCreateProcess's connection
// and blanket sequence, including the explicit identity plumbing, but stops
// before any method execution.
func wmiProbeConnectAndQuery(host string, identity *winrmCredentials) (connectHR, queryHR uintptr, err error) {
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
	connectHR = comCallHR(locator, 3, namespace, identityUser, identityPassword, 0, 0,
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
	queryHR = comCallHR(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0)
	if classObject != 0 {
		comRelease(classObject)
	}
	return connectHR, queryHR, nil
}

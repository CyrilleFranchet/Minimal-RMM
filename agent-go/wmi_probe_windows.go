//go:build windows

package main

import (
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// The WMI probe pipeline records every stage — impersonation, COM
// initialization, ConnectServer, proxy configuration, query, cleanup —
// with a UTC timestamp, OS thread identity, raw HRESULT, and duration, and
// marks stages that never executed as not_run. A stage that did not run is
// never reported with a zero HRESULT: a denied connect leaves no proxy and
// no query, and reporting one would be evidence that never existed.

var (
	wmiProbeKernel32                = syscall.NewLazyDLL("kernel32.dll")
	procProbeGetCurrentProcessId    = wmiProbeKernel32.NewProc("GetCurrentProcessId")
	procProbeGetCurrentThreadId     = wmiProbeKernel32.NewProc("GetCurrentThreadId")
	procProbeLstrlenW               = wmiProbeKernel32.NewProc("lstrlenW")
	procCoQueryProxyBlanket         = ole32.NewProc("CoQueryProxyBlanket")
	procCoTaskMemFree               = ole32.NewProc("CoTaskMemFree")
	procProbeConvertSidToStringSidW = advapi32.NewProc("ConvertSidToStringSidW")
	procProbeLocalFree              = wmiProbeKernel32.NewProc("LocalFree")
)

// wmiProbeStage is one pipeline step. detail carries safe evidence only:
// HRESULTs, token identifiers and SIDs, principal names, blanket
// parameters — never credential material.
type wmiProbeStage struct {
	name     string
	utc      string
	notRun   bool
	hasHR    bool
	hr       uintptr
	detail   string
	duration time.Duration
}

// wmiProbeToken is a token snapshot through TOKEN_STATISTICS plus the user
// SID. AuthenticationId identifies the logon session; ModifiedId changes
// with every token modification, catching changes an unchanged logon ID
// would miss.
type wmiProbeToken struct {
	present    bool
	tokenID    uint64
	authID     uint64
	modifiedID uint64
	tokenType  uint32
	impLevel   uint32
	sid        string
}

// wmiProbeResult carries one probe attempt: the aggregates the verdict
// needs, the thread identity snapshots around ConnectServer, and the full
// stage list. HRESULT and stage fields hold zero values only for stages
// that actually ran.
type wmiProbeResult struct {
	ConnectHR uintptr
	Err       error
	Explicit  bool
	StartUTC  string
	EndUTC    string
	PID       uint32
	TID       uint32
	Before    wmiProbeToken
	After     wmiProbeToken
	Stages    []wmiProbeStage
}

func wmiProbeTimestamp() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
}

func (r *wmiProbeResult) stageHR(name string, hr uintptr, duration time.Duration, detail string) {
	r.Stages = append(r.Stages, wmiProbeStage{name: name, utc: wmiProbeTimestamp(), hasHR: true, hr: hr, duration: duration, detail: detail})
}

func (r *wmiProbeResult) stageInfo(name string, duration time.Duration, detail string) {
	r.Stages = append(r.Stages, wmiProbeStage{name: name, utc: wmiProbeTimestamp(), duration: duration, detail: detail})
}

func (r *wmiProbeResult) stageNotRun(name, why string) {
	r.Stages = append(r.Stages, wmiProbeStage{name: name, utc: wmiProbeTimestamp(), notRun: true, detail: why})
}

func authnServiceLabel(service uint32) string {
	switch service {
	case 0:
		return "none"
	case ^uint32(0):
		return "default"
	case 9:
		return "negotiate"
	case 10:
		return "ntlm"
	case 16:
		return "kerberos"
	}
	return "other"
}

func authnLevelLabel(level uint32) string {
	switch level {
	case 0:
		return "default"
	case 1:
		return "none"
	case 2:
		return "connect"
	case 3:
		return "call"
	case 4:
		return "pkt"
	case 5:
		return "pkt-integrity"
	case 6:
		return "pkt-privacy"
	}
	return "other"
}

func impersonationLevelLabel(level uint32) string {
	switch level {
	case 0:
		return "anonymous"
	case 1:
		return "identification"
	case 2:
		return "impersonation"
	case 3:
		return "delegation"
	}
	return "other"
}

func tokenTypeLabel(kind uint32) string {
	if kind == 1 {
		return "primary"
	}
	return "impersonation"
}

// proxyImpersonationLevelLabel decodes RPC_C_IMP_LEVEL values, whose
// numbering differs from SECURITY_IMPERSONATION_LEVEL on token snapshots.
func proxyImpersonationLevelLabel(level uint32) string {
	if level == 0 {
		return "default"
	}
	if level > 4 {
		return "other"
	}
	return impersonationLevelLabel(level - 1)
}

// report renders the attempt's evidence: a header with clock, process, and
// OS thread, then one line per stage. Stages that never executed say so
// explicitly and never show a zero HRESULT.
func (r wmiProbeResult) report() string {
	var out strings.Builder
	fmt.Fprintf(&out, "start=%s end=%s pid=%d tid=%d mode=%s",
		r.StartUTC, r.EndUTC, r.PID, r.TID, map[bool]string{true: "explicit", false: "ambient"}[r.Explicit])
	if r.Err != nil {
		fmt.Fprintf(&out, " machinery-error=%v", r.Err)
	}
	out.WriteByte('\n')
	for _, s := range r.Stages {
		fmt.Fprintf(&out, "  utc=%s pid=%d tid=%d ", s.utc, r.PID, r.TID)
		switch {
		case s.notRun:
			fmt.Fprintf(&out, "  %-15s not_run (%s)\n", s.name, s.detail)
		case s.hasHR:
			fmt.Fprintf(&out, "  %-15s hr=0x%08x (%s) %s\n", s.name, s.hr, s.duration, s.detail)
		default:
			fmt.Fprintf(&out, "  %-15s %s", s.name, s.detail)
			if s.duration > 0 {
				fmt.Fprintf(&out, " (%s)", s.duration)
			}
			out.WriteByte('\n')
		}
	}
	return out.String()
}

// identityDetail renders one snapshot plus its comparison with a
// reference: the invariant identity fields (logon session and SID) and the
// change detector (ModifiedId).
func (t wmiProbeToken) identityDetail(reference wmiProbeToken) string {
	if !t.present {
		return "no thread token"
	}
	text := fmt.Sprintf("authID=0x%016x tokenID=0x%016x modifiedID=0x%016x type=%s level=%s sid=%s",
		t.authID, t.tokenID, t.modifiedID, tokenTypeLabel(t.tokenType),
		impersonationLevelLabel(t.impLevel), t.sid)
	if !reference.present {
		return text
	}
	if t.authID != reference.authID || t.sid != reference.sid {
		return text + " IDENTITY-DIFFERS-from-reference"
	}
	if t.modifiedID != reference.modifiedID || t.tokenID != reference.tokenID {
		return text + " same-logon-and-sid-token-object-differs"
	}
	return text + " unchanged-vs-reference"
}

// wmiProbeVerdict interprets the two ambient probes under the evidence
// discipline: an unchanged thread identity is useful evidence, but it does
// not establish that the token is entirely sound, nor that any particular
// client state caused the rejection. A reproduced denial therefore reads
// "cause undetermined" and names the target-side evidence that would
// settle the question.
func wmiProbeVerdict(host string, first, second wmiProbeResult) string {
	switch {
	case first.Err != nil || first.ConnectHR != 0:
		return fmt.Sprintf("verdict inconclusive: the first probe did not succeed (err=%v connect=0x%08x); fix the probe environment (credentials, target reachability) before reading more",
			first.Err, first.ConnectHR)
	case second.Err != nil:
		return fmt.Sprintf("verdict inconclusive: the second probe failed in probe machinery (%v)", second.Err)
	case second.ConnectHR == 0:
		return "verdict no-repro: the second ambient connect succeeded here"
	case !first.Before.present || !second.Before.present:
		return "verdict inconclusive: the thread token was absent on one attempt; the ambient identity itself is in question"
	case second.Before.authID != first.Before.authID || second.Before.sid != first.Before.sid:
		return fmt.Sprintf("verdict token-identity-differs: the two attempts did not carry the same identity (authID 0x%016x vs 0x%016x, sid %s vs %s); treat the token lifecycle as the suspect",
			first.Before.authID, second.Before.authID, first.Before.sid, second.Before.sid)
	case second.After.present && second.Before.modifiedID != second.After.modifiedID:
		return fmt.Sprintf("verdict token-modified-during-connect: the thread token changed across the second ConnectServer (modifiedID 0x%016x then 0x%016x); the token lifecycle is implicated",
			second.Before.modifiedID, second.After.modifiedID)
	}
	var out strings.Builder
	fmt.Fprintf(&out, "verdict: second ambient connection denied; cause undetermined.\n")
	fmt.Fprintf(&out, "  evidence: both pre-connect snapshots carried the same logon ID and SID (authID=0x%016x sid=%s second-level=%s). Consult the before/after snapshots for other changes.",
		second.Before.authID, second.Before.sid, impersonationLevelLabel(second.Before.impLevel))
	fmt.Fprintf(&out, " An unchanged identity does not prove the token entirely sound, nor identify which side rejected the second connect.\n")
	fmt.Fprintf(&out, "  next distinction: target authentication events around %s .. %s (see the checklist below).", first.StartUTC, second.EndUTC)
	return out.String()
}

// wmiProbeExecute runs the diagnostic against one host using the active
// make_token state: two ambient probes — deliberately on the cloaked path
// the deployed wmiexec uses under make_token — and one explicit comparison
// probe, then the verdict and the target-side evidence checklist. Neither
// the report nor the verdict names a cause beyond the recorded evidence.
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
	source := wmiProbeCaptureToken(token)
	var report strings.Builder
	fmt.Fprintf(&report, "wmi probe host=%s; process logon LUID=0x%016x;", host, wmiProbeProcessAuthenticationID())
	if token == 0 {
		report.WriteString(" no make_token active: ambient probes authenticate as the agent identity\n")
	} else {
		fmt.Fprintf(&report, " source token authID=0x%016x modifiedID=0x%016x sid=%s\n",
			source.authID, source.modifiedID, source.sid)
	}
	first := wmiProbeRun(host, nil, token, source)
	fmt.Fprintf(&report, "probe 1 ambient\n%s", first.report())
	second := wmiProbeRun(host, nil, token, source)
	fmt.Fprintf(&report, "probe 2 ambient\n%s", second.report())
	if credentials != nil {
		third := wmiProbeRun(host, credentials, token, source)
		fmt.Fprintf(&report, "probe 3 explicit\n%s", third.report())
		if third.Err == nil && third.ConnectHR == 0 {
			report.WriteString("the explicit-credential probe connected in the same process; the forced-identity path did not hit the denial here\n")
		}
	} else {
		report.WriteString("probe 3 explicit skipped: no make_token active\n")
	}
	fmt.Fprintf(&report, "%s\n", wmiProbeVerdict(host, first, second))
	fmt.Fprintf(&report, "%s\n", wmiProbeChecklist(host, first, second))
	return report.String(), nil
}

// wmiProbeChecklist prints the target-side correlation guidance, keyed to
// the probes' UTC timestamps. Security events on the target record local
// time, so the collection window stays generous; correlate by source
// address and account when the window is ambiguous. Field names differ
// between 4624 and 4625, so the example reads the full message rather than
// fixed property indices.
func wmiProbeChecklist(host string, first, second wmiProbeResult) string {
	return "target evidence (collect on " + host + ", local time applies to Security events):\n" +
		"  Get-WinEvent -FilterHashtable @{LogName='Security'; Id=@(4624,4625)} -MaxEvents 400 |\n" +
		"    Where-Object { $_.TimeCreated -gt (Get-Date).AddHours(-3) } |\n" +
		"    Select-Object TimeCreated, Id, Message | Format-List\n" +
		"  probes ran at " + first.StartUTC + " and " + second.StartUTC + " UTC; match on this agent host's source address.\n" +
		"  record per attempt: account and domain, authentication package, logon type, source address;\n" +
		"  on 4625 additionally the Status and SubStatus codes.\n" +
		"  reading: a different account on attempt two means the client presented another identity;\n" +
		"  the same account with 4625 means an authentication or authorization rejection;\n" +
		"  a missing event does not by itself prove the attempt never left the client."
}

// wmiProbeRun duplicates the supplied token, impersonates it on a locked OS
// thread, captures the thread identity before and after ConnectServer, and
// performs one read-only WMI round trip, recording every stage. The source
// snapshot (captured from the supplied token handle) is the reference the
// thread identity is compared against. A nil identity mirrors the ambient
// (cloaked) deployments; a non-nil identity takes the explicit
// ConnectServer path. A zero token runs without impersonation, matching a
// token-less agent.
func wmiProbeRun(host string, identity *winrmCredentials, token syscall.Token, source wmiProbeToken) (result wmiProbeResult) {
	result = wmiProbeResult{StartUTC: wmiProbeTimestamp(), Explicit: identity != nil}
	pid, _, _ := procProbeGetCurrentProcessId.Call()
	result.PID = uint32(pid)
	defer func() { result.EndUTC = wmiProbeTimestamp() }()
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	tid, _, _ := procProbeGetCurrentThreadId.Call()
	result.TID = uint32(tid)
	impersonated := false
	if token != 0 {
		var duplicate syscall.Handle
		process := syscall.Handle(^uintptr(0))
		if err := syscall.DuplicateHandle(process, syscall.Handle(token), process, &duplicate, 0, false, 2); err != nil {
			result.Err = fmt.Errorf("duplicate the network-only token: %w", err)
			result.stageNotRun("impersonate", err.Error())
			return result
		}
		defer syscall.CloseHandle(duplicate)
		start := time.Now()
		ok, _, impErr := procImpersonateLoggedOnUser.Call(uintptr(duplicate))
		if ok == 0 {
			result.stageInfo("impersonate", time.Since(start), fmt.Sprintf("ImpersonateLoggedOnUser failed: %v", impErr))
			result.Err = fmt.Errorf("impersonate the network-only token: %w", impErr)
			return result
		}
		impersonated = true
		result.stageInfo("impersonate", time.Since(start), "ImpersonateLoggedOnUser ok (duplicated handle)")
	} else {
		result.stageNotRun("impersonate", "no token supplied: ambient process identity")
	}
	start := time.Now()
	result.Before = wmiProbeCaptureThreadToken()
	result.stageInfo("token-before", time.Since(start), result.Before.identityDetail(source))
	result.connectAndQuery(host, identity)
	if impersonated {
		start = time.Now()
		ok, _, revertErr := procRevertToSelf.Call()
		if ok == 0 {
			result.stageInfo("cleanup", time.Since(start), fmt.Sprintf("RevertToSelf failed: %v", revertErr))
		} else {
			result.stageInfo("cleanup", time.Since(start), "RevertToSelf ok")
		}
	} else {
		result.stageNotRun("cleanup", "nothing impersonated")
	}
	return result
}

// wmiProbeCaptureThreadToken snapshots the calling thread's token through
// TOKEN_STATISTICS plus the user SID. Without impersonation the thread has
// no token and the snapshot reports absent rather than synthetic values.
func wmiProbeCaptureThreadToken() wmiProbeToken {
	open := advapi32.NewProc("OpenThreadToken")
	var handle syscall.Token
	ok, _, openErr := open.Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&handle)))
	if ok == 0 {
		if errno, valid := openErr.(syscall.Errno); valid && errno == syscall.Errno(1008) {
			return wmiProbeToken{}
		}
		return wmiProbeToken{sid: fmt.Sprintf("capture-error: %v", openErr)}
	}
	defer handle.Close()
	return wmiProbeReadToken(syscall.Token(handle))
}

// wmiProbeCaptureToken snapshots a supplied token handle (the captured
// source token) without opening the thread token.
func wmiProbeCaptureToken(handle syscall.Token) wmiProbeToken {
	if handle == 0 {
		return wmiProbeToken{sid: "token-absent"}
	}
	return wmiProbeReadToken(handle)
}

// wmiProbeReadToken fills TOKEN_STATISTICS and the user SID of an open
// token handle. Errors land in the sid field so a partial snapshot is
// visible in the report instead of silently empty.
func wmiProbeReadToken(handle syscall.Token) wmiProbeToken {
	get := advapi32.NewProc("GetTokenInformation")
	var returned uint32
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
	ok, _, statsErr := get.Call(uintptr(handle), 10, uintptr(unsafe.Pointer(&statistics)),
		unsafe.Sizeof(statistics), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		return wmiProbeToken{sid: fmt.Sprintf("stats-error: %v", statsErr)}
	}
	result := wmiProbeToken{
		present:    true,
		tokenID:    statistics.TokenID,
		authID:     statistics.AuthenticationID,
		modifiedID: statistics.ModifiedID,
		tokenType:  statistics.TokenType,
		impLevel:   statistics.ImpersonationLevel,
	}
	var needed uint32
	get.Call(uintptr(handle), 1, 0, 0, uintptr(unsafe.Pointer(&needed)))
	if needed == 0 || needed > 4096 {
		result.sid = fmt.Sprintf("sid-query-error: needed=%d", needed)
		return result
	}
	buffer := make([]byte, needed)
	ok, _, userErr := get.Call(uintptr(handle), 1, uintptr(unsafe.Pointer(&buffer[0])),
		uintptr(needed), uintptr(unsafe.Pointer(&returned)))
	if ok == 0 {
		result.sid = fmt.Sprintf("user-query-error: %v", userErr)
		return result
	}
	sidPtr := *(*uintptr)(unsafe.Pointer(&buffer[0]))
	var stringPtr unsafe.Pointer
	convOk, _, convErr := procProbeConvertSidToStringSidW.Call(sidPtr, uintptr(unsafe.Pointer(&stringPtr)))
	if convOk == 0 || stringPtr == nil {
		result.sid = fmt.Sprintf("sid-convert-error: %v", convErr)
		return result
	}
	result.sid = wmiProbeReadWide(stringPtr)
	procProbeLocalFree.Call(uintptr(stringPtr))
	return result
}

// wmiProbeReadWide reads a NUL-terminated UTF-16 string from native memory,
// for COM-returned principals and SIDs. The pointer stays an
// unsafe.Pointer end to end: uintptr conversions only ever appear in call
// arguments.
func wmiProbeReadWide(ptr unsafe.Pointer) string {
	length, _, _ := procProbeLstrlenW.Call(uintptr(ptr))
	if length == 0 || length > 4096 {
		return ""
	}
	return syscall.UTF16ToString(unsafe.Slice((*uint16)(ptr), int(length)))
}

// wmiProbeProcessAuthenticationID reports the host process logon session
// LUID for context: the identity an ambient fallback would be assumed to use.
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

// wmiProbeBlanket reads a proxy's effective security through
// CoQueryProxyBlanket and describes it without credential contents: the
// authentication service, the server principal name, authentication and
// impersonation levels, and capability flags.
func wmiProbeBlanket(services uintptr) (string, error) {
	var authnService, authzService, authnLevel, impLevel, capabilities uint32
	var principal unsafe.Pointer
	hr, _, _ := procCoQueryProxyBlanket.Call(services,
		uintptr(unsafe.Pointer(&authnService)), uintptr(unsafe.Pointer(&authzService)),
		uintptr(unsafe.Pointer(&principal)),
		uintptr(unsafe.Pointer(&authnLevel)), uintptr(unsafe.Pointer(&impLevel)),
		0, // pAuthInfo stays NULL: credential material is never read back
		uintptr(unsafe.Pointer(&capabilities)))
	if hr != 0 {
		return "", fmt.Errorf("0x%08x", hr)
	}
	principalText := "<none>"
	if principal != nil {
		principalText = wmiProbeReadWide(principal)
		procCoTaskMemFree.Call(uintptr(principal))
	}
	if principalText == "" {
		principalText = "<empty>"
	}
	return fmt.Sprintf("authn=%d(%s) authz=%d principal=%q level=%d(%s) imp=%d(%s) caps=0x%x",
		authnService, authnServiceLabel(authnService), authzService, principalText,
		authnLevel, authnLevelLabel(authnLevel), impLevel, proxyImpersonationLevelLabel(impLevel),
		capabilities), nil
}

// connectAndQuery performs the instrumented read-only chain: COM
// initialization, the once-per-process COM security report, locator
// creation, ConnectServer with a token snapshot immediately after, the
// blanket observed before and after applying it, and the class fetch. When
// an upstream stage fails, every downstream stage records itself as not_run
// rather than carrying a zero result.
func (r *wmiProbeResult) connectAndQuery(host string, identity *winrmCredentials) {
	authnService, ambientAuthority, serverPrincipal, err := wmiAuthentication(host)
	if err != nil {
		r.Err = err
		r.markRemainingNotRun("authentication inputs")
		return
	}
	defer sysFreeString(ambientAuthority)
	defer runtime.KeepAlive(serverPrincipal)
	var identityUser, identityPassword, identityAuthority uintptr
	var blanket *secWinNTAuthIdentity
	if identity != nil {
		userPtr, passwordPtr, authorityPtr, blanketPtr, credErr := wmiConnectIdentity(identity)
		if credErr != nil {
			r.Err = credErr
			r.markRemainingNotRun("credential encoding")
			return
		}
		identityUser, identityPassword, identityAuthority, blanket = userPtr, passwordPtr, authorityPtr, blanketPtr
		defer sysFreeString(identityUser)
		defer sysFreeString(identityPassword)
		defer sysFreeString(identityAuthority)
		defer runtime.KeepAlive(blanket)
	}
	start := time.Now()
	hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded)
	label := "initialization failed"
	if hr == 0 {
		label = "S_OK new-MTA-on-thread"
	} else if hr == 1 {
		label = "S_FALSE thread-already-in-COM"
	}
	r.stageHR("co-init", hr, time.Since(start), label)
	if hr != 0 && hr != 1 {
		r.Err = fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
		r.markRemainingNotRun("co-init")
		return
	}
	defer procCoUninitialize.Call()
	start = time.Now()
	initErr := initializeWMISecurity()
	r.stageInfo("com-security", time.Since(start), wmiSecurityInitReport())
	if initErr != nil {
		r.Err = initErr
		r.markRemainingNotRun("com-security blocked the chain")
		return
	}
	start = time.Now()
	var locator uintptr
	hr, _, _ = procCoCreateInstance.Call(
		uintptr(unsafe.Pointer(&clsidWbemLocator)), 0, clsctxInprocServer,
		uintptr(unsafe.Pointer(&iidIWbemLocator)), uintptr(unsafe.Pointer(&locator)))
	r.stageHR("cocreate", hr, time.Since(start), "WbemLocator in-proc")
	if hr != 0 || locator == 0 {
		r.Err = fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x", hr)
		r.markRemainingNotRun("cocreate")
		return
	}
	defer comRelease(locator)
	namespace := sysAllocString(`\\` + host + `\root\cimv2`)
	defer sysFreeString(namespace)
	if namespace == 0 {
		r.Err = fmt.Errorf("allocate namespace string")
		r.markRemainingNotRun("namespace allocation")
		return
	}
	authority, authnService := ambientAuthority, authnService
	blanketMode := "dynamic-cloaking"
	if identity != nil {
		authority, authnService = identityAuthority, rpcCAuthnWinnt
		blanketMode = "explicit-SEC_WINNT_AUTH_IDENTITY"
	}
	var services uintptr
	start = time.Now()
	r.ConnectHR = comCallHR(locator, 3, namespace, identityUser, identityPassword, 0, 0,
		authority, 0, uintptr(unsafe.Pointer(&services)))
	r.stageHR("connect", r.ConnectHR, time.Since(start), fmt.Sprintf(`\\%s\root\cimv2 authn=%d(%s)`,
		host, authnService, authnServiceLabel(uint32(authnService))))
	start = time.Now()
	r.After = wmiProbeCaptureThreadToken()
	r.stageInfo("token-after", time.Since(start), r.After.identityDetail(r.Before))
	if r.ConnectHR != 0 || services == 0 {
		r.markRemainingNotRun("connect denied: no proxy exists")
		return
	}
	defer comRelease(services)
	start = time.Now()
	before, blanketErr := wmiProbeBlanket(services)
	if blanketErr != nil {
		r.stageNotRun("blanket-before", "CoQueryProxyBlanket failed: "+blanketErr.Error())
	} else {
		r.stageInfo("blanket-before", time.Since(start), before)
	}
	start = time.Now()
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
	r.stageHR("blanket", blanketHR, time.Since(start), blanketMode)
	if blanketHR != 0 {
		r.markRemainingNotRun("blanket failed")
		return
	}
	if after, err := wmiProbeBlanket(services); err != nil {
		r.stageNotRun("blanket-after", "CoQueryProxyBlanket failed: "+err.Error())
	} else {
		r.stageInfo("blanket-after", 0, after)
	}
	classPath := sysAllocString("Win32_ComputerSystem")
	defer sysFreeString(classPath)
	if classPath == 0 {
		r.Err = fmt.Errorf("allocate class path string")
		r.stageNotRun("query", "class path allocation failed")
		return
	}
	var classObject uintptr
	start = time.Now()
	queryHR := comCallHR(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0)
	r.stageHR("query", queryHR, time.Since(start), "GetObject Win32_ComputerSystem")
	if classObject != 0 {
		comRelease(classObject)
	}
}

// markRemainingNotRun records the stages after a failure point so no
// unexecuted step can appear as a succeeded zero result.
func (r *wmiProbeResult) markRemainingNotRun(failedAt string) {
	why := "upstream stage failed: " + failedAt
	executed := map[string]bool{}
	for _, s := range r.Stages {
		executed[s.name] = true
	}
	for _, name := range []string{"co-init", "com-security", "cocreate", "connect", "token-after", "blanket-before", "blanket", "blanket-after", "query"} {
		if !executed[name] {
			r.stageNotRun(name, why)
		}
	}
}

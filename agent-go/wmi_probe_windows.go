//go:build windows

package main

import (
	"errors"
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
	ConnectHR       uintptr
	SecondConnectHR uintptr
	Err             error
	Explicit        bool
	StartUTC        string
	EndUTC          string
	PID             uint32
	TID             uint32
	Before          wmiProbeToken
	After           wmiProbeToken
	Stages          []wmiProbeStage
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
	fmt.Fprintf(&out, "  next distinction: target authentication events around %s .. %s (see the checklist below), plus the lifecycle comparisons on fresh agent processes: `__WMI_PROBE__ %s held` (COM held, impersonation held) and `__WMI_PROBE__ %s cycled` (COM held, impersonation reverted and reapplied between the connections), which separate the COM-lifetime variable from the impersonation-cycling variable.",
		first.StartUTC, second.EndUTC, host, host)
	return out.String()
}

// wmiProbeExecute runs the diagnostic against one host using the active
// make_token state. The default (isolated) mode runs two ambient probes —
// deliberately on the cloaked path the deployed wmiexec uses under
// make_token — and one explicit comparison probe. The held and cycled
// modes both run their connections inside one continuous COM
// initialization (single CoInitializeEx, no CoUninitialize between the
// sequences, same OS thread); they differ in the impersonation: held
// keeps one impersonation across both connects, while cycled reverts and
// reapplies a fresh duplicate between them, mirroring the default mode's
// per-attempt identity handling. Held therefore changes two variables at
// once versus the isolated mode, and cycled separates them. Neither the
// report nor the verdict names a cause beyond the recorded evidence.
func wmiProbeExecute(host, mode string) (string, error) {
	if identityBoundaryFault.Load() {
		return "", errIdentityUnsafe
	}
	if mode == "held" || mode == "cycled" {
		token, _, err := captureLateralIdentity(false)
		if err != nil {
			return "", err
		}
		defer func() { _ = token.Close() }()
		source := wmiProbeCaptureToken(token)
		if mode == "cycled" && token == 0 {
			return "", errors.New("cycled mode requires an active make_token: it exists to cycle one")
		}
		var report strings.Builder
		fmt.Fprintf(&report, "wmi probe host=%s mode=%s; process logon LUID=0x%016x;",
			host, mode, wmiProbeProcessAuthenticationID())
		if token == 0 {
			report.WriteString(" no make_token active: the held probes authenticate as the agent identity\n")
		} else {
			fmt.Fprintf(&report, " source token authID=0x%016x modifiedID=0x%016x sid=%s\n",
				source.authID, source.modifiedID, source.sid)
		}
		if mode == "held" {
			held := wmiProbeHeld(host, token, source)
			fmt.Fprintf(&report, "probe held (one continuous COM initialization, impersonation held across both connects)\n%s", held.report())
			fmt.Fprintf(&report, "%s\n", wmiProbeHeldVerdict(host, held))
			fmt.Fprintf(&report, "%s\n", wmiProbeChecklist(host, held.StartUTC, held.EndUTC))
		} else {
			cycled := wmiProbeCycled(host, token, source)
			fmt.Fprintf(&report, "probe cycled (one continuous COM initialization, impersonation reverted and reapplied between the connects)\n%s", cycled.report())
			fmt.Fprintf(&report, "%s\n", wmiProbeCycledVerdict(host, cycled))
			fmt.Fprintf(&report, "%s\n", wmiProbeChecklist(host, cycled.StartUTC, cycled.EndUTC))
		}
		return report.String(), nil
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
	fmt.Fprintf(&report, "%s\n", wmiProbeChecklist(host, first.StartUTC, second.EndUTC))
	return report.String(), nil
}

// wmiProbeHeld runs both connections inside one continuous COM
// initialization on a single locked OS thread: one impersonation held
// across both sequences, one CoInitializeEx and CoInitializeSecurity, two
// complete connect sequences (fresh locator, connect, blanket observation,
// class fetch, full proxy release each), and one teardown at the end.
// Versus the isolated mode this changes two variables at once — the COM
// lifecycle and the impersonation continuity — so a held second connect
// that succeeds narrows the suspect set to those two without singling one
// out; the cycled mode separates them.
func wmiProbeHeld(host string, token syscall.Token, source wmiProbeToken) (result wmiProbeResult) {
	result = wmiProbeResult{StartUTC: wmiProbeTimestamp()}
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
			result.markRemainingNotRun("impersonation setup", "@1")
			result.markRemainingNotRun("impersonation setup", "@2")
			return result
		}
		defer syscall.CloseHandle(duplicate)
		start := time.Now()
		ok, _, impErr := procImpersonateLoggedOnUser.Call(uintptr(duplicate))
		if ok == 0 {
			result.stageInfo("impersonate", time.Since(start), fmt.Sprintf("ImpersonateLoggedOnUser failed: %v", impErr))
			result.Err = fmt.Errorf("impersonate the network-only token: %w", impErr)
			result.markRemainingNotRun("impersonation setup", "@1")
			result.markRemainingNotRun("impersonation setup", "@2")
			return result
		}
		impersonated = true
		result.stageInfo("impersonate", time.Since(start), "ImpersonateLoggedOnUser ok (held across both sequences)")
	} else {
		result.stageNotRun("impersonate", "no token supplied: ambient process identity")
	}
	defer func() {
		if !impersonated {
			result.stageNotRun("cleanup", "nothing impersonated")
			return
		}
		start := time.Now()
		ok, _, revertErr := procRevertToSelf.Call()
		if ok == 0 {
			result.stageInfo("cleanup", time.Since(start), fmt.Sprintf("RevertToSelf failed: %v", revertErr))
			result.Err = fmt.Errorf("revert held probe identity: %w", revertErr)
		} else {
			result.stageInfo("cleanup", time.Since(start), "RevertToSelf ok")
		}
	}()
	start := time.Now()
	result.Before = wmiProbeCaptureThreadToken()
	result.stageInfo("token-before", time.Since(start), result.Before.identityDetail(source))
	start = time.Now()
	hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded)
	label := "initialization failed"
	if hr == 0 {
		label = "S_OK new-MA-on-thread, held for both sequences"
	} else if hr == 1 {
		label = "S_FALSE thread-already-in-COM, held for both sequences"
	}
	result.stageHR("co-init", hr, time.Since(start), label)
	if hr != 0 && hr != 1 {
		result.Err = fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
		result.markRemainingNotRun("co-init", "@1")
		result.markRemainingNotRun("co-init", "@2")
		return result
	}
	defer func() {
		start := time.Now()
		procCoUninitialize.Call()
		result.stageInfo("co-uninit", time.Since(start), "single teardown after held initialization")
	}()
	start = time.Now()
	initErr := initializeWMISecurity()
	result.stageInfo("com-security", time.Since(start), wmiSecurityInitReport())
	if initErr != nil {
		result.Err = initErr
		result.markRemainingNotRun("com-security blocked the chain", "@1")
		result.markRemainingNotRun("com-security blocked the chain", "@2")
		return result
	}
	result.ConnectHR = result.connectSequence(host, nil, "@1")
	result.SecondConnectHR = result.connectSequence(host, nil, "@2")
	return result
}

// wmiProbeHeldVerdict interprets the held run under the evidence
// discipline: it compares against the isolated mode's known behavior but
// never attributes a cause the recorded evidence does not support.
func wmiProbeHeldVerdict(host string, held wmiProbeResult) string {
	switch {
	case held.Err != nil:
		return fmt.Sprintf("verdict inconclusive: held-mode machinery failed (%v)", held.Err)
	case held.ConnectHR != 0:
		return fmt.Sprintf("verdict inconclusive: the first held connect did not succeed (connect=0x%08x); fix the probe environment (credentials, target reachability) before comparing lifetimes",
			held.ConnectHR)
	case held.SecondConnectHR == 0:
		return fmt.Sprintf("verdict: second connect succeeded within one continuous COM initialization (first held connect also 0x%08x)."+
			" Held mode changes two variables at once versus the isolated mode — the COM lifecycle and the impersonation continuity — so this success narrows the suspects to the two without singling one out (it does not by itself attribute the failure to CoUninitialize)."+
			" Run `__WMI_PROBE__ %s cycled` on a fresh agent process (COM held, impersonation reverted and reapplied) to separate them, and correlate the target events checklist.",
			held.ConnectHR, host)
	}
	return fmt.Sprintf("verdict: second connect denied even within one continuous COM initialization (0x%08x)."+
		" The denial trigger is neither the per-attempt COM teardown nor the impersonation cycling alone: state from earlier connections in this process or a target-side rejection remain candidates."+
		" Correlate the target events below and, ideally, run both modes on fresh agent processes before attributing a cause.",
		held.SecondConnectHR)
}

// wmiProbeCycled runs the discriminating comparison between the held and
// isolated lifecycles: both connections execute inside one continuous COM
// initialization (single CoInitializeEx, no CoUninitialize between them,
// same OS thread), but the impersonation is reverted and reapplied from a
// fresh duplicate between the two connects — mirroring the identity
// handling the default mode performs per attempt, while the COM lifetime
// matches the held mode. If both connects succeed here, the impersonation
// cycling alone does not explain the isolated mode's denial and the COM
// lifecycle becomes the stronger suspect; a denied second connect makes
// the impersonation cycling — or its interaction with COM/RPC state — the
// stronger suspect. Cycled mode requires an active make_token.
func wmiProbeCycled(host string, token syscall.Token, source wmiProbeToken) (result wmiProbeResult) {
	result = wmiProbeResult{StartUTC: wmiProbeTimestamp()}
	pid, _, _ := procProbeGetCurrentProcessId.Call()
	result.PID = uint32(pid)
	defer func() { result.EndUTC = wmiProbeTimestamp() }()
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	tid, _, _ := procProbeGetCurrentThreadId.Call()
	result.TID = uint32(tid)
	var duplicate syscall.Handle
	process := syscall.Handle(^uintptr(0))
	if err := syscall.DuplicateHandle(process, syscall.Handle(token), process, &duplicate, 0, false, 2); err != nil {
		result.Err = fmt.Errorf("duplicate the network-only token: %w", err)
		result.stageNotRun("impersonate", err.Error())
		result.markRemainingNotRun("impersonation setup", "@1")
		result.markRemainingNotRun("impersonation setup", "@2")
		return result
	}
	defer syscall.CloseHandle(duplicate)
	start := time.Now()
	ok, _, impErr := procImpersonateLoggedOnUser.Call(uintptr(duplicate))
	if ok == 0 {
		result.stageInfo("impersonate", time.Since(start), fmt.Sprintf("ImpersonateLoggedOnUser failed: %v", impErr))
		result.Err = fmt.Errorf("impersonate the network-only token: %w", impErr)
		result.markRemainingNotRun("impersonation setup", "@1")
		result.markRemainingNotRun("impersonation setup", "@2")
		return result
	}
	result.stageInfo("impersonate", time.Since(start), "ImpersonateLoggedOnUser ok (first cycle)")
	defer func() {
		start := time.Now()
		ok, _, err := procRevertToSelf.Call()
		if ok == 0 {
			result.stageInfo("cleanup", time.Since(start), fmt.Sprintf("RevertToSelf failed: %v", err))
			result.Err = fmt.Errorf("revert cycled probe identity: %w", err)
		} else {
			result.stageInfo("cleanup", time.Since(start), "RevertToSelf ok")
		}
	}()
	start = time.Now()
	result.Before = wmiProbeCaptureThreadToken()
	result.stageInfo("token-before", time.Since(start), result.Before.identityDetail(source))
	start = time.Now()
	hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded)
	label := "initialization failed"
	if hr == 0 {
		label = "S_OK new-MA-on-thread, held across the impersonation cycle"
	} else if hr == 1 {
		label = "S_FALSE thread-already-in-COM, held across the impersonation cycle"
	}
	result.stageHR("co-init", hr, time.Since(start), label)
	if hr != 0 && hr != 1 {
		result.Err = fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
		result.markRemainingNotRun("co-init", "@1")
		result.markRemainingNotRun("co-init", "@2")
		return result
	}
	defer func() {
		start := time.Now()
		procCoUninitialize.Call()
		result.stageInfo("co-uninit", time.Since(start), "single teardown after cycled initialization")
	}()
	start = time.Now()
	initErr := initializeWMISecurity()
	result.stageInfo("com-security", time.Since(start), wmiSecurityInitReport())
	if initErr != nil {
		result.Err = initErr
		result.markRemainingNotRun("com-security blocked the chain", "@1")
		result.markRemainingNotRun("com-security blocked the chain", "@2")
		return result
	}
	result.ConnectHR = result.connectSequence(host, nil, "@1")
	// The cycle: mirror the default mode's per-attempt identity handling —
	// revert, then reapply a fresh duplicate — while the COM apartment and
	// its process-wide security stay initialized.
	start = time.Now()
	ok, _, revertErr := procRevertToSelf.Call()
	if ok == 0 {
		result.stageInfo("revert", time.Since(start), fmt.Sprintf("RevertToSelf failed: %v", revertErr))
		result.Err = fmt.Errorf("revert between probe connections: %w", revertErr)
		result.markRemainingNotRun("identity revert failed", "@2")
		return result
	} else {
		result.stageInfo("revert", time.Since(start), "RevertToSelf ok (between the connects)")
	}
	start = time.Now()
	reverted := wmiProbeCaptureThreadToken()
	result.stageInfo("token-reverted", time.Since(start), reverted.identityDetail(result.Before))
	var nextDuplicate syscall.Handle
	if err := syscall.DuplicateHandle(process, syscall.Handle(token), process, &nextDuplicate, 0, false, 2); err != nil {
		result.Err = fmt.Errorf("duplicate the network-only token for the second cycle: %w", err)
		result.stageNotRun("reimpersonate", err.Error())
		result.markRemainingNotRun("reimpersonation setup", "@2")
		return result
	}
	defer syscall.CloseHandle(nextDuplicate)
	start = time.Now()
	ok, _, reimpErr := procImpersonateLoggedOnUser.Call(uintptr(nextDuplicate))
	if ok == 0 {
		result.stageInfo("reimpersonate", time.Since(start), fmt.Sprintf("ImpersonateLoggedOnUser failed: %v", reimpErr))
		result.Err = fmt.Errorf("reapply the network-only token: %w", reimpErr)
		result.markRemainingNotRun("reimpersonation setup", "@2")
		return result
	}
	result.stageInfo("reimpersonate", time.Since(start), "ImpersonateLoggedOnUser ok (fresh duplicate, second cycle)")
	start = time.Now()
	reapplied := wmiProbeCaptureThreadToken()
	result.stageInfo("token-reapplied", time.Since(start), reapplied.identityDetail(source))
	result.SecondConnectHR = result.connectSequence(host, nil, "@2")
	return result
}

// wmiProbeCycledVerdict interprets the cycled run under the decision rules
// the comparison was designed for, keeping the evidence discipline: COM
// lifecycle and impersonation cycling come out as stronger or weaker
// suspects, never as proven causes, and the target-event correlation keeps
// its inferred-assignment caveat.
func wmiProbeCycledVerdict(host string, cycled wmiProbeResult) string {
	switch {
	case cycled.Err != nil:
		return fmt.Sprintf("verdict inconclusive: cycled-mode machinery failed (%v)", cycled.Err)
	case cycled.ConnectHR != 0:
		return fmt.Sprintf("verdict inconclusive: the first cycled connect did not succeed (connect=0x%08x); fix the probe environment before reading the comparison",
			cycled.ConnectHR)
	case cycled.SecondConnectHR == 0:
		return fmt.Sprintf("verdict: second connect succeeded with COM held and the impersonation reverted/reapplied between the connects (first connect 0x%08x)."+
			" Impersonation cycling alone does not reproduce the isolated mode's denial here, so the per-attempt COM initialization/teardown lifecycle becomes the stronger suspect — attribution remains inferred, not proven: corroborate with the target events checklist and re-run the compared modes on fresh agent processes.",
			cycled.ConnectHR)
	}
	return fmt.Sprintf("verdict: second connect denied with COM held and the impersonation reverted/reapplied between the connects (0x%08x)."+
		" The impersonation cycling — or its interaction with COM/RPC state — becomes the stronger suspect; the COM lifecycle alone cannot explain this denial. Attribution remains inferred, not proven: correlate the target events below.",
		cycled.SecondConnectHR)
}

// wmiProbeChecklist prints the target-side correlation guidance, keyed to
// the probes' UTC timestamps. Security events on the target record local
// time, so the collection window stays generous; correlate by source
// address and account when the window is ambiguous. Field names differ
// between 4624 and 4625, so the example reads the full message rather than
// fixed property indices.
func wmiProbeChecklist(host, startUTC, endUTC string) string {
	return "target evidence (collect on " + host + ", local time applies to Security events):\n" +
		"  Get-WinEvent -FilterHashtable @{LogName='Security'; Id=@(4624,4625)} -MaxEvents 400 |\n" +
		"    Where-Object { $_.TimeCreated -gt (Get-Date).AddHours(-3) } |\n" +
		"    Select-Object TimeCreated, Id, RecordId, Message | Format-List\n" +
		"  probes ran between " + startUTC + " and " + endUTC + " UTC; match on this agent host's source address.\n" +
		"  record per attempt (RecordId pins each event unambiguously): account and domain, authentication package,\n" +
		"  logon type, source address; on 4625 additionally the Status and SubStatus codes.\n" +
		"  caveat: Security timestamps have second resolution, so event-to-call assignment without RecordId is inferred.\n" +
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

// connectAndQuery performs the instrumented read-only chain for the
// isolated mode: COM initialization, the once-per-process COM security
// report, then one full connect sequence. When an upstream stage fails,
// every downstream stage records itself as not_run rather than carrying a
// zero result.
func (r *wmiProbeResult) connectAndQuery(host string, identity *winrmCredentials) {
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
		r.markRemainingNotRun("co-init", "")
		return
	}
	defer procCoUninitialize.Call()
	start = time.Now()
	initErr := initializeWMISecurity()
	r.stageInfo("com-security", time.Since(start), wmiSecurityInitReport())
	if initErr != nil {
		r.Err = initErr
		r.markRemainingNotRun("com-security blocked the chain", "")
		return
	}
	r.ConnectHR = r.connectSequence(host, identity, "")
}

// connectSequence runs one full instrumented connect — fresh locator,
// ConnectServer with a token snapshot immediately after, the blanket
// observed before and after applying it, and the class fetch — inside an
// already-initialized COM apartment. The suffix parameterizes the stage
// names so the held mode can record two sequences in one report. It
// returns the raw ConnectServer HRESULT.
func (r *wmiProbeResult) connectSequence(host string, identity *winrmCredentials, suffix string) uintptr {
	authnService, ambientAuthority, serverPrincipal, err := wmiAuthentication(host)
	if err != nil {
		r.Err = err
		r.markRemainingNotRun("authentication inputs", suffix)
		return 0
	}
	defer sysFreeString(ambientAuthority)
	defer runtime.KeepAlive(serverPrincipal)
	var identityUser, identityPassword, identityAuthority uintptr
	var blanket *secWinNTAuthIdentity
	if identity != nil {
		userPtr, passwordPtr, authorityPtr, blanketPtr, credErr := wmiConnectIdentity(identity)
		if credErr != nil {
			r.Err = credErr
			r.markRemainingNotRun("credential encoding", suffix)
			return 0
		}
		identityUser, identityPassword, identityAuthority, blanket = userPtr, passwordPtr, authorityPtr, blanketPtr
		defer sysFreeString(identityUser)
		defer sysFreeString(identityPassword)
		defer sysFreeString(identityAuthority)
		defer runtime.KeepAlive(blanket)
	}
	start := time.Now()
	var locator uintptr
	hr, _, _ := procCoCreateInstance.Call(
		uintptr(unsafe.Pointer(&clsidWbemLocator)), 0, clsctxInprocServer,
		uintptr(unsafe.Pointer(&iidIWbemLocator)), uintptr(unsafe.Pointer(&locator)))
	r.stageHR("cocreate"+suffix, hr, time.Since(start), "WbemLocator in-proc")
	if hr != 0 || locator == 0 {
		r.Err = fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x", hr)
		r.markRemainingNotRun("cocreate"+suffix, suffix)
		return 0
	}
	defer comRelease(locator)
	namespace := sysAllocString(`\\` + host + `\root\cimv2`)
	defer sysFreeString(namespace)
	if namespace == 0 {
		r.Err = fmt.Errorf("allocate namespace string")
		r.markRemainingNotRun("namespace allocation"+suffix, suffix)
		return 0
	}
	authority, authnService := ambientAuthority, authnService
	blanketMode := "dynamic-cloaking"
	if identity != nil {
		authority, authnService = identityAuthority, rpcCAuthnWinnt
		blanketMode = "explicit-SEC_WINNT_AUTH_IDENTITY"
	}
	var services uintptr
	start = time.Now()
	connectHR := comCallHR(locator, 3, namespace, identityUser, identityPassword, 0, 0,
		authority, 0, uintptr(unsafe.Pointer(&services)))
	r.stageHR("connect"+suffix, connectHR, time.Since(start), fmt.Sprintf(`\\%s\root\cimv2 authn=%d(%s)`,
		host, authnService, authnServiceLabel(uint32(authnService))))
	start = time.Now()
	r.After = wmiProbeCaptureThreadToken()
	r.stageInfo("token-after"+suffix, time.Since(start), r.After.identityDetail(r.Before))
	if connectHR != 0 || services == 0 {
		r.markRemainingNotRun("connect denied: no proxy exists"+suffix, suffix)
		return connectHR
	}
	defer comRelease(services)
	start = time.Now()
	before, blanketErr := wmiProbeBlanket(services)
	if blanketErr != nil {
		r.stageNotRun("blanket-before"+suffix, "CoQueryProxyBlanket failed: "+blanketErr.Error())
	} else {
		r.stageInfo("blanket-before"+suffix, time.Since(start), before)
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
	r.stageHR("blanket"+suffix, blanketHR, time.Since(start), blanketMode)
	if blanketHR != 0 {
		r.markRemainingNotRun("blanket failed"+suffix, suffix)
		return connectHR
	}
	if after, err := wmiProbeBlanket(services); err != nil {
		r.stageNotRun("blanket-after"+suffix, "CoQueryProxyBlanket failed: "+err.Error())
	} else {
		r.stageInfo("blanket-after"+suffix, 0, after)
	}
	classPath := sysAllocString("Win32_ComputerSystem")
	defer sysFreeString(classPath)
	if classPath == 0 {
		r.Err = fmt.Errorf("allocate class path string")
		r.stageNotRun("query"+suffix, "class path allocation failed")
		return connectHR
	}
	var classObject uintptr
	start = time.Now()
	queryHR := comCallHR(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0)
	r.stageHR("query"+suffix, queryHR, time.Since(start), "GetObject Win32_ComputerSystem")
	if classObject != 0 {
		comRelease(classObject)
	}
	return connectHR
}

// markRemainingNotRun records the stages after a failure point — within the
// connect sequence identified by suffix — so no unexecuted step can appear
// as a succeeded zero result.
func (r *wmiProbeResult) markRemainingNotRun(failedAt, suffix string) {
	why := "upstream stage failed: " + failedAt
	executed := map[string]bool{}
	for _, s := range r.Stages {
		executed[s.name] = true
	}
	for _, name := range []string{"cocreate", "connect", "token-after", "blanket-before", "blanket", "blanket-after", "query"} {
		if !executed[name+suffix] {
			r.stageNotRun(name+suffix, why)
		}
	}
}

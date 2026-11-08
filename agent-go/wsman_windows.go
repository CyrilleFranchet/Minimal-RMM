//go:build windows

package main

import (
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"
)

const (
	wsmanShellCmdURI       = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd"
	wsmanCommandStateDone  = "/CommandState/Done"
	wsmanStreamReadLimit   = 128
	wsmanCompletionTimeout = 30 * time.Second
	wsmanEndOfOperation    = 1
	// WSManAuthenticationFlags values from wsman.h. The native WSMan client
	// exposes no direct NTLM flag; IP targets normally negotiate NTLM.
	wsmanAuthNegotiate                     = 0x4
	wsmanAuthKerberos                      = 0x10
	wsmanAllowNegotiateImplicitCredentials = 32
	wsmanDataTypeDWORD                     = 4
	wsmanSkipCACheck                       = 18
	wsmanSkipCNCheck                       = 19
	wsmanSkipRevocationCheck               = 31
)

// wsmanCaller isolates native dispatch for Windows ABI regression fixtures.
type wsmanCaller interface {
	call(string, ...uintptr) uintptr
}
type wsmanNative map[string]*syscall.LazyProc

func (n wsmanNative) call(name string, args ...uintptr) uintptr {
	value, _, _ := n[name].Call(args...)
	return value
}
func loadWSMan() (wsmanNative, error) {
	dll := syscall.NewLazyDLL("WsmSvc.dll")
	native := wsmanNative{}
	for _, name := range []string{
		"WSManInitialize", "WSManDeinitialize", "WSManCreateSession", "WSManCloseSession",
		"WSManCreateShell", "WSManCloseShell", "WSManRunShellCommand", "WSManCloseCommand",
		"WSManReceiveShellOutput", "WSManCloseOperation",
		"WSManSetSessionOption", "WSManSendShellInput",
	} {
		proc := dll.NewProc(name)
		if err := proc.Find(); err != nil {
			return nil, fmt.Errorf("%s unavailable: %w", name, err)
		}
		native[name] = proc
	}
	return native, nil
}

// SDK layouts for the supported Windows amd64 and arm64 targets.
type wsmanShellAsync struct{ context, callback uintptr }
type wsmanStringSet struct {
	count  uint32
	values **uint16
}
type wsmanData struct {
	kind   uint32
	_      uint32
	length uint32
	_      uint32
	value  uintptr
}
type wsmanReceiveData struct {
	streamID     *uint16
	streamData   wsmanData
	commandState *uint16
	exitCode     uint32
}

// wsmanError mirrors WSMAN_ERROR; its strings are valid only in the callback.
type wsmanError struct {
	code                              uint32
	detail, language, machine, plugin *uint16
}

// wsmanAuthCredentials mirrors WSMAN_AUTHENTICATION_CREDENTIALS for explicit
// accounts or an explicitly selected mechanism with implicit credentials.
type wsmanAuthCredentials struct {
	mechanism uint32
	_         uint32
	username  uintptr
	password  uintptr
}

// wsmanAuthMechanism selects the explicit-credential mechanism from the
// target's written form: Kerberos for DNS names, Negotiate (NTLM against an
// IP target under default Windows configuration) for IP literals.
func wsmanAuthMechanism(host string) uint32 {
	if lateralHostIsIP(host) {
		return wsmanAuthNegotiate
	}
	return wsmanAuthKerberos
}

// wsmanResult retains copied values rather than native callback pointers.
type wsmanResult struct {
	mu          sync.Mutex
	done        chan struct{}
	receive     bool
	finished    bool
	err         error
	commandDone bool
	exitCode    uint32
	output      string
}
type wsmanPending struct {
	async  wsmanShellAsync
	result *wsmanResult
}

var wsmanContexts sync.Map
var wsmanContextID atomic.Uint64

// wsmanScope roots asynchronous inputs through synchronous session teardown,
// including callbacks that arrive after a local timeout or CloseOperation.
type wsmanScope struct {
	pending []*wsmanPending
	roots   []any
}

func (s *wsmanScope) keep(value any) { s.roots = append(s.roots, value) }
func (s *wsmanScope) pendingCall(receive bool) *wsmanPending {
	result := &wsmanResult{done: make(chan struct{}), receive: receive}
	context := uintptr(wsmanContextID.Add(1))
	wsmanContexts.Store(context, result)
	pending := &wsmanPending{async: wsmanShellAsync{context, wsmanCompletionCallback}, result: result}
	s.pending = append(s.pending, pending)
	return pending
}
func (s *wsmanScope) release() {
	for _, pending := range s.pending {
		wsmanContexts.Delete(pending.async.context)
	}
	runtime.KeepAlive(s)
}

// wsmanCompletion implements all seven WSMAN_SHELL_COMPLETION_FUNCTION slots.
// Only receive callbacks interpret the response union as receive data.
func wsmanCompletion(context, flags, errPtr, shell, command, operation, data uintptr) uintptr {
	value, ok := wsmanContexts.Load(context)
	if !ok {
		return 0
	}
	result := value.(*wsmanResult)
	result.mu.Lock()
	defer result.mu.Unlock()
	if result.finished {
		return 0
	}
	if errPtr != 0 {
		fault := (*wsmanError)(unsafe.Pointer(errPtr))
		code := fault.code
		if code != 0 {
			result.err = fmt.Errorf("WSMan error %d (0x%08x)", code, code)
			if detail := strings.TrimSpace(readWSManStringBounded(uintptr(unsafe.Pointer(fault.detail)), 4096)); detail != "" {
				result.err = fmt.Errorf("%w: %s", result.err, detail)
			}
		}
	}
	if result.err == nil && result.receive && data != 0 {
		receive := (*wsmanReceiveData)(unsafe.Pointer(data))
		// Retain only a bounded ownership acknowledgment, never file contents.
		stream := receive.streamData
		if readWSManString(uintptr(unsafe.Pointer(receive.streamID))) == "stdout" && stream.kind == 2 && stream.value != 0 && stream.length != 0 && len(result.output) < 4096 {
			length := int(stream.length)
			if length > 4096-len(result.output) {
				length = 4096 - len(result.output)
			}
			result.output += string(unsafe.Slice((*byte)(unsafe.Pointer(stream.value)), length))
		}
		if strings.HasSuffix(readWSManString(uintptr(unsafe.Pointer(receive.commandState))), wsmanCommandStateDone) {
			result.commandDone = true
			result.exitCode = receive.exitCode
		}
	}
	// A stream block or end-of-stream flag alone does not finish a receive.
	if flags&wsmanEndOfOperation != 0 || result.err != nil || result.commandDone {
		result.finished = true
		close(result.done)
	}
	return 0
}

var wsmanCompletionCallback = syscall.NewCallback(wsmanCompletion)

func (r *wsmanResult) wait(timeout time.Duration) bool {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-r.done:
		return true
	case <-timer.C:
		return false
	}
}
func (r *wsmanResult) snapshot() (error, bool, uint32) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.err, r.commandDone, r.exitCode
}

// readWSManString copies a bounded command-state URI during its callback.
func readWSManString(pointer uintptr) string {
	return readWSManStringBounded(pointer, wsmanStreamReadLimit)
}

// readWSManStringBounded copies native strings within the callback lifetime,
// allowing longer fault details while keeping command-state reads short.
func readWSManStringBounded(pointer uintptr, limit int) string {
	if pointer == 0 {
		return ""
	}
	runes := make([]uint16, 0, limit)
	for i := 0; i < limit; i++ {
		char := *(*uint16)(unsafe.Pointer(pointer + uintptr(i)*2))
		if char == 0 {
			break
		}
		runes = append(runes, char)
	}
	return syscall.UTF16ToString(runes)
}

// closeHandle uses the mandatory close callback. Enclosing session teardown
// drains any remaining callbacks if this bounded wait times out.
func (s *wsmanScope) closeHandle(native wsmanCaller, name string, handle uintptr, timeout time.Duration) error {
	if handle == 0 {
		return nil
	}
	pending := s.pendingCall(false)
	native.call(name, handle, 0, uintptr(unsafe.Pointer(&pending.async)))
	if !pending.result.wait(timeout) {
		return fmt.Errorf("%s timed out", name)
	}
	err, _, _ := pending.result.snapshot()
	if err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

// winrmDeployAgent streams the local executable through WSMan and starts it
// in memory inside the receiver, using the same authentication and TLS
// policy for transfer and launch. The receiver maps the received bytes into
// a PAGE_EXECUTE_READWRITE allocation and starts the agent on a detached
// thread; the WinRM shell must then stay open for the agent's lifetime.
func winrmDeployAgent(host, sourcePath, sessionID string, workspaceID *string, identity *winrmCredentials, useHTTPS, insecure bool) error {
	source, err := os.Open(sourcePath)
	if err != nil {
		return fmt.Errorf("open local agent for WinRM: %w", err)
	}
	defer source.Close()
	transfer, err := prepareWinRMTransfer(source)
	if err != nil {
		return fmt.Errorf("prepare WinRM transfer: %w", err)
	}
	// Initialize the shared COM security policy before WSMan can initialize
	// it implicitly, so a later WMI command can use thread-token cloaking.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded); hr != 0 && hr != 1 {
		return fmt.Errorf("CoInitializeEx for WinRM failed: 0x%08x", hr)
	}
	defer procCoUninitialize.Call()
	if err := initializeWMISecurity(); err != nil {
		return err
	}
	native, err := loadWSMan()
	if err != nil {
		return err
	}
	return winrmExecuteWithTLSAPI(native, host, winrmReceiverCommandPath,
		winrmReceiverArguments(sessionID, workspaceID, transfer),
		wsmanCompletionTimeout, identity, useHTTPS, insecure, transfer, true)
}

func winrmCreateProcessWithAPI(native wsmanCaller, host, commandPath string, arguments []string, timeout time.Duration) (resultErr error) {
	return winrmCreateProcessWithIdentityAPI(native, host, commandPath, arguments, timeout, nil)
}

// winrmCreateProcessWithIdentityAPI supplies selected make_token credentials
// explicitly to WSMan; an absent identity retains default thread credentials.
func winrmCreateProcessWithIdentityAPI(native wsmanCaller, host, commandPath string, arguments []string, timeout time.Duration, identity *winrmCredentials) (resultErr error) {
	return winrmCreateProcessWithTransportAPI(native, host, commandPath, arguments, timeout, identity, false)
}

// winrmCreateProcessWithTransportAPI selects HTTP/5985 or HTTPS/5986 while
// leaving native server-certificate and authentication validation enabled.
func winrmCreateProcessWithTransportAPI(native wsmanCaller, host, commandPath string, arguments []string, timeout time.Duration, identity *winrmCredentials, useHTTPS bool) (resultErr error) {
	return winrmCreateProcessWithTLSAPI(native, host, commandPath, arguments, timeout, identity, useHTTPS, false)
}

// winrmCreateProcessWithTLSAPI scopes optional certificate bypass to this
// HTTPS session. It never changes machine certificate stores or TrustedHosts.
func winrmCreateProcessWithTLSAPI(native wsmanCaller, host, commandPath string, arguments []string, timeout time.Duration, identity *winrmCredentials, useHTTPS, insecure bool) (resultErr error) {
	return winrmExecuteWithTLSAPI(native, host, commandPath, arguments, timeout, identity, useHTTPS, insecure, nil, false)
}

// winrmHeldShell records one live in-memory deployment: the receiver command
// never completes, so its WinRM command, shell, session, and API handles
// must outlive the lateral worker that created them.
type winrmHeldShell struct {
	native              wsmanCaller
	api, session, shell uintptr
	command             uintptr
}

var winrmHeldShells = struct {
	sync.Mutex
	shells []*winrmHeldShell
}{}

// holdWinRMShell keeps a completed transfer's native handles open; closing
// them would terminate the receiver process tree and the in-memory agent.
func holdWinRMShell(deployment *winrmHeldShell) {
	winrmHeldShells.Lock()
	defer winrmHeldShells.Unlock()
	winrmHeldShells.shells = append(winrmHeldShells.shells, deployment)
}

// stopHeldWinRMShells closes every shell held open for an in-memory WinRM
// deployment. It runs at agent shutdown: the deployed sessions die with
// their source agent by design.
func stopHeldWinRMShells() {
	winrmHeldShells.Lock()
	shells := winrmHeldShells.shells
	winrmHeldShells.shells = nil
	winrmHeldShells.Unlock()
	for _, deployment := range shells {
		scope := &wsmanScope{}
		_ = scope.closeHandle(deployment.native, "WSManCloseCommand", deployment.command, wsmanCompletionTimeout)
		_ = scope.closeHandle(deployment.native, "WSManCloseShell", deployment.shell, wsmanCompletionTimeout)
		_ = scope.closeHandle(deployment.native, "WSManCloseSession", deployment.session, wsmanCompletionTimeout)
		if code := deployment.native.call("WSManDeinitialize", deployment.api, 0); code != 0 {
			debugEvent("winrm.held.deinitialize.failed", fmt.Errorf("WSManDeinitialize failed: 0x%08x", code))
		}
		scope.release()
	}
}

// winrmExecuteWithTLSAPI shares the session policy between process-only ABI
// fixtures and the production transfer-and-hold path. When hold is set, a
// successful transfer moves the native handles into the held-shell registry
// instead of tearing the session down: the receiver never completes.
func winrmExecuteWithTLSAPI(native wsmanCaller, host, commandPath string, arguments []string, timeout time.Duration, identity *winrmCredentials, useHTTPS, insecure bool, transfer *winrmTransfer, hold bool) (resultErr error) {
	if insecure && !useHTTPS {
		return errors.New("insecure requires winrm HTTPS")
	}
	held := false
	scope := &wsmanScope{}
	defer scope.release()
	// Roots outlive deferred CloseSession/Deinitialize, which drain callbacks.
	utf16 := func(value string) (*uint16, error) {
		pointer, err := syscall.UTF16PtrFromString(value)
		if err == nil {
			scope.keep(pointer)
		}
		return pointer, err
	}
	scheme, port := "http", "5985"
	if useHTTPS {
		scheme, port = "https", "5986"
	}
	connection, err := utf16(scheme + "://" + net.JoinHostPort(host, port) + "/wsman")
	if err != nil {
		return err
	}
	// Keep mechanism selection separate from the explicit/default credential
	// source. A netonly thread token alone need not supply reusable WSMan creds.
	credentials := wsmanAuthCredentials{mechanism: wsmanAuthMechanism(host)}
	// HTTPS can authenticate the server while Negotiate uses the current
	// process credentials. This is PowerShell's NegotiateWithImplicitCredential
	// mode; Default authentication plus option 32 is not equivalent for IP URLs.
	implicitNegotiate := identity == nil && useHTTPS
	if implicitNegotiate {
		credentials.mechanism = wsmanAuthNegotiate
	}
	if identity != nil {
		if identity.user == "" {
			return errors.New("WinRM selected identity has no username")
		}
		user := identity.user
		if identity.domain != "" {
			user = identity.domain + `\` + user
		}
		username, err := utf16(user)
		if err != nil {
			return errors.New("encode WinRM selected username")
		}
		password, err := syscall.UTF16FromString(identity.password)
		if err != nil {
			return errors.New("encode WinRM selected password")
		}
		// This defer runs after native session teardown, before scope release.
		defer func() {
			for i := range password {
				password[i] = 0
			}
		}()
		scope.keep(password)
		credentials.username = uintptr(unsafe.Pointer(username))
		credentials.password = uintptr(unsafe.Pointer(&password[0]))
	}
	scope.keep(&credentials)
	uri, err := utf16(wsmanShellCmdURI)
	if err != nil {
		return err
	}
	commandLine, err := utf16(commandPath)
	if err != nil {
		return err
	}
	argumentPointers := make([]*uint16, len(arguments))
	for i, argument := range arguments {
		argumentPointers[i], err = utf16(argument)
		if err != nil {
			return err
		}
	}
	scope.keep(argumentPointers)
	argumentSet := &wsmanStringSet{count: uint32(len(argumentPointers)), values: &argumentPointers[0]}
	scope.keep(argumentSet)
	var api, session, shell uintptr
	scope.keep(&api)
	scope.keep(&session)
	scope.keep(&shell)
	if code := native.call("WSManInitialize", 0, uintptr(unsafe.Pointer(&api))); code != 0 {
		return fmt.Errorf("WSManInitialize failed: 0x%08x", code)
	}
	defer func() {
		if held {
			return
		}
		if code := native.call("WSManDeinitialize", api, 0); code != 0 {
			resultErr = errors.Join(resultErr, fmt.Errorf("WSManDeinitialize failed: 0x%08x", code))
		}
	}()
	// HTTP defaults use NULL. HTTPS process identity explicitly selects
	// Negotiate with empty account fields; option 32 enables implicit creds.
	var authentication uintptr
	if identity != nil || implicitNegotiate {
		authentication = uintptr(unsafe.Pointer(&credentials))
	}
	if code := native.call("WSManCreateSession", api, uintptr(unsafe.Pointer(connection)), 0, authentication, 0, uintptr(unsafe.Pointer(&session))); code != 0 {
		return fmt.Errorf("WSManCreateSession(%s) failed: 0x%08x", host, code)
	}
	defer func() {
		if held {
			return
		}
		if code := native.call("WSManCloseSession", session, 0); code != 0 {
			resultErr = errors.Join(resultErr, fmt.Errorf("WSManCloseSession failed: 0x%08x", code))
		}
	}()
	if implicitNegotiate {
		// The DWORD union member is at offset 8, shared with binary length.
		// This enables credentials for this session independently of the
		// explicit certificate options below. It does not modify TrustedHosts.
		option := wsmanData{kind: wsmanDataTypeDWORD, length: 1}
		if code := native.call("WSManSetSessionOption", session,
			wsmanAllowNegotiateImplicitCredentials, uintptr(unsafe.Pointer(&option))); code != 0 {
			return fmt.Errorf("WSManSetSessionOption(implicit Negotiate credentials) failed: 0x%08x", code)
		}
		runtime.KeepAlive(&option)
	}
	if insecure {
		// WSMAN_OPTION_SKIP_CA_CHECK, SKIP_CN_CHECK, SKIP_REVOCATION_CHECK.
		option := wsmanData{kind: wsmanDataTypeDWORD, length: 1}
		for _, setting := range []uintptr{wsmanSkipCACheck, wsmanSkipCNCheck, wsmanSkipRevocationCheck} {
			if code := native.call("WSManSetSessionOption", session, setting, uintptr(unsafe.Pointer(&option))); code != 0 {
				return fmt.Errorf("WSManSetSessionOption(certificate check %d) failed: 0x%08x", setting, code)
			}
		}
		runtime.KeepAlive(&option)
	}
	shellCall := scope.pendingCall(false)
	native.call("WSManCreateShell", session, 0, uintptr(unsafe.Pointer(uri)), 0, 0, 0,
		uintptr(unsafe.Pointer(&shellCall.async)), uintptr(unsafe.Pointer(&shell)))
	defer func() {
		if held {
			return
		}
		resultErr = errors.Join(resultErr, scope.closeHandle(native, "WSManCloseShell", shell, timeout))
	}()
	if !shellCall.result.wait(timeout) {
		return errors.New("WinRM shell creation timed out")
	}
	if err, _, _ := shellCall.result.snapshot(); err != nil {
		mode := "default"
		if implicitNegotiate {
			mode = "implicit-negotiate"
		} else if identity != nil {
			mode = "explicit-negotiate"
			if credentials.mechanism == wsmanAuthKerberos {
				mode = "explicit-kerberos"
			}
		}
		certificateMode := "verify"
		if insecure {
			certificateMode = "skip-ca-cn-revocation"
		}
		return fmt.Errorf("WinRM shell on %s (%s; auth=%s; cert=%s): %w", host, scheme, mode, certificateMode, err)
	}
	if shell == 0 {
		return errors.New("WSManCreateShell returned no shell handle")
	}
	deployment := &winrmHeldShell{native: native, api: api, session: session, shell: shell}
	held, resultErr = scope.runCommand(native, shell, commandLine, argumentSet, host, timeout, transfer, hold, deployment)
	if held {
		holdWinRMShell(deployment)
	}
	return resultErr
}

// runCommand keeps all asynchronous arguments rooted until session teardown.
// A held deployment returns true after the transfer completes so the caller
// can keep the native handles open; failed transfers tear down normally.
func (scope *wsmanScope) runCommand(native wsmanCaller, shell uintptr, commandLine *uint16, argumentSet *wsmanStringSet, host string, timeout time.Duration, transfer *winrmTransfer, hold bool, deployment *winrmHeldShell) (held bool, resultErr error) {
	var command, receiveOperation uintptr
	scope.keep(&command)
	scope.keep(&receiveOperation)
	var receiveCall *wsmanPending
	defer func() {
		if held {
			return
		}
		if receiveOperation != 0 {
			if code := native.call("WSManCloseOperation", receiveOperation, 0); code != 0 {
				resultErr = errors.Join(resultErr, fmt.Errorf("WSManCloseOperation failed: 0x%08x", code))
			}
		}
		resultErr = errors.Join(resultErr, scope.closeHandle(native, "WSManCloseCommand", command, timeout))
	}()
	commandCall := scope.pendingCall(false)
	native.call("WSManRunShellCommand", shell, 0, uintptr(unsafe.Pointer(commandLine)), uintptr(unsafe.Pointer(argumentSet)), 0,
		uintptr(unsafe.Pointer(&commandCall.async)), uintptr(unsafe.Pointer(&command)))
	if !commandCall.result.wait(timeout) {
		return false, errors.New("WinRM command creation timed out")
	}
	if err, _, _ := commandCall.result.snapshot(); err != nil {
		return false, fmt.Errorf("WinRM command on %s: %w", host, err)
	}
	if command == 0 {
		return false, errors.New("WSManRunShellCommand returned no command handle")
	}
	if hold {
		// The receiver maps the agent in memory and never completes; there
		// is no command result to receive. The shell stays open in the
		// held-shell registry once the last input packet is acknowledged.
		if err := scope.sendInput(native, shell, command, transfer, timeout); err != nil {
			return false, err
		}
		deployment.command = command
		return true, nil
	}
	receiveCall = scope.pendingCall(true)
	// NULL requests all output streams. One receive produces multiple callbacks.
	native.call("WSManReceiveShellOutput", shell, command, 0, 0, uintptr(unsafe.Pointer(&receiveCall.async)), uintptr(unsafe.Pointer(&receiveOperation)))
	if transfer != nil {
		if err := scope.sendInput(native, shell, command, transfer, timeout); err != nil {
			return false, err
		}
	}
	if !receiveCall.result.wait(2 * timeout) {
		return false, errors.New("WinRM detach completion not confirmed before timeout")
	}
	if err, done, exitCode := receiveCall.result.snapshot(); err != nil {
		return false, fmt.Errorf("WinRM receive on %s: %w", host, err)
	} else if !done {
		return false, errors.New("WinRM receive ended without command completion")
	} else if exitCode != 0 {
		return false, fmt.Errorf("WinRM detach command exited with %d", exitCode)
	}
	return false, nil
}

// sendInput sends bounded binary WSMan stdin packets and an explicit EOF.
// Pending buffers remain alive through operation cancellation and session drain.
func (scope *wsmanScope) sendInput(native wsmanCaller, shell, command uintptr, transfer *winrmTransfer, timeout time.Duration) error {
	stdin, _ := syscall.UTF16PtrFromString("stdin")
	scope.keep(stdin)
	source := io.LimitReader(transfer.source, transfer.size+1)
	var total int64
	for {
		buffer := make([]byte, winrmTransferChunkSize)
		n, readErr := source.Read(buffer)
		if readErr != nil && readErr != io.EOF {
			return fmt.Errorf("read local WinRM agent: %w", readErr)
		}
		total += int64(n)
		if total > transfer.size || (readErr == io.EOF && total != transfer.size) {
			return errors.New("WinRM local agent length changed during transfer")
		}
		if n == 0 && readErr == nil {
			return errors.New("WinRM local agent reader made no progress")
		}
		data := &wsmanData{kind: 2, length: uint32(n)}
		if n > 0 {
			data.value = uintptr(unsafe.Pointer(&buffer[0]))
		}
		scope.keep(buffer)
		scope.keep(data)
		pending := scope.pendingCall(false)
		operation := new(uintptr)
		scope.keep(operation)
		var eof uintptr
		if readErr == io.EOF {
			eof = 1
		}
		native.call("WSManSendShellInput", shell, command, 0, uintptr(unsafe.Pointer(stdin)), uintptr(unsafe.Pointer(data)), eof,
			uintptr(unsafe.Pointer(&pending.async)), uintptr(unsafe.Pointer(operation)))
		completed := pending.result.wait(timeout)
		var closeErr error
		if *operation != 0 {
			if code := native.call("WSManCloseOperation", *operation, 0); code != 0 {
				closeErr = fmt.Errorf("close WinRM input operation: 0x%08x", code)
			}
		}
		if !completed {
			return errors.Join(errors.New("WinRM agent input timed out"), closeErr)
		}
		if err, _, _ := pending.result.snapshot(); err != nil || closeErr != nil {
			return errors.Join(fmt.Errorf("WinRM agent input failed"), err, closeErr)
		}
		if eof != 0 {
			return nil
		}
	}
}

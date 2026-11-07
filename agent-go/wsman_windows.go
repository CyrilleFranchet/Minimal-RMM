//go:build windows

package main

import (
	"errors"
	"fmt"
	"net"
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
		"WSManSetSessionOption",
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

// wsmanAuthCredentials mirrors WSMAN_AUTHENTICATION_CREDENTIALS: the
// mechanism selects the authentication family while the null user account
// keeps the implicit thread identity, which carries the active make_token
// credentials when one is set.
type wsmanAuthCredentials struct {
	mechanism uint32
	_         uint32
	username  uintptr
	password  uintptr
}

// wsmanAuthMechanism selects the WSMan authentication mechanism from the
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
		code := *(*uint32)(unsafe.Pointer(errPtr))
		if code != 0 {
			result.err = fmt.Errorf("WSMan error %d (0x%08x)", code, code)
		}
	}
	if result.err == nil && result.receive && data != 0 {
		receive := (*wsmanReceiveData)(unsafe.Pointer(data))
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
	if pointer == 0 {
		return ""
	}
	runes := make([]uint16, 0, wsmanStreamReadLimit)
	for i := 0; i < wsmanStreamReadLimit; i++ {
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

// winrmCreateProcess uses the native WSMan shell API with default thread
// credentials. The existing detach command must finish before its shell closes.
func winrmCreateProcess(host, agentPath, sessionID string, workspaceID *string, serviceName string) error {
	native, err := loadWSMan()
	if err != nil {
		return err
	}
	return winrmCreateProcessWithAPI(native, host, agentPath, sessionID, workspaceID, serviceName, wsmanCompletionTimeout)
}

func winrmCreateProcessWithAPI(native wsmanCaller, host, agentPath, sessionID string, workspaceID *string, serviceName string, timeout time.Duration) (resultErr error) {
	arguments, err := lateralDetachArguments(sessionID, workspaceID, serviceName)
	if err != nil {
		return err
	}
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
	connection, err := utf16("http://" + net.JoinHostPort(host, "5985") + "/wsman")
	if err != nil {
		return err
	}
	// The session selects Kerberos for DNS names and Negotiate for IP
	// targets; the null user account preserves the implicit thread identity.
	credentials := wsmanAuthCredentials{mechanism: wsmanAuthMechanism(host)}
	scope.keep(&credentials)
	uri, err := utf16(wsmanShellCmdURI)
	if err != nil {
		return err
	}
	commandLine, err := utf16(agentPath)
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
	var api, session, shell, command, receiveOperation uintptr
	scope.keep(&api)
	scope.keep(&session)
	scope.keep(&shell)
	scope.keep(&command)
	scope.keep(&receiveOperation)
	if code := native.call("WSManInitialize", 0, uintptr(unsafe.Pointer(&api))); code != 0 {
		return fmt.Errorf("WSManInitialize failed: 0x%08x", code)
	}
	defer func() {
		if code := native.call("WSManDeinitialize", api, 0); code != 0 {
			resultErr = errors.Join(resultErr, fmt.Errorf("WSManDeinitialize failed: 0x%08x", code))
		}
	}()
	if code := native.call("WSManCreateSession", api, uintptr(unsafe.Pointer(connection)), 0, uintptr(unsafe.Pointer(&credentials)), 0, uintptr(unsafe.Pointer(&session))); code != 0 {
		return fmt.Errorf("WSManCreateSession(%s) failed: 0x%08x", host, code)
	}
	defer func() {
		if code := native.call("WSManCloseSession", session, 0); code != 0 {
			resultErr = errors.Join(resultErr, fmt.Errorf("WSManCloseSession failed: 0x%08x", code))
		}
	}()
	if credentials.mechanism == wsmanAuthNegotiate {
		// The DWORD union member is at offset 8, shared with binary length.
		// This enables credentials for this session; host-trust policy remains
		// enforced by WinRM. Do not change TrustedHosts or certificate checks.
		option := wsmanData{kind: wsmanDataTypeDWORD, length: 1}
		if code := native.call("WSManSetSessionOption", session,
			wsmanAllowNegotiateImplicitCredentials, uintptr(unsafe.Pointer(&option))); code != 0 {
			return fmt.Errorf("WSManSetSessionOption(implicit Negotiate credentials) failed: 0x%08x", code)
		}
		runtime.KeepAlive(&option)
	}
	shellCall := scope.pendingCall(false)
	native.call("WSManCreateShell", session, 0, uintptr(unsafe.Pointer(uri)), 0, 0, 0,
		uintptr(unsafe.Pointer(&shellCall.async)), uintptr(unsafe.Pointer(&shell)))
	defer func() {
		resultErr = errors.Join(resultErr, scope.closeHandle(native, "WSManCloseShell", shell, timeout))
	}()
	if !shellCall.result.wait(timeout) {
		return errors.New("WinRM shell creation timed out")
	}
	if err, _, _ := shellCall.result.snapshot(); err != nil {
		return fmt.Errorf("WinRM shell on %s: %w", host, err)
	}
	if shell == 0 {
		return errors.New("WSManCreateShell returned no shell handle")
	}
	commandCall := scope.pendingCall(false)
	native.call("WSManRunShellCommand", shell, 0, uintptr(unsafe.Pointer(commandLine)), uintptr(unsafe.Pointer(argumentSet)), 0,
		uintptr(unsafe.Pointer(&commandCall.async)), uintptr(unsafe.Pointer(&command)))
	defer func() {
		resultErr = errors.Join(resultErr, scope.closeHandle(native, "WSManCloseCommand", command, timeout))
	}()
	if !commandCall.result.wait(timeout) {
		return errors.New("WinRM command creation timed out")
	}
	if err, _, _ := commandCall.result.snapshot(); err != nil {
		return fmt.Errorf("WinRM command on %s: %w", host, err)
	}
	if command == 0 {
		return errors.New("WSManRunShellCommand returned no command handle")
	}
	receiveCall := scope.pendingCall(true)
	// NULL requests all output streams. One receive produces multiple callbacks.
	native.call("WSManReceiveShellOutput", shell, command, 0, 0, uintptr(unsafe.Pointer(&receiveCall.async)), uintptr(unsafe.Pointer(&receiveOperation)))
	defer func() {
		if receiveOperation != 0 {
			if code := native.call("WSManCloseOperation", receiveOperation, 0); code != 0 {
				resultErr = errors.Join(resultErr, fmt.Errorf("WSManCloseOperation failed: 0x%08x", code))
			}
		}
	}()
	if !receiveCall.result.wait(2 * timeout) {
		return errors.New("WinRM detach completion not confirmed before timeout")
	}
	if err, done, exitCode := receiveCall.result.snapshot(); err != nil {
		return fmt.Errorf("WinRM receive on %s: %w", host, err)
	} else if !done {
		return errors.New("WinRM receive ended without command completion")
	} else if exitCode != 0 {
		return fmt.Errorf("WinRM detach command exited with %d", exitCode)
	}
	return nil
}

// lateralDetachArguments builds the remote command arguments: the detach
// flag followed by the standard lateral startup arguments.
func lateralDetachArguments(sessionID string, workspaceID *string, serviceName string) ([]string, error) {
	args := []string{"--lateral-detach", "--lateral-session", sessionID}
	if workspaceID != nil {
		value := *workspaceID
		if value == "" {
			value = "-"
		}
		args = append(args, "--workspace-id", value)
	}
	if serviceName != "" {
		args = append(args, "--service-name", serviceName)
	}
	return args, nil
}

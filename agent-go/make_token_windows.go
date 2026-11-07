//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"runtime"
	"sync"
	"syscall"
	"unsafe"
)

// LOGON32_LOGON_NEW_CREDENTIALS clones the caller's token and attaches the
// supplied credentials to outbound network authentication only: callers keep
// the agent's local identity but authenticate on the network as the alternate
// user, exactly like runas /netonly. The token type is 9 (new credentials);
// ImpersonateLoggedOnUser sets it on the calling OS thread so that subsequent
// network operations (SMB, SCM, COM, WSMan) on that thread use those creds
// without requiring SE_ASSIGNPRIMARYTOKEN_PRIVILEGE.
const (
	logonNewCredentials  = 9
	logonProviderWinNT32 = 3
)

var (
	advapi32                    = syscall.NewLazyDLL("advapi32.dll")
	procLogonUserW              = advapi32.NewProc("LogonUserW")
	procImpersonateLoggedOnUser = advapi32.NewProc("ImpersonateLoggedOnUser")
	procRevertToSelf            = advapi32.NewProc("RevertToSelf")

	tokenMu          sync.Mutex
	activeToken      syscall.Token // live operator token; explicit rev2self closes it
	storedIdentity   string        // desired operator identity while enabled
	tokenIdentity    string        // enabled operator identity; callbacks suspend it only on their thread
	tokenActive      bool          // operator token is enabled, not a statement about every OS thread
	spawnCredentials winrmCredentials
)

// makeTokenAction creates a netonly-style token and immediately impersonates
// it on the calling goroutine's OS thread (which must be locked by the
// caller). LogonUser does not validate the credentials against a domain
// controller for this logon type, so success only proves that the token was
// created, not that the password is correct. Stored credentials are never
// logged or echoed; temporary restoration uses the live handle, while explicit
// rev2self permanently revokes and closes it.
func makeTokenAction(domain, user, password string) (string, error) {
	if identityBoundaryFault.Load() {
		return "", errIdentityUnsafe
	}
	userPtr, err := syscall.UTF16PtrFromString(user)
	if err != nil {
		return "", fmt.Errorf("encode user: %w", err)
	}
	domainPtr, err := syscall.UTF16PtrFromString(domain)
	if err != nil {
		return "", fmt.Errorf("encode domain: %w", err)
	}
	passwordPtr, err := syscall.UTF16PtrFromString(password)
	if err != nil {
		return "", fmt.Errorf("encode password: %w", err)
	}
	var handle syscall.Handle
	result, _, callErr := procLogonUserW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		uintptr(logonNewCredentials),
		uintptr(logonProviderWinNT32),
		uintptr(unsafe.Pointer(&handle)),
	)
	if result == 0 {
		return "", fmt.Errorf("LogonUserW failed: %w", callErr)
	}
	identity := user
	if domain != "" {
		identity = domain + `\` + user
	}
	// Lock this goroutine to its OS thread so the thread token survives
	// goroutine rescheduling. The lock is held until rev2self releases it.
	tokenMu.Lock()
	alreadyLocked := tokenActive
	tokenMu.Unlock()
	if !alreadyLocked {
		runtime.LockOSThread()
	}
	if r, _, impErr := procImpersonateLoggedOnUser.Call(uintptr(handle)); r == 0 {
		if !alreadyLocked {
			runtime.UnlockOSThread()
		}
		_ = syscall.CloseHandle(handle)
		return "", fmt.Errorf("ImpersonateLoggedOnUser failed: %w", impErr)
	}
	tokenMu.Lock()
	previousToken := activeToken
	previousIdentity := storedIdentity
	activeToken = syscall.Token(handle)
	tokenIdentity = identity
	storedIdentity = identity
	tokenActive = true
	spawnCredentials.domain, spawnCredentials.user, spawnCredentials.password = domain, user, password
	tokenMu.Unlock()
	if previousToken != 0 {
		_ = previousToken.Close()
		return fmt.Sprintf(
			"token replaced: %s (was: %s); network operations on this thread use this identity",
			identity, previousIdentity,
		), nil
	}
	return fmt.Sprintf(
		"token created: %s; network operations on this thread use this identity",
		identity,
	), nil
}

// rev2selfAction permanently revokes the operator token. Temporary callback
// suspension instead captures and restores the actual thread token separately.
func rev2selfAction() (string, error) {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	token, identity, wasActive := activeToken, tokenIdentity, tokenActive
	if token == 0 {
		return "no token active", nil
	}
	if wasActive {
		if ok, _, err := procRevertToSelf.Call(); ok == 0 {
			return "", identityBoundaryFailure(fmt.Errorf("revoke thread identity: %w", err))
		}
	}
	activeToken, tokenIdentity, storedIdentity, tokenActive = 0, "", "", false
	spawnCredentials.domain, spawnCredentials.user, spawnCredentials.password = "", "", ""
	_ = token.Close()
	if wasActive {
		runtime.UnlockOSThread()
	}
	return fmt.Sprintf("token reverted: %s; spawned commands use the agent identity", identity), nil
}

// suspendServerIdentity captures the actual calling thread token rather than
// global make_token state. Keep this goroutine on the clean thread until all
// request work, including body reads and close, has finished.
func suspendServerIdentity() (func() error, error) {
	runtime.LockOSThread()
	var saved syscall.Token
	open := advapi32.NewProc("OpenThreadToken")
	// GetCurrentThread's pseudo handle is -2. OpenAsSelf checks access using
	// the process identity; the returned impersonation token needs query and
	// impersonate access for restoration through ImpersonateLoggedOnUser.
	ok, _, openErr := open.Call(^uintptr(1), 0x0008|0x0004, 1, uintptr(unsafe.Pointer(&saved)))
	if ok == 0 && openErr != syscall.Errno(1008) { // ERROR_NO_TOKEN is expected without impersonation.
		// Keep an uncertain thread pinned until this goroutine terminates.
		return nil, fmt.Errorf("capture callback thread token: %w", openErr)
	}
	if saved == 0 {
		return func() error { runtime.UnlockOSThread(); return nil }, nil
	}
	if reverted, _, revertErr := procRevertToSelf.Call(); reverted == 0 {
		_ = saved.Close()
		return nil, fmt.Errorf("revert callback thread token: %w", revertErr)
	}
	return func() error {
		defer runtime.UnlockOSThread()
		defer saved.Close()
		if restored, _, restoreErr := procImpersonateLoggedOnUser.Call(uintptr(saved)); restored == 0 {
			return fmt.Errorf("restore callback thread token: %w", restoreErr)
		}
		return nil
	}, nil
}

// reestablishThreadToken reapplies an enabled token on the calling OS thread.
// The main command goroutine is pinned by make_token; workers must pin and clean
// their own threads. Explicit rev2self leaves no token to reactivate.
func reestablishThreadToken() {
	if identityBoundaryFault.Load() {
		return
	}
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if !tokenActive || activeToken == 0 {
		return
	}
	if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(activeToken)); ok == 0 {
		identityBoundaryFailure(fmt.Errorf("restore operator thread token: %w", err))
	}
}

// applyCommandToken gives an operator command child the active network
// identity via SysProcAttr.Token (CreateProcessAsUserW / CreateProcessWithTokenW).
// Lateral execution uses pinned worker impersonation instead. After a call
// that uses SysProcAttr.Token, call
// reestablishThreadToken to restore the creating goroutine's thread token.
func applyCommandToken(command *exec.Cmd) {
	if identityBoundaryFault.Load() {
		return
	}
	if command.SysProcAttr == nil {
		return
	}
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if tokenActive && activeToken != 0 {
		command.SysProcAttr.Token = activeToken
	}
}

// processUserIdentity resolves the account running this agent process,
// falling back to the environment when the token lookup fails.
func processUserIdentity() string {
	if token, err := syscall.OpenCurrentProcessToken(); err == nil {
		defer token.Close()
		if user, err := token.GetTokenUser(); err == nil {
			if account, domain, _, err := user.User.Sid.LookupAccount(""); err == nil && account != "" {
				if domain != "" {
					return domain + `\` + account
				}
				return account
			}
		}
	}
	return username()
}

// activeNetworkIdentity returns the make_token identity, if one is active.
func activeNetworkIdentity() string {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	return tokenIdentity
}

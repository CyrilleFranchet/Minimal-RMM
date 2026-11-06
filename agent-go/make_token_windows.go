//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"sync"
	"syscall"
	"unsafe"
)

// LOGON32_LOGON_NEW_CREDENTIALS clones the caller's token and attaches the
// supplied credentials to outbound network authentication only: the spawned
// commands keep the agent's local identity but authenticate on the network
// as the alternate user, exactly like runas /netonly.
const (
	logonNewCredentials  = 9
	logonProviderWinNT32 = 3
)

var (
	advapi32       = syscall.NewLazyDLL("advapi32.dll")
	procLogonUserW = advapi32.NewProc("LogonUserW")

	tokenMu       sync.Mutex
	activeToken   syscall.Token
	tokenIdentity string
)

// makeTokenAction creates a netonly-style token and stores it for subsequent
// operator command children. LogonUser does not validate the credentials
// against a domain controller for this logon type, so success only proves
// that the token was created, not that the password is correct.
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
	tokenMu.Lock()
	previousIdentity := tokenIdentity
	previousToken := activeToken
	activeToken = syscall.Token(handle)
	tokenIdentity = identity
	tokenMu.Unlock()
	if previousToken != 0 {
		_ = previousToken.Close()
		return fmt.Sprintf(
			"token replaced: %s (was: %s); spawned commands now use this network identity",
			identity, previousIdentity,
		), nil
	}
	return fmt.Sprintf(
		"token created: %s; spawned commands now use this network identity",
		identity,
	), nil
}

// rev2selfAction closes the active token so spawned commands return to the
// agent's own identity. It is idempotent: reverting without an active token
// is a success.
func rev2selfAction() (string, error) {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if activeToken == 0 {
		return "no token active", nil
	}
	identity := tokenIdentity
	token := activeToken
	activeToken = 0
	tokenIdentity = ""
	_ = token.Close()
	return fmt.Sprintf(
		"token reverted: %s; spawned commands use the agent identity",
		identity,
	), nil
}

// applyCommandToken gives an operator command child the active network
// identity. Go creates such children with CreateProcessAsUser; the cloned
// token preserves the local identity and swaps only network credentials.
func applyCommandToken(command *exec.Cmd) {
	if command.SysProcAttr == nil {
		return
	}
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if activeToken != 0 {
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

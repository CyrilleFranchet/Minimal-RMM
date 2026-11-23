//go:build !windows

package main

import (
	"errors"
)

func spawnasCommand(_ string, _ *config) (string, error) {
	return "", errors.New("spawnas requires the Windows agent build")
}

// spawnWithLogonDetached is the non-Windows stub behind the staged spawnas
// flow; the dispatch refuses non-Windows builds before reaching it.
func spawnWithLogonDetached(_, _, _, _ string, _ []string, _ []string) (uint32, error) {
	return 0, errors.New("spawnas requires the Windows agent build")
}

func runasCommand(_ string) (string, error) {
	return "", errors.New("runas requires the Windows agent build")
}

//go:build !windows

package main

import (
	"errors"
)

func spawnasCommand(_ string, _ *config) (string, error) {
	return "", errors.New("spawnas requires the Windows agent build")
}

func runasCommand(_ string) (string, error) {
	return "", errors.New("runas requires the Windows agent build")
}

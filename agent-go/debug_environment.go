package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// resolveDebugAgentExecutable selects the separately built debug companion without fallback.
func resolveDebugAgentExecutable(current string, debug bool) (string, error) {
	if !debug {
		return current, nil
	}
	path := filepath.Join(filepath.Dir(current), "minimal-rmm-agent-debug-windows-"+runtime.GOARCH+".exe")
	info, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("debug agent companion unavailable: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", errors.New("debug agent companion must be a regular executable file")
	}
	file, err := os.Open(path)
	if err != nil {
		return "", fmt.Errorf("open debug agent companion: %w", err)
	}
	defer file.Close()
	var magic [2]byte
	if _, err := io.ReadFull(file, magic[:]); err != nil || magic != [2]byte{'M', 'Z'} {
		return "", errors.New("debug agent companion is not a Windows executable")
	}
	return path, nil
}

// spawnasDebugEnvironment makes diagnostic selection independent of the parent.
func spawnasDebugEnvironment(env []string, enabled bool) []string {
	result := make([]string, 0, len(env)+1)
	for _, entry := range env {
		key, _, _ := strings.Cut(entry, "=")
		if !strings.EqualFold(key, "RMM_DEBUG") {
			result = append(result, entry)
		}
	}
	value := "0"
	if enabled {
		value = "1"
	}
	return append(result, "RMM_DEBUG="+value)
}

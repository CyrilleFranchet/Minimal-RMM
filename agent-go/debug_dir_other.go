//go:build rmmdebug && !windows

package main

import "os"

// debugDirectory selects the current process's temporary directory.
func debugDirectory() string { return os.TempDir() }

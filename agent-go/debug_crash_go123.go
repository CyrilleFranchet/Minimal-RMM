//go:build rmmdebug && go1.23

package main

import (
	"os"
	"runtime/debug"
)

const debugCrashOutputSupported = true

// installDebugCrashOutput duplicates the file for Go fatal errors and unhandled panics.
func installDebugCrashOutput(file *os.File) error {
	return debug.SetCrashOutput(file, debug.CrashOptions{})
}

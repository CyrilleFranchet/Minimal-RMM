//go:build rmmdebug && !go1.23

package main

import (
	"os"
)

const debugCrashOutputSupported = false

// installDebugCrashOutput preserves older builds while reporting unavailable crash capture.
func installDebugCrashOutput(file *os.File) error {
	if file == nil {
		return nil
	}
	return errDebugCrashUnsupported
}

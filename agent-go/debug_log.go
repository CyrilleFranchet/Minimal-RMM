//go:build rmmdebug

package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

const agentDebugBuild = true

var debugLog = struct {
	sync.Mutex
	file *os.File
}{}

var errDebugCrashUnsupported = errors.New("runtime crash capture requires Go 1.23 or newer")

// initAgentDebug enables diagnostics for every process compiled in debug mode.
func initAgentDebug(announce bool) {
	path := filepath.Join(debugDirectory(), fmt.Sprintf("minimal-rmm-debug-%d.log", os.Getpid()))
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		if announce {
			logf("debug file initialization failed")
		}
		return
	}
	debugLog.Lock()
	debugLog.file = file
	debugLog.Unlock()
	if announce {
		logf("debug diagnostics: %s", path)
	}
	debugEvent("process.start", nil)
	if err := installDebugCrashOutput(file); err != nil {
		debugEvent("crash_capture.unavailable", err)
	} else {
		debugEvent("crash_capture.enabled", nil)
	}
}

// closeAgentDebug releases the event writer; the runtime retains its duplicated crash handle.
func closeAgentDebug() {
	debugEvent("diagnostics.close", nil)
	debugLog.Lock()
	defer debugLog.Unlock()
	if debugLog.file != nil {
		_ = debugLog.file.Close()
		debugLog.file = nil
	}
}

// debugEvent records fixed checkpoints and error classes, never arbitrary error text.
// Callers must use static event names; payloads, paths and identity labels are excluded.
func debugEvent(event string, err error) {
	debugLog.Lock()
	defer debugLog.Unlock()
	if debugLog.file == nil {
		return
	}
	result := "ok"
	if err != nil {
		var code syscall.Errno
		switch {
		case errors.As(err, &code):
			result = fmt.Sprintf("win32:%d", code)
		case errors.Is(err, context.Canceled):
			result = "canceled"
		case errors.Is(err, context.DeadlineExceeded):
			result = "deadline"
		case errors.Is(err, errDebugCrashUnsupported):
			result = "requires_go_1.23"
		default:
			result = "error"
		}
	}
	_, _ = fmt.Fprintf(debugLog.file, "%s pid=%d event=%s result=%s\n",
		time.Now().UTC().Format(time.RFC3339Nano), os.Getpid(), event, result)
	_ = debugLog.file.Sync()
}

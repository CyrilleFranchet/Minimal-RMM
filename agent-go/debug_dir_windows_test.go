//go:build rmmdebug && windows

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDebugDirectoryAvoidsInheritedParentTemp(t *testing.T) {
	parentTemp := t.TempDir()
	t.Setenv("TEMP", parentTemp)
	t.Setenv("TMP", parentTemp)
	expected := filepath.Join(os.Getenv("SystemRoot"), "Temp")
	if directory := debugDirectory(); !strings.EqualFold(filepath.Clean(directory), filepath.Clean(expected)) {
		t.Fatalf("debug directory = %q, want Windows Temp %q", directory, expected)
	}
}

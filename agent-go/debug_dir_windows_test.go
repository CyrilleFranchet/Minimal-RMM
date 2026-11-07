//go:build rmmdebug && windows

package main

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestDebugDirectoryAvoidsInheritedParentTemp(t *testing.T) {
	parentTemp := t.TempDir()
	t.Setenv("TEMP", parentTemp)
	t.Setenv("TMP", parentTemp)
	if directory := debugDirectory(); strings.EqualFold(filepath.Clean(directory), filepath.Clean(parentTemp)) {
		t.Fatal("debug diagnostics fell back to inherited parent TEMP instead of process profile")
	}
}

//go:build windows

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWindowsCMDQuotedCommands(t *testing.T) {
	script := filepath.Join(t.TempDir(), "quoted command.cmd")
	if err := os.WriteFile(script, []byte("@echo off\r\necho [%~1]\r\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tt := range []struct {
		name, command, want string
	}{
		{"quoted argument", `echo "two words"`, `"two words"`},
		{"single-quoted argument", `echo 'two words'`, `"two words"`},
		{"single-quoted program and argument", `'` + script + `' 'two words'`, `[two words]`},
		{"quoted program and argument", `"` + script + `" "two words"`, `[two words]`},
		{"operators", `echo "two & words" & echo done`, "\"two & words\"\r\ndone"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			name, args, err := buildCommandInvocation(tt.command, "windows", true)
			if err != nil {
				t.Fatal(err)
			}
			child := newCommand(name, args...)
			out, err := child.CombinedOutput()
			// CMD echo can retain whitespace immediately before an operator.
			lines := strings.Split(strings.TrimSpace(string(out)), "\r\n")
			for i := range lines {
				lines[i] = strings.TrimSpace(lines[i])
			}
			if got := strings.Join(lines, "\r\n"); err != nil || got != tt.want {
				t.Fatalf("output=%q err=%v; want %q", out, err, tt.want)
			}
		})
	}
}

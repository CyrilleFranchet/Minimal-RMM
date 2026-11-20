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

// An active make_token identity must never block command execution: without
// the process-creation privilege the child still runs, under the agent
// identity, and the output mentions the degraded state.
func TestRunCommandIdentityDegradesWithoutCreationPrivilege(t *testing.T) {
	t.Cleanup(func() {
		commandTokenPrivilegeHeld = processCommandTokenPrivilegeHeld
		identityBoundaryFault.Store(false)
	})
	if _, err := makeTokenAction(".", "callback-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	// The pinned decision models an account without process-creation
	// privileges: plain CreateProcess must still run the command.
	commandTokenPrivilegeHeld = func() bool { return false }
	output, resultType := runCommand(`echo privilege-fixture`)
	if resultType != "output" {
		t.Fatalf("unexpected result type: %q", resultType)
	}
	if !strings.Contains(output, "privilege-fixture") {
		t.Fatalf("command did not run through plain CreateProcess: %q", output)
	}
	if !strings.Contains(output, "SeAssignPrimaryTokenPrivilege") || !strings.Contains(output, "runas") {
		t.Fatalf("degraded command output lacks the identity notice: %q", output)
	}
	// The token assignment follows the held decision.
	privilege := true
	commandTokenPrivilegeHeld = func() bool { return privilege }
	child := newCommand("cmd.exe", "/c", "exit", "0")
	if !applyCommandToken(child) || child.SysProcAttr.Token == 0 {
		t.Fatal("held privilege did not apply the active token")
	}
	privilege = false
	child = newCommand("cmd.exe", "/c", "exit", "0")
	if applyCommandToken(child) || child.SysProcAttr.Token != 0 {
		t.Fatal("missing privilege still applied the active token")
	}
	// Without an active token there is nothing to degrade: no notice.
	if _, err := rev2selfAction(); err != nil {
		t.Fatal(err)
	}
	commandTokenPrivilegeHeld = func() bool { return false }
	output, _ = runCommand(`echo no-token-fixture`)
	if strings.Contains(output, "SeAssignPrimaryTokenPrivilege") {
		t.Fatalf("degraded notice without an active make_token: %q", output)
	}
	if !strings.Contains(output, "no-token-fixture") {
		t.Fatalf("command without an active token did not run: %q", output)
	}
}

package main

import (
	"strings"
	"testing"
)

// TestParseWmiProbeCommand checks the probe command line: a single target
// host, written like the lateral deployment targets, the optional mode
// word, and the optional second-account JSON body for the switch mode —
// with explicit errors for missing or malformed hosts, unknown modes, and
// misplaced or invalid bodies. Credentials are validated but never appear
// in error text.
func TestParseWmiProbeCommand(t *testing.T) {
	for _, line := range []string{
		"__WMI_PROBE__ 10.254.100.32",
		"  __WMI_PROBE__   ws01.lab.local  ",
		"__WMI_PROBE__ fd00::8",
		"__WMI_PROBE__ 10.254.100.32 HELD",
		"__WMI_PROBE__ 10.254.100.32 cycled",
		"__WMI_PROBE__ 10.254.100.32 switch",
	} {
		host, mode, identity, err := parseWmiProbeCommand(line)
		if err != nil {
			t.Fatalf("%q: %v", line, err)
		}
		if host == "" || identity != nil {
			t.Fatalf("%q: host=%q identity=%v", line, host, identity)
		}
		want := ""
		for _, word := range []string{"held", "cycled", "switch"} {
			if strings.Contains(strings.ToLower(line), word) {
				want = word
			}
		}
		if mode != want {
			t.Fatalf("%q: mode=%q, want %q", line, mode, want)
		}
	}
	for _, line := range []string{
		"__WMI_PROBE__",
		"__WMI_PROBE__  ",
		"__WMI_PROBE__ 10.254.100.32 lab.local",
		`__WMI_PROBE__ \\target`,
		`__WMI_PROBE__ target\pipe\x`,
		"__WMI_PROBE__ 10.254.100.32 continuous",
		"__WMI_PROBE__ 10.254.100.32 held extra",
	} {
		if _, _, _, err := parseWmiProbeCommand(line); err == nil {
			t.Fatalf("%q: expected an error", line)
		}
	}
	// The prefix must not be trimmed away from a longer command family.
	if _, _, _, err := parseWmiProbeCommand("__MAKE_TOKEN__\n{}"); err == nil {
		t.Fatal("a make_token payload must not parse as a probe")
	}
}

// TestParseWmiProbeCommandSwitchBody covers the switch mode's optional
// second-account JSON body: credentials accepted without echoing, the
// DOMAIN\user split, and rejections for missing users, malformed JSON, and
// bodies sent to other modes.
func TestParseWmiProbeCommandSwitchBody(t *testing.T) {
	host, mode, identity, err := parseWmiProbeCommand(
		"__WMI_PROBE__ 10.254.100.32 switch\n" + `{"domain":"LAB","user":"svc-b","password":"s3cret"}`)
	if err != nil {
		t.Fatal(err)
	}
	if host != "10.254.100.32" || mode != "switch" {
		t.Fatalf("host=%q mode=%q", host, mode)
	}
	if identity == nil || identity.Domain != "LAB" || identity.User != "svc-b" || identity.Password != "s3cret" {
		t.Fatalf("identity=%+v", identity)
	}

	// A combined DOMAIN\user value splits like make_token.
	_, _, identity, err = parseWmiProbeCommand(
		"__WMI_PROBE__ ws01 switch\n" + `{"user":"LAB\\svc-b","password":"pw"}`)
	if err != nil || identity == nil || identity.Domain != "LAB" || identity.User != "svc-b" {
		t.Fatalf("split identity: err=%v identity=%+v", err, identity)
	}

	// An explicit domain plus an embedded backslash is ambiguous.
	if _, _, _, err := parseWmiProbeCommand(
		"__WMI_PROBE__ ws01 switch\n" + `{"domain":"LAB","user":"LAB\\svc-b","password":"pw"}`); err == nil {
		t.Fatal("a body with both an explicit domain and an embedded backslash must fail")
	}

	for _, line := range []string{
		"__WMI_PROBE__ ws01 switch\nnot-json",
		"__WMI_PROBE__ ws01 switch\n{}",
		"__WMI_PROBE__ ws01 switch\n" + `{"user":"","password":"pw"}`,
		"__WMI_PROBE__ ws01 switch\n" + `{"domain":"LAB","user":"\\","password":"pw"}`,
		"__WMI_PROBE__ ws01 held\n" + `{"user":"svc-b","password":"pw"}`,
		"__WMI_PROBE__ ws01\n" + `{"user":"svc-b","password":"pw"}`,
	} {
		if _, _, _, err := parseWmiProbeCommand(line); err == nil {
			t.Fatalf("%q: expected an error", line)
		}
	}

	// Credentials never leak into error text.
	_, _, _, err = parseWmiProbeCommand("__WMI_PROBE__ ws01 switch\n" + `{"user":"","password":"s3cret"}`)
	if err != nil && strings.Contains(err.Error(), "s3cret") {
		t.Fatalf("body error leaks the password: %v", err)
	}
}

package main

import (
	"strings"
	"testing"
)

// TestParseWmiProbeCommand checks the probe command line: a single target
// host, written like the lateral deployment targets, with explicit errors
// for missing or malformed hosts and for unknown modes. Credentials never
// appear on this command.
func TestParseWmiProbeCommand(t *testing.T) {
	for _, line := range []string{
		"__WMI_PROBE__ 10.254.100.32",
		"  __WMI_PROBE__   ws01.lab.local  ",
		"__WMI_PROBE__ fd00::8",
		"__WMI_PROBE__ 10.254.100.32 HELD",
	} {
		host, mode, err := parseWmiProbeCommand(line)
		if err != nil {
			t.Fatalf("%q: %v", line, err)
		}
		if host == "" {
			t.Fatalf("%q: empty host", line)
		}
		if strings.EqualFold(mode, "held") != strings.Contains(strings.ToLower(line), "held") {
			t.Fatalf("%q: mode=%q", line, mode)
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
		if _, _, err := parseWmiProbeCommand(line); err == nil {
			t.Fatalf("%q: expected an error", line)
		}
	}
	// The prefix must not be trimmed away from a longer command family.
	if _, _, err := parseWmiProbeCommand("__MAKE_TOKEN__\n{}"); err == nil {
		t.Fatal("a make_token payload must not parse as a probe")
	}
}

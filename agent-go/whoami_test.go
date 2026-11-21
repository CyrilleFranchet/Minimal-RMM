package main

import (
	"runtime"
	"strings"
	"testing"
)

func TestWhoamiPrivilegeStates(t *testing.T) {
	for _, tc := range []struct {
		a       uint32
		present bool
		want    string
	}{
		{0, false, "absent"}, {0, true, "disabled"}, {2, true, "enabled"}, {4, true, "removed"}, {6, true, "removed"}, {0x80000003, true, "enabled (default=true, used-for-access=true"},
	} {
		got := whoamiPrivilegeState(tc.a, tc.present)
		if !strings.HasPrefix(got, tc.want) {
			t.Errorf("attributes %x present=%t: got %q want prefix %q", tc.a, tc.present, got, tc.want)
		}
	}
	seen := map[string]bool{}
	for _, name := range whoamiPrivilegeCatalog {
		if seen[name] {
			t.Errorf("duplicate privilege %s", name)
		}
		seen[name] = true
	}
	if len(seen) < 35 || !seen["SeDelegateSessionUserImpersonatePrivilege"] || !seen["SeDebugPrivilege"] {
		t.Fatal("incomplete Windows privilege catalog")
	}
}

func TestWhoamiDispatch(t *testing.T) {
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "go-test"}
	for _, command := range []string{"__WHOAMI__", "__WHOAMI__ --tickets"} {
		output, kind := execute(command, cfg, nil)
		if kind != "output" {
			t.Fatalf("%q returned kind %q", command, kind)
		}
		if runtime.GOOS == "windows" {
			if !strings.Contains(output, "Process token") {
				t.Fatalf("collector not dispatched: %q", output)
			}
		} else if !strings.Contains(output, "requires a Windows Go agent") {
			t.Fatalf("unexpected platform response: %q", output)
		}
	}
	for _, command := range []string{"__WHOAMI__ --invalid", "__WHOAMI__ --tickets extra", "__WHOAMI__ --tickets --tickets"} {
		output, kind := execute(command, cfg, nil)
		if kind != "output" || !strings.Contains(output, "usage: __WHOAMI__ [--tickets]") {
			t.Fatalf("invalid command %q: %s / %s", command, output, kind)
		}
	}
}

func TestWhoamiScalarLengths(t *testing.T) {
	for _, tc := range []struct {
		class uint32
		data  []byte
		want  uint32
		bad   bool
	}{
		{21, []byte{1}, 1, false}, {21, []byte{0}, 0, false}, {21, []byte{1, 0xff, 0xff, 0xff}, 1, false},
		{21, nil, 0, true}, {20, []byte{1}, 0, true}, {20, []byte{1, 2, 3}, 0, true},
		{20, []byte{1, 2, 3, 4}, 0x04030201, false}, {8, []byte{2, 0, 0, 0}, 2, false},
	} {
		got, err := whoamiScalar(tc.class, tc.data)
		if (err != nil) != tc.bad || (!tc.bad && got != tc.want) {
			t.Errorf("class=%d data=%v got=%d err=%v", tc.class, tc.data, got, err)
		}
	}
}

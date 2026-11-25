package main

import (
	"strings"
	"testing"
)

func TestGetuidxLabelWithoutIdentitySplit(t *testing.T) {
	for _, networkIdentity := range []string{"", `LAB\operator`} {
		label := getuidxLabel(`LAB\agent`, networkIdentity, false)
		if !strings.Contains(label, `process identity: LAB\agent`) {
			t.Fatalf("missing process identity: %q", label)
		}
		if !strings.Contains(label, "network identity: none (commands use agent identity)") {
			t.Fatalf("inactive split must not claim an alternate network identity: %q", label)
		}
		if !strings.Contains(label, "identity split: false") {
			t.Fatalf("inactive split must be explicit: %q", label)
		}
	}
}

func TestGetuidxLabelWithIdentitySplit(t *testing.T) {
	label := getuidxLabel(`LAB\agent`, `LAB\operator`, true)
	for _, want := range []string{
		`process identity: LAB\agent`,
		`network identity: LAB\operator`,
		`identity split: true`,
		`local commands run as LAB\agent`,
		`network traffic authenticates as LAB\operator`,
	} {
		if !strings.Contains(label, want) {
			t.Fatalf("getuidx label missing %q: %q", want, label)
		}
	}
}

func TestGetuidxLabelRequiresNetworkIdentityForSplit(t *testing.T) {
	label := getuidxLabel(`LAB\agent`, "", true)
	if strings.Contains(label, "identity split: true") || !strings.Contains(label, "identity split: false") {
		t.Fatalf("empty network identity cannot form a split: %q", label)
	}
}

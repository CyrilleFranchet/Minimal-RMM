//go:build windows

package main

import (
	"strings"
	"testing"
	"time"
)

func TestWSManHTTPSTransport(t *testing.T) {
	for _, tc := range []struct {
		host, url string
		secure    bool
	}{
		{"10.0.0.8", "https://10.0.0.8:5986/wsman", true},
		{"fd00::8", "https://[fd00::8]:5986/wsman", true},
		{"lab-host", "https://lab-host:5986/wsman", true},
		{"10.0.0.8", "http://10.0.0.8:5985/wsman", false},
	} {
		for _, explicit := range []bool{false, true} {
			f := &wsmanFixture{t: t, wantURL: tc.url}
			var identity *winrmCredentials
			if explicit {
				identity = &winrmCredentials{domain: "LAB", user: "fixture", password: "fixture"}
				f.wantIdentity = identity
			}
			if tc.host != "lab-host" {
				f.wantAuth = 4
			}
			workspace := "fixture-workspace"
			if err := winrmCreateProcessWithTransportAPI(f, tc.host, `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, identity, tc.secure); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// A native TLS/authentication failure must not retry using HTTP or disable
// certificate checks. The fixture rejects all unexpected session options.
func TestWSManHTTPSFailureDoesNotDowngrade(t *testing.T) {
	f := &wsmanFixture{t: t, wantURL: "https://10.0.0.8:5986/wsman", fail: "WSManCreateShell"}
	workspace := "fixture-workspace"
	err := winrmCreateProcessWithTransportAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, nil, true)
	if err == nil || !strings.Contains(err.Error(), "WSMan error") {
		t.Fatalf("expected native failure, got %v", err)
	}
	sessions := 0
	for _, call := range f.calls {
		if call == "WSManCreateSession" {
			sessions++
		}
	}
	if sessions != 1 {
		t.Fatalf("created %d sessions after failure", sessions)
	}
}

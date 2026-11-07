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
			f := &wsmanFixture{t: t, wantURL: tc.url, wantImplicitNegotiate: tc.secure && !explicit}
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
	f := &wsmanFixture{t: t, wantURL: "https://10.0.0.8:5986/wsman", fail: "WSManCreateShell", wantImplicitNegotiate: true}
	workspace := "fixture-workspace"
	err := winrmCreateProcessWithTransportAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, nil, true)
	if err == nil || !strings.Contains(err.Error(), "WSMan error") {
		t.Fatalf("expected native failure, got %v", err)
	}
	if !strings.Contains(err.Error(), "https; auth=implicit-negotiate") {
		t.Fatalf("missing configured transport/authentication context: %v", err)
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

// TestWSManHTTPSFailureMatrix covers the implicit/explicit credential branches
// through native failures, including teardown and credential-buffer cleanup.
func TestWSManHTTPSFailureMatrix(t *testing.T) {
	stages := []string{"WSManInitialize", "WSManCreateSession", "WSManSetSessionOption", "WSManCreateShell", "WSManRunShellCommand", "WSManReceiveShellOutput", "WSManCloseOperation", "WSManCloseCommand", "WSManCloseShell", "WSManCloseSession", "WSManDeinitialize"}
	for _, explicit := range []bool{false, true} {
		for _, stage := range stages {
			if explicit && stage == "WSManSetSessionOption" {
				continue
			}
			name := "implicit/" + stage
			if explicit {
				name = "explicit/" + stage
			}
			t.Run(name, func(t *testing.T) {
				f := &wsmanFixture{t: t, wantURL: "https://10.0.0.8:5986/wsman", wantAuth: 4, fail: stage, wantImplicitNegotiate: !explicit}
				var identity *winrmCredentials
				if explicit {
					identity = &winrmCredentials{domain: "LAB", user: "fixture", password: "never-echo-fixture-secret"}
					f.wantIdentity = identity
				}
				workspace := "fixture-workspace"
				err := winrmCreateProcessWithTransportAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, identity, true)
				if err == nil {
					t.Fatal("native failure reported success")
				}
				if strings.Contains(err.Error(), "never-echo-fixture-secret") {
					t.Fatal("failure exposed selected password")
				}
				sessions := 0
				for _, call := range f.calls {
					if call == "WSManCreateSession" {
						sessions++
					}
				}
				if sessions > 1 {
					t.Fatal("failure retried session creation")
				}
				for _, context := range f.contexts {
					if _, exists := wsmanContexts.Load(context); exists {
						t.Fatal("failure leaked callback context")
					}
				}
				for _, value := range f.passwordBuffer {
					if value != 0 {
						t.Fatal("failure retained native password buffer")
					}
				}
			})
		}
	}
}

// Bypass must set all three native checks only for the requested HTTPS
// session, and stop before shell creation if any option cannot be applied.
func TestWSManCertificateBypass(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		for _, failure := range []uintptr{0, 18, 19, 31} {
			f := &wsmanFixture{t: t, wantURL: "https://10.0.0.8:5986/wsman", wantAuth: 4, wantCertificateBypass: true, wantImplicitNegotiate: !explicit, failOption: failure}
			if failure != 0 {
				f.fail = "WSManSetSessionOption"
			}
			var identity *winrmCredentials
			if explicit {
				identity = &winrmCredentials{user: "fixture", password: "fixture"}
				f.wantIdentity = identity
			}
			workspace := "fixture-workspace"
			err := winrmCreateProcessWithTLSAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, identity, true, true)
			if (err != nil) != (failure != 0) {
				t.Fatalf("bypass option %d: %v", failure, err)
			}
			if failure != 0 {
				for _, call := range f.calls {
					if call == "WSManCreateShell" {
						t.Fatal("shell opened after bypass option failed")
					}
				}
			}
		}
	}
	f := &wsmanFixture{t: t}
	workspace := "fixture-workspace"
	if err := winrmCreateProcessWithTLSAPI(f, "target", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, nil, false, true); err == nil || len(f.calls) != 0 {
		t.Fatal("HTTP bypass must fail before native calls")
	}
}

// Skip-CA/name/revocation is not a blanket TLS-validation override. Native
// secure-channel failures, including certificate expiry, must still surface.
func TestWSManPartialCertificateBypassPreservesTLSFailure(t *testing.T) {
	identity := &winrmCredentials{user: "fixture", password: "fixture"}
	f := &wsmanFixture{t: t, wantURL: "https://10.0.0.8:5986/wsman", wantAuth: 4,
		wantIdentity: identity, wantCertificateBypass: true, fail: "WSManCreateShell", faultCode: 12175}
	workspace := "fixture-workspace"
	err := winrmCreateProcessWithTLSAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second, identity, true, true)
	if err == nil || !strings.Contains(err.Error(), "WSMan error 12175") || !strings.Contains(err.Error(), "cert=skip-ca-cn-revocation") {
		t.Fatalf("partial certificate bypass obscured native TLS failure: %v", err)
	}
}

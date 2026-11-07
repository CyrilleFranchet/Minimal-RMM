package main

import (
	"encoding/base64"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
)

type workspaceBindingTransport func(*http.Request) (*http.Response, error)

func (transport workspaceBindingTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return transport(request)
}

func TestWorkspaceBindingConfigUsesEmbeddedAndRuntimeValues(t *testing.T) {
	original := defaultWorkspaceIDB64
	defer func() { defaultWorkspaceIDB64 = original }()
	defaultWorkspaceIDB64 = base64.StdEncoding.EncodeToString([]byte("embedded-workspace"))
	t.Setenv("RMM_BASE_URL", "https://beacon.example.test")
	t.Setenv("RMM_BEACON_SECRET", "fixture-secret")
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "")
	if err := os.Unsetenv("RMM_AGENT_WORKSPACE_ID"); err != nil {
		t.Fatal(err)
	}
	cfg, err := loadConfig()
	if err != nil || cfg.WorkspaceID != "embedded-workspace" {
		t.Fatalf("embedded workspace was not loaded: %#v / %v", cfg, err)
	}
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "runtime-workspace")
	cfg, err = loadConfig()
	if err != nil || cfg.WorkspaceID != "runtime-workspace" {
		t.Fatalf("runtime workspace override was not honored: %#v / %v", cfg, err)
	}
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "")
	cfg, err = loadConfig()
	if err != nil || cfg.WorkspaceID != "" {
		t.Fatalf("explicit empty binding did not clear embedded workspace: %#v / %v", cfg, err)
	}
	t.Setenv("RMM_AGENT_WORKSPACE_ID", "../invalid")
	if _, err := loadConfig(); err == nil {
		t.Fatal("invalid workspace must fail before registration")
	}
}

func TestRegistrationIncludesWorkspaceRoutingID(t *testing.T) {
	for _, workspaceID := range []string{"", "team.lab-1"} {
		t.Run(workspaceID, func(t *testing.T) {
			cfg := &config{BaseURL: "https://beacon.example.test", BeaconSecret: "fixture-secret", SessionID: "go-test-session", WorkspaceID: workspaceID}
			called := false
			client := &http.Client{Transport: workspaceBindingTransport(func(request *http.Request) (*http.Response, error) {
				called = true
				if request.URL.Path != "/register" || request.URL.Query().Get("workspace_id") != workspaceID {
					t.Fatalf("unexpected registration route: %s", request.URL)
				}
				if request.URL.Query().Get("u") != processUserIdentity() {
					t.Fatal("registration did not report the process identity used by uid")
				}
				if _, found := request.URL.Query()["workspace_id"]; found != (workspaceID != "") {
					t.Fatalf("empty owner should not create a workspace claim: %s", request.URL)
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("REGISTERED")), Header: make(http.Header)}, nil
			})}
			if err := register(client, cfg); err != nil || !called {
				t.Fatalf("registration failed: called=%v err=%v", called, err)
			}
		})
	}
}

func TestLateralStartupOnlyAcceptsKnownIdentityFlags(t *testing.T) {
	sessionID, workspaceID, err := lateralStartupConfig([]string{"--lateral-session", "new-session-id", "--workspace-id", "team.lab-1"})
	if err != nil || sessionID != "new-session-id" || workspaceID != "team.lab-1" {
		t.Fatalf("target identity arguments were not preserved: %q %q %v", sessionID, workspaceID, err)
	}
	for _, args := range [][]string{
		{"--lateral-session"},
		{"--lateral-session", "bad"},
		{"--lateral-session", "new-session-id", "--unknown", "team"},
		{"--lateral-session", "new-session-id", "--workspace-id", "../team"},
		{"--lateral-session", "new-session-id", "--workspace-id", "team", "extra"},
	} {
		if _, _, err := lateralStartupConfig(args); err == nil {
			t.Fatalf("invalid startup arguments accepted: %#v", args)
		}
	}
	if _, workspaceID, err := lateralStartupConfig([]string{"--lateral-session", "new-session-id"}); err != nil || workspaceID != "" {
		t.Fatalf("legacy target startup rejected: workspace=%q err=%v", workspaceID, err)
	}
}

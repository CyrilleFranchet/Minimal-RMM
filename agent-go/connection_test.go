package main

import (
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strings"
	"testing"
)

func TestRegistrationPreservesBeaconAuthenticationThroughHTTPSProxy(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-RMM-Beacon-Token") != "beacon-secret" {
			http.Error(w, "FORBIDDEN", http.StatusForbidden)
			return
		}
		if r.URL.Path != "/register" || r.URL.Query().Get("id") != "go-test" || r.URL.Query().Get("agent") != "go" {
			http.Error(w, "invalid registration", http.StatusBadRequest)
			return
		}
		_, _ = w.Write([]byte("OK"))
	}))
	defer origin.Close()
	upstream, _ := url.Parse(origin.URL)
	edge := httptest.NewTLSServer(httputil.NewSingleHostReverseProxy(upstream))
	defer edge.Close()
	cfg := &config{BaseURL: edge.URL, BeaconSecret: "beacon-secret", SessionID: "go-test", SleepSeconds: 60}
	if err := register(edge.Client(), cfg); err != nil {
		t.Fatalf("registration through HTTPS proxy failed: %v", err)
	}
	cfg.BeaconSecret = "operator-token"
	if err := register(edge.Client(), cfg); err == nil || !strings.Contains(err.Error(), "HTTP 403: FORBIDDEN") {
		t.Fatalf("incorrect beacon secret did not produce expected 403: %v", err)
	}
}

func TestAgentEnvironmentOverridesEmbeddedConnection(t *testing.T) {
	oldURL, oldSecret := defaultBaseURLB64, defaultBeaconSecretB64
	defer func() { defaultBaseURLB64, defaultBeaconSecretB64 = oldURL, oldSecret }()
	defaultBaseURLB64 = base64.StdEncoding.EncodeToString([]byte("https://current.trycloudflare.com"))
	defaultBeaconSecretB64 = base64.StdEncoding.EncodeToString([]byte("embedded-secret"))
	t.Setenv("RMM_BASE_URL", "")
	t.Setenv("RMM_BEACON_SECRET", "")
	cfg, err := loadConfig()
	if err != nil || cfg.BaseURL != "https://current.trycloudflare.com" || cfg.BeaconSecret != "embedded-secret" {
		t.Fatal("embedded connection settings were not loaded")
	}
	t.Setenv("RMM_BASE_URL", "https://old.trycloudflare.com")
	t.Setenv("RMM_BEACON_SECRET", "environment-secret")
	cfg, err = loadConfig()
	if err != nil || cfg.BaseURL != "https://old.trycloudflare.com" || cfg.BeaconSecret != "environment-secret" {
		t.Fatal("explicit environment overrides were not honored")
	}
}

package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestTLSVersionNegotiation(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), DNSNames: []string{"test.local"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, version              string
		serverMin, serverMax, want uint16
		fail                       bool
	}{
		{"1.2 accepted", "1.2", tls.VersionTLS12, tls.VersionTLS13, tls.VersionTLS12, false},
		{"1.3 accepted", "1.3", tls.VersionTLS12, tls.VersionTLS13, tls.VersionTLS13, false},
		{"1.2 rejects 1.3-only server", "1.2", tls.VersionTLS13, tls.VersionTLS13, 0, true},
		{"1.3 rejects 1.2-only server", "1.3", tls.VersionTLS12, tls.VersionTLS12, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := newHTTPClient(&config{TLSVersion: tc.version})
			transport := client.Transport.(*http.Transport)
			transport.TLSClientConfig.RootCAs = x509.NewCertPool()
			transport.TLSClientConfig.RootCAs.AddCert(cert)
			transport.TLSClientConfig.ServerName = "test.local"
			defer client.CloseIdleConnections()
			clientPipe, serverPipe := net.Pipe()
			defer clientPipe.Close()
			defer serverPipe.Close()
			_ = clientPipe.SetDeadline(time.Now().Add(5 * time.Second))
			_ = serverPipe.SetDeadline(time.Now().Add(5 * time.Second))
			serverTLS := tls.Server(serverPipe, &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}, MinVersion: tc.serverMin, MaxVersion: tc.serverMax})
			serverErr := make(chan error, 1)
			go func() { serverErr <- serverTLS.Handshake() }()
			clientTLS := tls.Client(clientPipe, transport.TLSClientConfig)
			clientErr := clientTLS.Handshake()
			if tc.fail {
				if clientErr == nil {
					t.Fatal("incompatible TLS versions negotiated successfully")
				}
				if err := <-serverErr; err == nil {
					t.Fatal("server accepted incompatible TLS version")
				}
				return
			}
			if clientErr != nil {
				t.Fatal(clientErr)
			}
			if err := <-serverErr; err != nil {
				t.Fatal(err)
			}
			if got := clientTLS.ConnectionState().Version; got != tc.want {
				t.Fatalf("negotiated %x, want %x", got, tc.want)
			}
		})
	}
	if got := newHTTPClient(&config{TLSVersion: "default"}).Transport.(*http.Transport).TLSClientConfig; got != nil {
		t.Fatal("default must preserve Go's TLS settings")
	}
}

func TestTLSVersionConfiguration(t *testing.T) {
	old := defaultTLSVersionB64
	t.Cleanup(func() { defaultTLSVersionB64 = old })
	t.Setenv("RMM_BASE_URL", "http://example.invalid")
	t.Setenv("RMM_BEACON_SECRET", "test-secret")
	for _, tc := range []struct {
		embedded, environment, want string
		invalid                     bool
	}{
		{"", "", "default", false},
		{"1.2", "", "1.2", false},
		{"1.2", "1.3", "1.3", false},
		{"1.3", "default", "default", false},
		{"", "1.1", "", true},
	} {
		t.Run(tc.embedded+"/"+tc.environment, func(t *testing.T) {
			defaultTLSVersionB64 = base64.StdEncoding.EncodeToString([]byte(tc.embedded))
			t.Setenv("RMM_TLS_VERSION", tc.environment)
			cfg, err := loadConfig()
			if tc.invalid {
				if err == nil {
					t.Fatal("invalid version accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if cfg.TLSVersion != tc.want {
				t.Fatalf("got %q, want %q", cfg.TLSVersion, tc.want)
			}
		})
	}
}

func TestTLSSessionResumptionConfiguration(t *testing.T) {
	old := defaultTLSSessionResumptionB64
	t.Cleanup(func() { defaultTLSSessionResumptionB64 = old })
	t.Setenv("RMM_BASE_URL", "https://example.invalid")
	t.Setenv("RMM_BEACON_SECRET", "test")
	for _, tc := range []struct {
		embedded, environment string
		want                  bool
	}{
		{"", "", false}, {"1", "", true}, {"1", "false", false}, {"0", "yes", true},
	} {
		defaultTLSSessionResumptionB64 = base64.StdEncoding.EncodeToString([]byte(tc.embedded))
		t.Setenv("RMM_TLS_SESSION_RESUMPTION", tc.environment)
		cfg, err := loadConfig()
		if err != nil || cfg.TLSSessionResumption != tc.want {
			t.Fatalf("embedded=%q environment=%q: config=%+v err=%v", tc.embedded, tc.environment, cfg, err)
		}
	}
}

func TestTLSSessionResumptionAcrossConnections(t *testing.T) {
	for _, version := range []struct {
		name  string
		value uint16
	}{{"1.2", tls.VersionTLS12}, {"1.3", tls.VersionTLS13}, {"default", tls.VersionTLS13}} {
		for _, enabled := range []bool{false, true} {
			t.Run(version.name+"/"+strconv.FormatBool(enabled), func(t *testing.T) {
				var seen []bool
				server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					seen = append(seen, r.TLS.DidResume)
					_, _ = w.Write([]byte("ok"))
				}))
				server.TLS = &tls.Config{MinVersion: version.value, MaxVersion: version.value}
				server.StartTLS()
				defer server.Close()
				client := newHTTPClient(&config{TLSVersion: version.name, TLSSessionResumption: enabled})
				defer client.CloseIdleConnections()
				transport := client.Transport.(*http.Transport)
				if transport.TLSClientConfig == nil {
					transport.TLSClientConfig = &tls.Config{}
				}
				roots := x509.NewCertPool()
				roots.AddCert(server.Certificate())
				transport.TLSClientConfig.RootCAs = roots
				for i := 0; i < 2; i++ {
					resp, err := client.Get(server.URL)
					if err != nil {
						t.Fatal(err)
					}
					if _, err := io.Copy(io.Discard, resp.Body); err != nil {
						t.Fatal(err)
					}
					resp.Body.Close()
				}
				if len(seen) != 2 || seen[0] || seen[1] != enabled {
					t.Fatalf("resumption enabled=%v, handshakes=%v", enabled, seen)
				}
			})
		}
	}
}

func TestBeaconConnectionPersistence(t *testing.T) {
	for _, tc := range []struct {
		name            string
		tls, persistent bool
	}{
		{"http-short-default", false, false},
		{"http-persistent-opt-in", false, true},
		{"https-short-default", true, false},
		{"https-persistent-opt-in", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var originConnections atomic.Int32
			origin := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				// Closing each origin exchange must not determine the edge's pooling.
				w.Header().Set("Connection", "close")
				_, _ = w.Write([]byte("OK"))
			}))
			origin.Config.ConnState = func(_ net.Conn, state http.ConnState) {
				if state == http.StateNew {
					originConnections.Add(1)
				}
			}
			origin.Start()
			defer origin.Close()
			upstream, err := url.Parse(origin.URL)
			if err != nil {
				t.Fatal(err)
			}
			var connections atomic.Int32
			server := httptest.NewUnstartedServer(httputil.NewSingleHostReverseProxy(upstream))
			server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
				if state == http.StateNew {
					connections.Add(1)
				}
			}
			if tc.tls {
				server.StartTLS()
			} else {
				server.Start()
			}
			defer server.Close()
			cfg := &config{BaseURL: server.URL, BeaconSecret: "secret", SessionID: "connection-test", PersistentHTTP: tc.persistent}
			client := newHTTPClient(cfg)
			if tc.tls {
				roots := x509.NewCertPool()
				roots.AddCert(server.Certificate())
				client.Transport.(*http.Transport).TLSClientConfig = &tls.Config{RootCAs: roots}
			}
			defer client.CloseIdleConnections()
			for i := 0; i < 3; i++ {
				if err := register(client, cfg); err != nil {
					t.Fatal(err)
				}
			}
			want := int32(3)
			if tc.persistent {
				want = 1
			}
			if got := connections.Load(); got != want {
				t.Fatalf("got %d TCP connections, want %d", got, want)
			}
			if got := originConnections.Load(); got != 3 {
				t.Fatalf("origin must close each exchange independently: got %d TCP connections, want 3", got)
			}
		})
	}
}

func TestPersistentHTTPConfiguration(t *testing.T) {
	old := defaultPersistentHTTPB64
	t.Cleanup(func() { defaultPersistentHTTPB64 = old })
	t.Setenv("RMM_BASE_URL", "http://example.invalid")
	t.Setenv("RMM_BEACON_SECRET", "test-secret")
	for _, tc := range []struct {
		name, embedded, environment string
		want                        bool
	}{
		{"default-short", "", "", false},
		{"embedded-opt-in", "true", "", true},
		{"environment-disable", "true", "0", false},
		{"environment-enable", "false", "1", true},
		{"case-insensitive", "", " TRUE ", true},
		{"yes", "", "yes", true},
		{"on", "", "on", true},
		{"false", "true", "false", false},
		{"invalid-is-short", "true", "invalid", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			defaultPersistentHTTPB64 = base64.StdEncoding.EncodeToString([]byte(tc.embedded))
			t.Setenv("RMM_PERSISTENT_HTTP", tc.environment)
			cfg, err := loadConfig()
			if err != nil {
				t.Fatal(err)
			}
			if cfg.PersistentHTTP != tc.want {
				t.Fatalf("got PersistentHTTP=%v, want %v", cfg.PersistentHTTP, tc.want)
			}
		})
	}
}

func TestBeaconRedirectDoesNotForwardSecret(t *testing.T) {
	var contacted atomic.Bool
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		contacted.Store(true)
		_, _ = w.Write([]byte("OK"))
	}))
	defer destination.Close()
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, destination.URL, http.StatusTemporaryRedirect)
	}))
	defer origin.Close()
	cfg := &config{BaseURL: origin.URL, BeaconSecret: "secret", SessionID: "test"}
	client := newHTTPClient(cfg)
	defer client.CloseIdleConnections()
	if err := register(client, cfg); err == nil {
		t.Fatal("redirect was treated as successful registration")
	}
	if contacted.Load() {
		t.Fatal("redirect destination received the authenticated beacon")
	}
}

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

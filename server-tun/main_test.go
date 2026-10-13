package main

import (
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/stack"

	M "github.com/xjasonlyu/tun2socks/v2/metadata"
)

func TestValidateConfig(t *testing.T) {
	valid := tunConfig{
		Device:     "tun://rmm0",
		Proxy:      "socks5://127.0.0.1:1080",
		Interface:  "eth0",
		UDPTimeout: time.Minute,
	}
	if err := validateConfig(valid); err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}

	tests := map[string]tunConfig{
		"missing device": {Proxy: valid.Proxy, UDPTimeout: valid.UDPTimeout},
		"missing proxy":  {Device: valid.Device, UDPTimeout: valid.UDPTimeout},
		"wrong proxy":    {Device: valid.Device, Proxy: "http://127.0.0.1:1080", UDPTimeout: valid.UDPTimeout},
		"control device": {Device: "tun://rmm\n0", Proxy: valid.Proxy, UDPTimeout: valid.UDPTimeout},
		"invalid mtu":    {Device: valid.Device, Proxy: valid.Proxy, MTU: 65536, UDPTimeout: valid.UDPTimeout},
		"short timeout":  {Device: valid.Device, Proxy: valid.Proxy, UDPTimeout: 500 * time.Millisecond},
	}
	for name, cfg := range tests {
		t.Run(name, func(t *testing.T) {
			if err := validateConfig(cfg); err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
}

func TestValidateConfigRejectsProxyCredentials(t *testing.T) {
	// Credentials are not needed for the local RMM SOCKS listener. Keeping the
	// helper's input limited avoids accidentally placing operator secrets in a
	// child-process command line.
	cfg := tunConfig{
		Device:     "tun://rmm0",
		Proxy:      "socks5://user:password@127.0.0.1:1080",
		UDPTimeout: time.Minute,
	}
	if err := validateConfig(cfg); err == nil || !strings.Contains(err.Error(), "credentials") {
		t.Fatalf("expected credential rejection, got %v", err)
	}
}

func TestParseTUNDevice(t *testing.T) {
	tests := map[string]string{
		"URL form":   "tun://rmm0",
		"bare Linux": "rmm0",
		"bare macOS": "utun4",
	}
	for name, input := range tests {
		t.Run(name, func(t *testing.T) {
			got, err := parseTUNDevice(input)
			if err != nil {
				t.Fatalf("parseTUNDevice(%q): %v", input, err)
			}
			want := strings.TrimPrefix(input, "tun://")
			if got != want {
				t.Fatalf("parseTUNDevice(%q) = %q, want %q", input, got, want)
			}
		})
	}

	for _, input := range []string{"http://rmm0", "tun://rmm0/path", "tun://rmm0?x=1", "tun://rmm/0"} {
		t.Run("reject "+input, func(t *testing.T) {
			if _, err := parseTUNDevice(input); err == nil {
				t.Fatalf("parseTUNDevice(%q) accepted an invalid device", input)
			}
		})
	}
}

func TestMetadataFromID(t *testing.T) {
	id := stack.TransportEndpointID{
		LocalAddress:  tcpip.AddrFrom4([4]byte{10, 10, 10, 20}),
		LocalPort:     443,
		RemoteAddress: tcpip.AddrFrom4([4]byte{198, 18, 0, 7}),
		RemotePort:    40123,
	}
	metadata := metadataFromID(id, M.TCP)
	if metadata.Network != M.TCP || metadata.SourceAddress() != "198.18.0.7:40123" || metadata.DestinationAddress() != "10.10.10.20:443" {
		t.Fatalf("metadataFromID returned unexpected metadata: %+v", metadata)
	}
}

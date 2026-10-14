package main

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/link/channel"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
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

// testProxy records metadata and returns an in-memory stream for TCP tests.
type testProxy struct {
	err    error
	dialed chan *M.Metadata
	peers  []net.Conn
}

// DialContext records a TCP proxy attempt and returns a test stream.
func (p *testProxy) DialContext(_ context.Context, metadata *M.Metadata) (net.Conn, error) {
	if p.dialed != nil {
		p.dialed <- metadata
	}
	if p.err != nil {
		return nil, p.err
	}
	local, peer := net.Pipe()
	p.peers = append(p.peers, peer)
	return local, nil
}

// DialUDP is unused by the TCP forwarding tests.
func (p *testProxy) DialUDP(*M.Metadata) (net.PacketConn, error) {
	return nil, errors.New("UDP is not used by this test")
}

// closePeers releases the peer ends kept open by successful test dials.
func (p *testProxy) closePeers() {
	for _, peer := range p.peers {
		_ = peer.Close()
	}
}

// TestTCPForwarderWaitsForProxyResult verifies the SYN-proxy behavior.
func TestTCPForwarderWaitsForProxyResult(t *testing.T) {
	tests := []struct {
		name       string
		proxyError error
		wantFlags  header.TCPFlags
	}{
		{name: "proxy success", wantFlags: header.TCPFlagSyn | header.TCPFlagAck},
		{name: "proxy failure", proxyError: errors.New("target refused"), wantFlags: header.TCPFlagRst},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			proxy := &testProxy{
				err:    test.proxyError,
				dialed: make(chan *M.Metadata, 1),
			}
			link := channel.New(8, 1500, "")
			handler := &relayHandler{proxy: proxy, udpTimeout: time.Minute}
			stackInstance, err := createStack(link, handler)
			if err != nil {
				t.Fatalf("createStack: %v", err)
			}
			defer func() {
				stackInstance.Close()
				stackInstance.Wait()
				proxy.closePeers()
			}()

			injectTCPsyn(t, link)
			select {
			case metadata := <-proxy.dialed:
				if got := metadata.DestinationAddress(); got != "10.10.10.20:443" {
					t.Fatalf("proxy destination = %q, want 10.10.10.20:443", got)
				}
			case <-time.After(time.Second):
				t.Fatal("forwarder did not attempt the proxy connection")
			}

			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			packet := link.ReadContext(ctx)
			if packet == nil {
				t.Fatal("forwarder did not produce a TCP response")
			}
			defer packet.DecRef()
			flags := header.TCP(packet.TransportHeader().Slice()).Flags()
			if !flags.Contains(test.wantFlags) {
				t.Fatalf("TCP response flags = %s, want %s", flags, test.wantFlags)
			}
		})
	}
}

// TestTCPForwarderRelaysRemoteData verifies that bytes received after the
// local three-way handshake are written back to the TUN link endpoint.
func TestTCPForwarderRelaysRemoteData(t *testing.T) {
	proxy := &testProxy{dialed: make(chan *M.Metadata, 1)}
	link := channel.New(32, 1500, "")
	handler := &relayHandler{proxy: proxy, udpTimeout: time.Minute}
	stackInstance, err := createStack(link, handler)
	if err != nil {
		t.Fatalf("createStack: %v", err)
	}
	defer func() {
		stackInstance.Close()
		stackInstance.Wait()
		proxy.closePeers()
	}()

	injectTCPsyn(t, link)
	select {
	case <-proxy.dialed:
	case <-time.After(time.Second):
		t.Fatal("forwarder did not attempt the proxy connection")
	}

	synAck := readTCPPacket(t, link)
	serverSequence := header.TCP(synAck.TransportHeader().Slice()).SequenceNumber()
	synAck.DecRef()

	injectTCPPacket(t, link, tcpPacketFields{
		SrcPort: 40123,
		DstPort: 443,
		SeqNum:  101,
		AckNum:  serverSequence + 1,
		Flags:   header.TCPFlagAck,
		Payload: nil,
	})

	if len(proxy.peers) != 1 {
		t.Fatalf("proxy peer count = %d, want 1", len(proxy.peers))
	}
	if _, err := proxy.peers[0].Write([]byte("target-response")); err != nil {
		t.Fatalf("write proxy response: %v", err)
	}

	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		packet := readTCPPacketWithTimeout(link, 100*time.Millisecond)
		if packet == nil {
			continue
		}
		payload, ok := packet.Data().PullUp(packet.Data().Size())
		if !ok {
			packet.DecRef()
			continue
		}
		if string(payload) == "target-response" {
			packet.DecRef()
			return
		}
		packet.DecRef()
	}
	t.Fatal("target response did not reach the TUN link endpoint")
}

type tcpPacketFields struct {
	SrcPort uint16
	DstPort uint16
	SeqNum  uint32
	AckNum  uint32
	Flags   header.TCPFlags
	Payload []byte
}

// readTCPPacket returns the next packet or fails the test after one second.
func readTCPPacket(t *testing.T, link *channel.Endpoint) *stack.PacketBuffer {
	t.Helper()
	packet := readTCPPacketWithTimeout(link, time.Second)
	if packet == nil {
		t.Fatal("timed out waiting for TCP packet")
	}
	return packet
}

// readTCPPacketWithTimeout returns nil when the in-memory link has no packet.
func readTCPPacketWithTimeout(link *channel.Endpoint, timeout time.Duration) *stack.PacketBuffer {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return link.ReadContext(ctx)
}

// injectTCPPacket sends a valid IPv4 TCP segment into the in-memory link.
func injectTCPPacket(t *testing.T, link *channel.Endpoint, fields tcpPacketFields) {
	t.Helper()
	src := tcpip.AddrFrom4([4]byte{198, 18, 0, 7})
	dst := tcpip.AddrFrom4([4]byte{10, 10, 10, 20})
	raw := make([]byte, header.IPv4MinimumSize+header.TCPMinimumSize+len(fields.Payload))
	ipHeader := header.IPv4(raw[:header.IPv4MinimumSize])
	ipHeader.Encode(&header.IPv4Fields{
		TotalLength: uint16(len(raw)),
		ID:          2,
		TTL:         64,
		Protocol:    uint8(header.TCPProtocolNumber),
		SrcAddr:     src,
		DstAddr:     dst,
	})
	ipHeader.SetChecksum(^ipHeader.CalculateChecksum())
	tcpHeader := header.TCP(raw[header.IPv4MinimumSize:])
	tcpHeader.Encode(&header.TCPFields{
		SrcPort:    fields.SrcPort,
		DstPort:    fields.DstPort,
		SeqNum:     fields.SeqNum,
		AckNum:     fields.AckNum,
		DataOffset: header.TCPMinimumSize,
		Flags:      fields.Flags,
		WindowSize: 65535,
	})
	copy(raw[header.IPv4MinimumSize+header.TCPMinimumSize:], fields.Payload)
	partial := header.PseudoHeaderChecksum(
		header.TCPProtocolNumber,
		src,
		dst,
		uint16(len(tcpHeader)),
	)
	tcpHeader.SetChecksum(^tcpHeader.CalculateChecksum(partial))

	packet := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(raw)})
	link.InjectInbound(ipv4.ProtocolNumber, packet)
	packet.DecRef()
}

// injectTCPsyn sends one valid IPv4 SYN into the in-memory link endpoint.
func injectTCPsyn(t *testing.T, link *channel.Endpoint) {
	t.Helper()
	src := tcpip.AddrFrom4([4]byte{198, 18, 0, 7})
	dst := tcpip.AddrFrom4([4]byte{10, 10, 10, 20})
	raw := make([]byte, header.IPv4MinimumSize+header.TCPMinimumSize)
	ipHeader := header.IPv4(raw[:header.IPv4MinimumSize])
	ipHeader.Encode(&header.IPv4Fields{
		TotalLength: uint16(len(raw)),
		ID:          1,
		TTL:         64,
		Protocol:    uint8(header.TCPProtocolNumber),
		SrcAddr:     src,
		DstAddr:     dst,
	})
	ipHeader.SetChecksum(^ipHeader.CalculateChecksum())
	tcpHeader := header.TCP(raw[header.IPv4MinimumSize:])
	tcpHeader.Encode(&header.TCPFields{
		SrcPort:    40123,
		DstPort:    443,
		SeqNum:     100,
		DataOffset: header.TCPMinimumSize,
		Flags:      header.TCPFlagSyn,
		WindowSize: 65535,
	})
	partial := header.PseudoHeaderChecksum(
		header.TCPProtocolNumber,
		src,
		dst,
		uint16(len(tcpHeader)),
	)
	tcpHeader.SetChecksum(^tcpHeader.CalculateChecksum(partial))

	packet := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(raw)})
	link.InjectInbound(ipv4.ProtocolNumber, packet)
	packet.DecRef()
}

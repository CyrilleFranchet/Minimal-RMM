// Command rmm-tun provides the RMM server-side TUN and user-space IP stack.
//
// The command is deliberately a small process boundary around the network
// stack. The Python RMM server owns lifecycle and operator authorization; this
// process owns the privileged TUN file descriptor and translates packets into
// connections through the RMM SOCKS listener.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/netip"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"gvisor.dev/gvisor/pkg/buffer"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"gvisor.dev/gvisor/pkg/tcpip/checksum"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv4"
	"gvisor.dev/gvisor/pkg/tcpip/network/ipv6"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/icmp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
	"gvisor.dev/gvisor/pkg/tcpip/transport/udp"
	"gvisor.dev/gvisor/pkg/waiter"

	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device/tun"
	"github.com/xjasonlyu/tun2socks/v2/core/option"
	"github.com/xjasonlyu/tun2socks/v2/dialer"
	"github.com/xjasonlyu/tun2socks/v2/log"
	M "github.com/xjasonlyu/tun2socks/v2/metadata"
	"github.com/xjasonlyu/tun2socks/v2/proxy"
	"github.com/xjasonlyu/tun2socks/v2/proxy/socks5"
)

const (
	// tcpConnectTimeout covers the agent-side SOCKS CONNECT before the local
	// TUN client receives a TCP SYN-ACK.
	tcpConnectTimeout = 20 * time.Second
	// tcpWaitTimeout gives the other half of a TCP relay time to close after
	// the first direction reaches EOF.
	tcpWaitTimeout = 60 * time.Second
	// maxTCPForwarderRequests bounds requests that are waiting for the agent.
	maxTCPForwarderRequests = 2 << 10
)

type tunConfig struct {
	Device     string
	Proxy      string
	Interface  string
	MTU        int
	LogLevel   string
	UDPTimeout time.Duration
}

func main() {
	cfg := tunConfig{}
	flag.StringVar(&cfg.Device, "device", "", "TUN device, for example tun://rmm0 or utun4")
	flag.StringVar(&cfg.Proxy, "proxy", "", "SOCKS5 proxy URL")
	flag.StringVar(&cfg.Interface, "interface", "", "Egress interface used for proxy connections")
	flag.IntVar(&cfg.MTU, "mtu", 0, "TUN MTU; zero uses the device default")
	flag.StringVar(&cfg.LogLevel, "log-level", "warn", "Stack log level")
	flag.DurationVar(&cfg.UDPTimeout, "udp-timeout", 5*time.Minute, "UDP flow idle timeout")
	flag.Parse()

	if err := validateConfig(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "rmm-tun: %v\n", err)
		os.Exit(2)
	}
	if err := runStack(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "rmm-tun: %v\n", err)
		os.Exit(1)
	}
}

// runStack opens the TUN device, starts the delayed-handshake IP stack, and
// waits until the helper is asked to stop.
func runStack(cfg tunConfig) error {
	levelText := strings.TrimSpace(cfg.LogLevel)
	if levelText == "" {
		levelText = "warn"
	}
	level, err := log.ParseLevel(levelText)
	if err != nil {
		return fmt.Errorf("invalid log level: %w", err)
	}
	logger, err := log.NewLeveled(level)
	if err != nil {
		return fmt.Errorf("create logger: %w", err)
	}
	log.SetLogger(logger)

	dialer.Reset()
	if cfg.Interface != "" {
		iface, err := net.InterfaceByName(cfg.Interface)
		if err != nil {
			return fmt.Errorf("find egress interface %q: %w", cfg.Interface, err)
		}
		dialer.RegisterSockOpt(dialer.WithBindToInterface(iface))
		log.Infof("[DIALER] bind to interface: %s", cfg.Interface)
	}

	deviceName, err := parseTUNDevice(cfg.Device)
	if err != nil {
		return err
	}
	proxyClient, err := parseSocks5Proxy(cfg.Proxy)
	if err != nil {
		return err
	}
	linkEndpoint, err := tun.Open(deviceName, uint32(cfg.MTU))
	if err != nil {
		return fmt.Errorf("open TUN device %q: %w", deviceName, err)
	}
	defer linkEndpoint.Close()

	handler := &relayHandler{
		proxy:      proxyClient,
		udpTimeout: cfg.UDPTimeout,
	}
	stackInstance, err := createStack(linkEndpoint, handler)
	if err != nil {
		return err
	}
	defer func() {
		stackInstance.Close()
		stackInstance.Wait()
	}()

	log.Infof("[STACK] %s <-> %s", cfg.Device, cfg.Proxy)
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	<-signals
	return nil
}

// parseTUNDevice normalizes a bare platform device name to tun:// syntax and
// rejects URL features that are not part of the helper's contract.
func parseTUNDevice(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errors.New("device is required")
	}
	if !strings.Contains(raw, "://") {
		raw = "tun://" + raw
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("invalid device: %w", err)
	}
	if parsed.Scheme != "tun" || parsed.Host == "" ||
		(parsed.Path != "" && parsed.Path != "/") || parsed.RawQuery != "" ||
		parsed.Fragment != "" || parsed.User != nil {
		return "", errors.New("device must be tun://<name> or a platform TUN name")
	}
	for _, character := range parsed.Host {
		if (character < 'a' || character > 'z') &&
			(character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') &&
			character != '_' && character != '.' && character != '-' {
			return "", errors.New("device name contains unsupported characters")
		}
	}
	return parsed.Host, nil
}

// parseSocks5Proxy constructs the only proxy type accepted by the helper.
func parseSocks5Proxy(raw string) (proxy.Proxy, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Scheme != "socks5" || parsed.Host == "" {
		return nil, errors.New("proxy must be a socks5:// URL with a host")
	}
	if parsed.User != nil {
		return nil, errors.New("proxy credentials are not supported")
	}
	client, err := socks5.Parse(parsed)
	if err != nil {
		return nil, fmt.Errorf("parse proxy: %w", err)
	}
	return client, nil
}

// createStack creates the gVisor stack with the custom TCP forwarder installed
// before the TUN NIC is attached. The stock tun2socks forwarder completes the
// local TCP handshake first, which makes closed target ports look open to a
// scanner. The custom forwarder waits for the proxy CONNECT first.
func createStack(linkEndpoint stack.LinkEndpoint, handler *relayHandler) (*stack.Stack, error) {
	stackInstance := stack.New(stack.Options{
		NetworkProtocols: []stack.NetworkProtocolFactory{
			ipv4.NewProtocol,
			ipv6.NewProtocol,
		},
		TransportProtocols: []stack.TransportProtocolFactory{
			tcp.NewProtocol,
			udp.NewProtocol,
			icmp.NewProtocol4,
			icmp.NewProtocol6,
		},
	})
	cleanup := func(err error) (*stack.Stack, error) {
		stackInstance.Close()
		stackInstance.Wait()
		return nil, err
	}

	if err := option.WithDefault()(stackInstance); err != nil {
		return cleanup(fmt.Errorf("configure stack defaults: %w", err))
	}

	tcpForwarder := tcp.NewForwarder(
		stackInstance,
		0,
		maxTCPForwarderRequests,
		handler.handleTCPRequest,
	)
	stackInstance.SetTransportProtocolHandler(tcp.ProtocolNumber, tcpForwarder.HandlePacket)

	udpForwarder := udp.NewForwarder(stackInstance, handler.handleUDPRequest)
	stackInstance.SetTransportProtocolHandler(udp.ProtocolNumber, udpForwarder.HandlePacket)
	installICMPHandler(stackInstance)

	nicID := stackInstance.NextNICID()
	if err := stackInstance.CreateNICWithOptions(nicID, linkEndpoint, stack.NICOptions{}); err != nil {
		return cleanup(fmt.Errorf("create TUN NIC: %v", err))
	}
	if err := stackInstance.SetPromiscuousMode(nicID, true); err != nil {
		return cleanup(fmt.Errorf("enable TUN promiscuous mode: %v", err))
	}
	if err := stackInstance.SetSpoofing(nicID, true); err != nil {
		return cleanup(fmt.Errorf("enable TUN spoofing: %v", err))
	}
	stackInstance.SetRouteTable([]tcpip.Route{
		{Destination: header.IPv4EmptySubnet, NIC: nicID},
		{Destination: header.IPv6EmptySubnet, NIC: nicID},
	})
	return stackInstance, nil
}

// installICMPHandler keeps the helper's local IPv4 echo behavior. It does not
// forward ICMP to the agent; the relay contract only carries TCP and UDP.
func installICMPHandler(stackInstance *stack.Stack) {
	handler := &icmpForwarder{stack: stackInstance}
	stackInstance.SetTransportProtocolHandler(icmp.ProtocolNumber4, handler.handlePacket)
	stackInstance.SetTransportProtocolHandler(icmp.ProtocolNumber6, handler.handlePacket)
}

type relayHandler struct {
	proxy      proxy.Proxy
	udpTimeout time.Duration
}

var _ adapter.TransportHandler = (*relayHandler)(nil)

// HandleTCP closes an already-established connection. The custom forwarder
// calls handleTCPRequest before the local handshake, so this method is only a
// defensive implementation of adapter.TransportHandler.
func (h *relayHandler) HandleTCP(conn adapter.TCPConn) {
	_ = conn.Close()
}

// HandleUDP starts one UDP association without blocking the gVisor dispatcher.
func (h *relayHandler) HandleUDP(conn adapter.UDPConn) {
	go h.handleUDPConn(conn)
}

// handleTCPRequest performs the agent-side SOCKS CONNECT before completing the
// local TUN handshake. A failed CONNECT sends a TCP reset to the local client.
func (h *relayHandler) handleTCPRequest(request *tcp.ForwarderRequest) {
	id := request.ID()
	metadata := metadataFromID(id, M.TCP)
	log.Infof("[TCP] connect %s -> %s", metadata.SourceAddress(), metadata.DestinationAddress())
	ctx, cancel := context.WithTimeout(context.Background(), tcpConnectTimeout)
	remoteConn, err := h.proxy.DialContext(ctx, metadata)
	cancel()
	if err != nil {
		log.Warnf("[TCP] dial %s: %v", metadata.DestinationAddress(), err)
		request.Complete(true)
		return
	}
	defer remoteConn.Close()
	log.Infof("[TCP] connected %s -> %s", metadata.SourceAddress(), metadata.DestinationAddress())

	var queue waiter.Queue
	endpoint, tcpErr := request.CreateEndpoint(&queue)
	if tcpErr != nil {
		log.Warnf("[TCP] complete local handshake for %s: %v", metadata.DestinationAddress(), tcpErr)
		request.Complete(true)
		return
	}
	defer request.Complete(false)

	localConn := gonet.NewTCPConn(&queue, endpoint)
	defer localConn.Close()
	log.Infof("[TCP] %s <-> %s", metadata.SourceAddress(), metadata.DestinationAddress())
	pipeTCP(localConn, remoteConn)
}

// handleUDPRequest creates a gVisor UDP connection and dispatches it to the
// proxy relay.
func (h *relayHandler) handleUDPRequest(request *udp.ForwarderRequest) bool {
	var queue waiter.Queue
	endpoint, tcpErr := request.CreateEndpoint(&queue)
	if tcpErr != nil {
		id := request.ID()
		log.Debugf("[UDP] create endpoint %s:%d->%s:%d: %v", id.RemoteAddress, id.RemotePort, id.LocalAddress, id.LocalPort, tcpErr)
		return false
	}
	conn := &udpConn{
		UDPConn: gonet.NewUDPConn(&queue, endpoint),
		id:      request.ID(),
	}
	h.HandleUDP(conn)
	return true
}

type udpConn struct {
	*gonet.UDPConn
	id stack.TransportEndpointID
}

func (c *udpConn) ID() stack.TransportEndpointID {
	return c.id
}

// handleUDPConn opens one SOCKS5 UDP association and copies datagrams in both
// directions until the flow is idle or one side closes.
func (h *relayHandler) handleUDPConn(origin adapter.UDPConn) {
	defer origin.Close()
	metadata := metadataFromID(origin.ID(), M.UDP)
	remote, err := h.proxy.DialUDP(metadata)
	if err != nil {
		log.Warnf("[UDP] dial %s: %v", metadata.DestinationAddress(), err)
		return
	}
	defer remote.Close()

	var destination net.Addr = metadata.Addr()
	if udpAddress := metadata.UDPAddr(); udpAddress != nil {
		destination = udpAddress
	}
	remote = newSymmetricNATPacketConn(remote, metadata)
	log.Infof("[UDP] %s <-> %s", metadata.SourceAddress(), metadata.DestinationAddress())
	pipeUDP(origin, remote, destination, h.udpTimeout)
}

// metadataFromID converts a gVisor four-tuple into tun2socks proxy metadata.
func metadataFromID(id stack.TransportEndpointID, network M.Network) *M.Metadata {
	return &M.Metadata{
		Network: network,
		SrcIP:   parseStackAddress(id.RemoteAddress),
		SrcPort: id.RemotePort,
		DstIP:   parseStackAddress(id.LocalAddress),
		DstPort: id.LocalPort,
	}
}

// parseStackAddress converts a gVisor address without doing DNS resolution.
func parseStackAddress(address tcpip.Address) netip.Addr {
	parsed, _ := netip.AddrFromSlice(address.AsSlice())
	return parsed
}

// pipeTCP copies a TCP stream in both directions and preserves half-close
// semantics for protocols that use EOF to delimit a request.
func pipeTCP(origin, remote net.Conn) {
	var wait sync.WaitGroup
	wait.Add(2)
	go copyTCPDirection(remote, origin, "origin->remote", &wait)
	go copyTCPDirection(origin, remote, "remote->origin", &wait)
	wait.Wait()
}

func copyTCPDirection(destination, source net.Conn, direction string, wait *sync.WaitGroup) {
	defer wait.Done()
	buffer := make([]byte, 20<<10)
	if _, err := io.CopyBuffer(destination, source, buffer); err != nil {
		log.Debugf("[TCP] copy data for %s: %v", direction, err)
	}
	if closer, ok := source.(interface{ CloseRead() error }); ok {
		_ = closer.CloseRead()
	}
	if closer, ok := destination.(interface{ CloseWrite() error }); ok {
		_ = closer.CloseWrite()
	}
	_ = destination.SetReadDeadline(time.Now().Add(tcpWaitTimeout))
}

// pipeUDP copies datagrams in both directions with the configured idle timeout.
func pipeUDP(origin, remote net.PacketConn, destination net.Addr, timeout time.Duration) {
	var wait sync.WaitGroup
	wait.Add(2)
	go copyUDPDirection(remote, origin, destination, "origin->remote", timeout, &wait)
	go copyUDPDirection(origin, remote, nil, "remote->origin", timeout, &wait)
	wait.Wait()
}

func copyUDPDirection(destination, source net.PacketConn, target net.Addr, direction string, timeout time.Duration, wait *sync.WaitGroup) {
	defer wait.Done()
	buffer := make([]byte, (1<<16)-1)
	for {
		_ = source.SetReadDeadline(time.Now().Add(timeout))
		length, _, err := source.ReadFrom(buffer)
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return
		}
		if err == io.EOF {
			return
		}
		if err != nil {
			log.Debugf("[UDP] copy data for %s: %v", direction, err)
			return
		}
		if _, err := destination.WriteTo(buffer[:length], target); err != nil {
			log.Debugf("[UDP] copy data for %s: %v", direction, err)
			return
		}
		_ = destination.SetReadDeadline(time.Now().Add(timeout))
	}
}

type symmetricNATPacketConn struct {
	net.PacketConn
	source      string
	destination string
}

func newSymmetricNATPacketConn(conn net.PacketConn, metadata *M.Metadata) *symmetricNATPacketConn {
	return &symmetricNATPacketConn{
		PacketConn:  conn,
		source:      metadata.SourceAddress(),
		destination: metadata.DestinationAddress(),
	}
}

func (conn *symmetricNATPacketConn) ReadFrom(packet []byte) (int, net.Addr, error) {
	for {
		length, source, err := conn.PacketConn.ReadFrom(packet)
		if source != nil && source.String() != conn.destination {
			log.Warnf("[UDP] symmetric NAT %s->%s: drop packet from %s", conn.source, conn.destination, source)
			continue
		}
		return length, source, err
	}
}

type icmpForwarder struct {
	stack *stack.Stack
}

// handlePacket responds to local IPv4 echo requests. IPv6 and non-echo ICMP
// packets are left unhandled because the agent transport has no raw packet
// channel.
func (f *icmpForwarder) handlePacket(_ stack.TransportEndpointID, packet *stack.PacketBuffer) bool {
	switch packet.NetworkProtocolNumber {
	case ipv4.ProtocolNumber:
		return f.handleIPv4(packet)
	case ipv6.ProtocolNumber:
		return false
	default:
		return false
	}
}

func (f *icmpForwarder) handleIPv4(packet *stack.PacketBuffer) bool {
	icmpHeader := header.ICMPv4(packet.TransportHeader().Slice())
	if icmpHeader.Type() != header.ICMPv4Echo {
		return false
	}
	ipHeader := header.IPv4(packet.NetworkHeader().Slice())
	localAddress := ipHeader.DestinationAddress()
	if packet.NetworkPacketInfo.LocalAddressBroadcast || header.IsV4MulticastAddress(localAddress) {
		localAddress = tcpip.Address{}
	}
	route, err := f.stack.FindRoute(
		packet.NICID,
		localAddress,
		ipHeader.SourceAddress(),
		ipv4.ProtocolNumber,
		false,
	)
	if err != nil {
		return false
	}
	defer route.Release()

	replyData := stack.PayloadSince(packet.TransportHeader())
	defer replyData.Release()
	replyHeader := header.ICMPv4(replyData.AsSlice())
	replyHeader.SetType(header.ICMPv4EchoReply)
	replyHeader.SetCode(0)
	replyHeader.SetChecksum(0)
	replyHeader.SetChecksum(^checksum.Checksum(replyData.AsSlice(), 0))

	replyPacket := stack.NewPacketBuffer(stack.PacketBufferOptions{
		ReserveHeaderBytes: int(route.MaxHeaderLength()),
		Payload:            buffer.MakeWithView(replyData.Clone()),
	})
	defer replyPacket.DecRef()
	if err := route.WritePacket(stack.NetworkHeaderParams{
		Protocol: header.ICMPv4ProtocolNumber,
		TTL:      route.DefaultTTL(),
	}, replyPacket); err != nil {
		return false
	}
	return true
}

func validateConfig(cfg tunConfig) error {
	if strings.TrimSpace(cfg.Device) == "" {
		return errors.New("device is required")
	}
	if strings.ContainsAny(cfg.Device, "\r\n\x00") {
		return errors.New("device contains control characters")
	}
	if _, err := parseTUNDevice(cfg.Device); err != nil {
		return err
	}
	if strings.TrimSpace(cfg.Proxy) == "" {
		return errors.New("proxy is required")
	}
	parsed, err := url.Parse(cfg.Proxy)
	if err != nil || parsed.Host == "" || parsed.Scheme != "socks5" {
		return errors.New("proxy must be a socks5:// URL with a host")
	}
	if parsed.User != nil {
		return errors.New("proxy credentials are not supported")
	}
	if cfg.MTU < 0 || cfg.MTU > 65535 {
		return errors.New("mtu must be between 0 and 65535")
	}
	if cfg.UDPTimeout < time.Second || cfg.UDPTimeout > 24*time.Hour {
		return errors.New("udp-timeout must be between one second and 24 hours")
	}
	if strings.ContainsAny(cfg.Interface, "\r\n\x00") {
		return errors.New("interface contains control characters")
	}
	return nil
}

// Command rmm-tun provides the RMM server-side TUN and user-space IP stack.
//
// The command is deliberately a small process boundary around the network
// stack. The Python RMM server owns lifecycle and operator authorization; this
// process owns the privileged TUN file descriptor and translates packets into
// connections through the RMM SOCKS listener.
package main

import (
	"errors"
	"flag"
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	_ "github.com/xjasonlyu/tun2socks/v2/dns"
	"github.com/xjasonlyu/tun2socks/v2/engine"
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

	key := &engine.Key{
		Device:     cfg.Device,
		Proxy:      cfg.Proxy,
		Interface:  cfg.Interface,
		MTU:        cfg.MTU,
		LogLevel:   cfg.LogLevel,
		UDPTimeout: cfg.UDPTimeout,
	}
	engine.Insert(key)
	engine.Start()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	<-signals
	engine.Stop()
}

func validateConfig(cfg tunConfig) error {
	if strings.TrimSpace(cfg.Device) == "" {
		return errors.New("device is required")
	}
	if strings.ContainsAny(cfg.Device, "\r\n\x00") {
		return errors.New("device contains control characters")
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

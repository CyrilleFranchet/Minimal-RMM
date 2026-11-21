package main

import (
	"errors"
	"runtime"
	"strings"
)

// wmiProbePrefix is the lab diagnostic command line: the Go agent runs
// two read-only WMI connections under the active make_token identity and a
// third through the explicit credential plumbing, reporting every stage
// with UTC timestamps, OS thread identity, raw HRESULTs, and durations.
// The optional `held` mode runs both connections inside one continuous
// COM initialization (one CoInitializeSecurity, no CoUninitialize between
// them) to isolate whether the per-attempt teardown triggers the denial.
// It writes nothing to the target, never touches credentials in its
// output, and exists to diagnose the one-shot second-connect failure
// without deploying anything.
const wmiProbePrefix = "__WMI_PROBE__"

// parseWmiProbeCommand extracts the target host — written exactly as the
// lateral deployment commands accept it: an IP literal or a DNS name — and
// the optional mode. Any other command family or mode word is rejected so
// the parser never matches by accident.
func parseWmiProbeCommand(command string) (host, mode string, err error) {
	trimmed := strings.TrimSpace(command)
	if !strings.HasPrefix(trimmed, wmiProbePrefix) {
		return "", "", errors.New("not a wmi probe command")
	}
	fields := strings.Fields(strings.TrimPrefix(trimmed, wmiProbePrefix))
	switch len(fields) {
	case 1:
	case 2:
		mode = strings.ToLower(fields[1])
		if mode != "held" {
			return "", "", errors.New("the only optional wmi probe mode is: held")
		}
	default:
		return "", "", errors.New("usage: __WMI_PROBE__ <host> [held]")
	}
	host = fields[0]
	if host == "" || strings.ContainsAny(host, "\\/") {
		return "", "", errors.New("the wmi probe target must be a single host name")
	}
	return host, mode, nil
}

// wmiProbeCommand executes the diagnostic. Only the Windows build can probe;
// like the lateral commands, other builds fail explicitly instead of
// silently degrading.
func wmiProbeCommand(command string) (string, error) {
	host, mode, err := parseWmiProbeCommand(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("wmi probe requires the Windows agent build")
	}
	return wmiProbeExecute(host, mode)
}

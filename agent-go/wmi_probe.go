package main

import (
	"errors"
	"runtime"
	"strings"
)

// wmiProbePrefix is the lab diagnostic command line: the Go agent runs two
// read-only WMI connections under the active make_token identity and a
// third through the explicit credential plumbing, reporting the thread
// token's authentication LUID and every COM HRESULT. It writes nothing to
// the target, never touches credentials in its output, and exists to
// diagnose the one-shot second-connect failure without deploying anything.
const wmiProbePrefix = "__WMI_PROBE__"

// parseWmiProbeCommand extracts the target host, written exactly as the
// lateral deployment commands accept it: an IP literal or a DNS name. Any
// other command family is rejected so the parser never matches by accident.
func parseWmiProbeCommand(command string) (string, error) {
	trimmed := strings.TrimSpace(command)
	if !strings.HasPrefix(trimmed, wmiProbePrefix) {
		return "", errors.New("not a wmi probe command")
	}
	host := strings.TrimSpace(strings.TrimPrefix(trimmed, wmiProbePrefix))
	if host == "" {
		return "", errors.New("wmi probe requires a target host: __WMI_PROBE__ <host>")
	}
	if strings.ContainsAny(host, " \t\r\n\\/") {
		return "", errors.New("the wmi probe target must be a single host name")
	}
	return host, nil
}

// wmiProbeCommand executes the diagnostic. Only the Windows build can probe;
// like the lateral commands, other builds fail explicitly instead of
// silently degrading.
func wmiProbeCommand(command string) (string, error) {
	host, err := parseWmiProbeCommand(command)
	if err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		return "", errors.New("wmi probe requires the Windows agent build")
	}
	return wmiProbeExecute(host)
}

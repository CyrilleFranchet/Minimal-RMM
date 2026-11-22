//go:build !windows

package main

import "errors"

// wmiProbeExecute never runs off Windows; the command layer already gates
// on GOOS, so the stub only satisfies the linker with the same failure the
// lateral commands report on non-Windows agents.
func wmiProbeExecute(host, mode string, bIdentity *lateralIdentity) (string, error) {
	_ = host
	_, _ = mode, bIdentity
	return "", errors.New("wmi probe requires the Windows agent build")
}

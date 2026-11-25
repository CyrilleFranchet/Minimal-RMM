//go:build !windows

package main

// getuidxPlatform is the non-Windows stub — returns process identity only.
func getuidxPlatform() (string, error) {
	return getuidxLabel(processUserIdentity(), "", false), nil
}

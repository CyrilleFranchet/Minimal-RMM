//go:build !windows

package main

import "fmt"

// whoamiAction rejects unsupported host-side development platforms.
func whoamiAction(tickets bool) (string, error) {
	return "", fmt.Errorf("whoami requires a Windows Go agent")
}

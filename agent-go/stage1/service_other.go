//go:build !windows

package main

// Non-Windows builds exist only for host-side development tests; stage1 is
// never deployed outside Windows targets.
func runStageAsService(work func() error) bool {
	return false
}

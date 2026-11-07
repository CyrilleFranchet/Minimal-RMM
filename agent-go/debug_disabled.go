//go:build !rmmdebug

package main

// Normal agents compile out diagnostic initialization, file handling, and checkpoints.
func initAgentDebug(bool)      {}
func closeAgentDebug()         {}
func debugEvent(string, error) {}

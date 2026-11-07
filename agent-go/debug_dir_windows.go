//go:build rmmdebug && windows

package main

import (
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

// debugDirectory resolves the process account's profile, avoiding inherited parent TEMP paths.
func debugDirectory() string {
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		return os.TempDir()
	}
	defer token.Close()
	getProfile := syscall.NewLazyDLL("userenv.dll").NewProc("GetUserProfileDirectoryW")
	var size uint32
	getProfile.Call(uintptr(token), 0, uintptr(unsafe.Pointer(&size)))
	if size == 0 || size > 32768 {
		return os.TempDir()
	}
	profile := make([]uint16, size)
	ok, _, _ := getProfile.Call(uintptr(token), uintptr(unsafe.Pointer(&profile[0])), uintptr(unsafe.Pointer(&size)))
	if ok == 0 {
		return os.TempDir()
	}
	path := filepath.Join(syscall.UTF16ToString(profile), "AppData", "Local", "Temp")
	if err := os.MkdirAll(path, 0700); err != nil {
		return os.TempDir()
	}
	return path
}

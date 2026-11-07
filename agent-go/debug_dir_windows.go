//go:build rmmdebug && windows

package main

import (
	"path/filepath"
	"syscall"
	"unsafe"
)

// debugDirectory uses the Windows installation's shared Temp directory.
func debugDirectory() string {
	buffer := make([]uint16, 32768)
	getWindowsDirectory := syscall.NewLazyDLL("kernel32.dll").NewProc("GetSystemWindowsDirectoryW")
	length, _, _ := getWindowsDirectory.Call(uintptr(unsafe.Pointer(&buffer[0])), uintptr(len(buffer)))
	if length == 0 || length >= uintptr(len(buffer)) {
		return `C:\Windows\Temp`
	}
	return filepath.Join(syscall.UTF16ToString(buffer[:length]), "Temp")
}

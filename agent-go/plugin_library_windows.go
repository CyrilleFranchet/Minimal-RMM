//go:build windows

package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

var (
	procLoadLibraryExW = kernel32Mod.NewProc("LoadLibraryExW")
	procLocalFree      = kernel32Mod.NewProc("LocalFree")
	procConvertSDDL    = syscall.NewLazyDLL("advapi32.dll").NewProc("ConvertStringSecurityDescriptorToSecurityDescriptorW")
)

// stageWindowsPlugin keeps the DLL immutable until the child exits. The parent
// owns cleanup so a native crash or context cancellation also removes the file.
func stageWindowsPlugin(data []byte) (string, func() error, error) {
	dir, err := createPluginDirectory()
	if err != nil {
		return "", nil, err
	}
	path := filepath.Join(dir, "plugin.dll")
	if err := os.WriteFile(path, data, 0600); err != nil {
		_ = os.RemoveAll(dir)
		return "", nil, fmt.Errorf("stage plugin: %w", err)
	}
	pathW, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		_ = os.RemoveAll(dir)
		return "", nil, err
	}
	handle, err := syscall.CreateFile(pathW, syscall.GENERIC_READ, syscall.FILE_SHARE_READ, nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		_ = os.RemoveAll(dir)
		return "", nil, fmt.Errorf("protect staged plugin: %w", err)
	}
	cleanup := func() error {
		closeErr := syscall.CloseHandle(handle)
		removeErr := os.RemoveAll(dir)
		if err := errors.Join(closeErr, removeErr); err != nil {
			return fmt.Errorf("plugin cleanup failed: %w", err)
		}
		return nil
	}
	return path, cleanup, nil
}

// createPluginDirectory atomically creates a directory whose protected DACL
// grants access only to the process account and SYSTEM, inherited by its files.
func createPluginDirectory() (string, error) {
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		return "", err
	}
	defer token.Close()
	user, err := token.GetTokenUser()
	if err != nil {
		return "", err
	}
	sid, err := user.User.Sid.String()
	if err != nil {
		return "", err
	}
	sddl, err := syscall.UTF16PtrFromString("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid + ")")
	if err != nil {
		return "", err
	}
	var descriptor uintptr
	ok, _, callErr := procConvertSDDL.Call(uintptr(unsafe.Pointer(sddl)), 1, uintptr(unsafe.Pointer(&descriptor)), 0)
	if ok == 0 {
		return "", fmt.Errorf("create plugin directory permissions: %w", callErr)
	}
	defer procLocalFree.Call(descriptor)
	attributes := syscall.SecurityAttributes{Length: uint32(unsafe.Sizeof(syscall.SecurityAttributes{})), SecurityDescriptor: descriptor}
	root, err := filepath.Abs(os.TempDir())
	if err != nil {
		return "", err
	}
	for attempt := 0; attempt < 3; attempt++ {
		var nonce [16]byte
		if _, err := rand.Read(nonce[:]); err != nil {
			return "", err
		}
		path := filepath.Join(root, "rmm-plugin-"+hex.EncodeToString(nonce[:]))
		pathW, err := syscall.UTF16PtrFromString(path)
		if err != nil {
			return "", err
		}
		if err := syscall.CreateDirectory(pathW, &attributes); err != nil {
			if errors.Is(err, syscall.ERROR_ALREADY_EXISTS) {
				continue
			}
			return "", fmt.Errorf("create plugin directory: %w", err)
		}
		return path, nil
	}
	return "", errors.New("could not allocate a unique plugin directory")
}

// runWindowsPluginChild loads a staged DLL with Windows, retaining it until
// process exit because its embedded Go runtime can own background threads.
func runWindowsPluginChild(args []string) error {
	if len(args) != 3 || !pluginExportPattern.MatchString(args[1]) || !filepath.IsAbs(args[2]) {
		return errors.New("usage: --plugin-library-child <export> <absolute DLL path>")
	}
	input, err := io.ReadAll(io.LimitReader(os.Stdin, 4097))
	if err != nil || len(input) > 4096 {
		return errors.New("invalid plugin input")
	}
	path, err := syscall.UTF16PtrFromString(args[2])
	if err != nil {
		return err
	}
	const loadLibrarySearchSystem32 = 0x800
	module, _, callErr := procLoadLibraryExW.Call(uintptr(unsafe.Pointer(path)), 0, loadLibrarySearchSystem32)
	if module == 0 {
		return fmt.Errorf("Windows plugin load failed: %w", callErr)
	}
	address, err := syscall.GetProcAddress(syscall.Handle(module), args[1])
	if err != nil {
		return fmt.Errorf("plugin export unavailable: %w", err)
	}
	result, err := invokePluginExport(address, args[1], string(input))
	if err != nil {
		return err
	}
	_, err = os.Stdout.WriteString(result)
	return err
}

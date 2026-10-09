// Screenshot plugin for the Minimal-RMM Go agent.
// Authorized lab use only.
package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"bytes"
	"encoding/base64"
	"errors"
	"image"
	"image/png"
	"syscall"
	"unsafe"
)

var (
	user32             = syscall.NewLazyDLL("user32.dll")
	gdi32              = syscall.NewLazyDLL("gdi32.dll")
	getSystemMetrics   = user32.NewProc("GetSystemMetrics")
	getDC              = user32.NewProc("GetDC")
	releaseDC          = user32.NewProc("ReleaseDC")
	createCompatibleDC = gdi32.NewProc("CreateCompatibleDC")
	createDIBSection   = gdi32.NewProc("CreateDIBSection")
	selectObject       = gdi32.NewProc("SelectObject")
	bitBlt             = gdi32.NewProc("BitBlt")
	deleteObject       = gdi32.NewProc("DeleteObject")
	deleteDC           = gdi32.NewProc("DeleteDC")
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

// Run captures the primary Windows display and writes a base64 PNG result.
// The input is intentionally ignored; the operator contract is parameterless.
//
//export Run
func Run(_ *C.char, output *C.char, outputCap C.int) C.int {
	result, err := capture()
	if err != nil {
		writeOutput(output, outputCap, []byte(`{"success":false,"error":"screenshot failed"}`))
		return 1
	}
	writeOutput(output, outputCap, []byte(result))
	return 0
}

func main() {}

func capture() (string, error) {
	widthValue, _, _ := getSystemMetrics.Call(smCXScreen)
	heightValue, _, _ := getSystemMetrics.Call(smCYScreen)
	width, height := int(widthValue), int(heightValue)
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
		Size: uint32(unsafe.Sizeof(bitmapInfoHeader{})), Width: int32(width),
		Height: -int32(height), Planes: 1, BitCount: 32, Compression: biRGB,
	}}
	var bits uintptr
	bitmap, _, _ := createDIBSection.Call(screenDC, uintptr(unsafe.Pointer(&info)), dibRGBColors, uintptr(unsafe.Pointer(&bits)), 0, 0)
	if bitmap == 0 || bits == 0 {
		return "", errors.New("CreateDIBSection failed")
	}
	defer deleteObject.Call(bitmap)
	previous, _, _ := selectObject.Call(memoryDC, bitmap)
	if previous == 0 {
		return "", errors.New("SelectObject failed")
	}
	if result, _, _ := bitBlt.Call(memoryDC, 0, 0, uintptr(width), uintptr(height), screenDC, 0, 0, srccopy|captureBlt); result == 0 {
		return "", errors.New("BitBlt failed")
	}
	selectObject.Call(memoryDC, previous)
	pixels := make([]byte, width*height*4)
	copy(pixels, unsafe.Slice((*byte)(unsafe.Pointer(bits)), len(pixels)))
	rgba := image.NewRGBA(image.Rect(0, 0, width, height))
	for i := 0; i < len(pixels); i += 4 {
		rgba.Pix[i], rgba.Pix[i+1], rgba.Pix[i+2], rgba.Pix[i+3] = pixels[i+2], pixels[i+1], pixels[i], 0xff
	}
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, rgba); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(encoded.Bytes()), nil
}

func writeOutput(output *C.char, cap C.int, data []byte) {
	if output == nil || cap <= 1 {
		return
	}
	limit := int(cap) - 1
	if len(data) > limit {
		data = data[:limit]
	}
	copy(unsafe.Slice((*byte)(unsafe.Pointer(output)), len(data)), data)
	*(*byte)(unsafe.Pointer(uintptr(unsafe.Pointer(output)) + uintptr(len(data)))) = 0
}

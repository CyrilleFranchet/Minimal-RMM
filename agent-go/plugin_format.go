package main

import (
	"bytes"
	"debug/pe"
	"encoding/binary"
	"errors"
	"fmt"
)

const windowsLoaderMarker = "RMMRequiresWindowsLoader"

// pluginRequiresWindowsLoader reads the opt-in export without running DLL code.
func pluginRequiresWindowsLoader(data []byte) (bool, error) {
	file, err := pe.NewFile(bytes.NewReader(data))
	if err != nil {
		return false, fmt.Errorf("invalid plugin PE: %w", err)
	}
	defer file.Close()
	opt, ok := file.OptionalHeader.(*pe.OptionalHeader64)
	if !ok || file.Machine != pe.IMAGE_FILE_MACHINE_AMD64 || file.Characteristics&pe.IMAGE_FILE_DLL == 0 {
		return false, errors.New("plugin must be a PE32+ amd64 DLL")
	}
	if opt.NumberOfRvaAndSizes < 1 || opt.DataDirectory[0].VirtualAddress == 0 {
		return false, nil
	}
	dir := opt.DataDirectory[0]
	if dir.Size < 40 {
		return false, errors.New("plugin export directory is truncated")
	}
	exports, err := pluginRVAData(file, data, dir.VirtualAddress)
	if err != nil || len(exports) < 40 {
		return false, errors.New("plugin export directory is outside the file")
	}
	count := binary.LittleEndian.Uint32(exports[24:28])
	if count == 0 {
		return false, nil
	}
	if count > 65536 {
		return false, errors.New("plugin export name count exceeds 65536")
	}
	names, err := pluginRVAData(file, data, binary.LittleEndian.Uint32(exports[32:36]))
	if err != nil || uint64(count)*4 > uint64(len(names)) {
		return false, errors.New("plugin export names are outside the file")
	}
	for i := uint32(0); i < count; i++ {
		name, err := pluginRVAData(file, data, binary.LittleEndian.Uint32(names[i*4:i*4+4]))
		if err != nil {
			return false, err
		}
		if len(name) >= len(windowsLoaderMarker)+1 &&
			string(name[:len(windowsLoaderMarker)]) == windowsLoaderMarker && name[len(windowsLoaderMarker)] == 0 {
			return true, nil
		}
	}
	return false, nil
}

// pluginRVAData returns file-backed bytes at an RVA, bounded by its section.
func pluginRVAData(file *pe.File, data []byte, rva uint32) ([]byte, error) {
	for _, section := range file.Sections {
		if uint64(rva) < uint64(section.VirtualAddress) || uint64(rva)-uint64(section.VirtualAddress) >= uint64(section.Size) {
			continue
		}
		offset := uint64(section.Offset) + uint64(rva) - uint64(section.VirtualAddress)
		end := uint64(section.Offset) + uint64(section.Size)
		if offset >= end || end > uint64(len(data)) {
			break
		}
		return data[offset:end], nil
	}
	return nil, errors.New("plugin RVA is outside file-backed sections")
}

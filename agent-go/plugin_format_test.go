package main

import (
	"bytes"
	"debug/pe"
	"encoding/binary"
	"testing"
)

// pluginMarkerFixture builds PE metadata only; it contains no executable code.
func pluginMarkerFixture(t testing.TB, name string) []byte {
	t.Helper()
	data := make([]byte, 1024)
	copy(data, "MZ")
	binary.LittleEndian.PutUint32(data[0x3c:], 128)
	copy(data[128:], "PE\x00\x00")
	var headers bytes.Buffer
	file := pe.FileHeader{Machine: pe.IMAGE_FILE_MACHINE_AMD64, NumberOfSections: 1, SizeOfOptionalHeader: 240, Characteristics: pe.IMAGE_FILE_DLL}
	opt := pe.OptionalHeader64{Magic: 0x20b, SizeOfImage: 8192, SizeOfHeaders: 512, NumberOfRvaAndSizes: 16}
	opt.DataDirectory[0] = pe.DataDirectory{VirtualAddress: 4096, Size: 40}
	section := pe.SectionHeader32{VirtualSize: 512, VirtualAddress: 4096, SizeOfRawData: 512, PointerToRawData: 512}
	copy(section.Name[:], ".edata")
	for _, value := range []any{file, opt, section} {
		if err := binary.Write(&headers, binary.LittleEndian, value); err != nil {
			t.Fatal(err)
		}
	}
	copy(data[132:], headers.Bytes())
	binary.LittleEndian.PutUint32(data[512+24:], 1)
	binary.LittleEndian.PutUint32(data[512+32:], 4096+40)
	binary.LittleEndian.PutUint32(data[512+40:], 4096+48)
	copy(data[512+48:], name)
	return data
}

func TestPluginLoaderMarker(t *testing.T) {
	for _, name := range []string{windowsLoaderMarker, "Run", windowsLoaderMarker + "Extra"} {
		t.Run(name, func(t *testing.T) {
			marked, err := pluginRequiresWindowsLoader(pluginMarkerFixture(t, name))
			if err != nil || marked != (name == windowsLoaderMarker) {
				t.Fatalf("marked=%v error=%v", marked, err)
			}
		})
	}
	for _, tc := range []struct {
		name    string
		corrupt func([]byte) []byte
	}{
		{"truncated", func(b []byte) []byte { return b[:600] }},
		{"name_count", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[536:], 0xffffffff); return b }},
		{"names_rva", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[544:], 0xffffffff); return b }},
		{"name_rva", func(b []byte) []byte { binary.LittleEndian.PutUint32(b[552:], 0xffffffff); return b }},
		{"not_dll", func(b []byte) []byte { binary.LittleEndian.PutUint16(b[150:], 0); return b }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := pluginRequiresWindowsLoader(tc.corrupt(pluginMarkerFixture(t, windowsLoaderMarker))); err == nil {
				t.Fatal("accepted corrupt PE metadata")
			}
		})
	}
}

func FuzzPluginLoaderMarker(f *testing.F) {
	f.Add(pluginMarkerFixture(f, windowsLoaderMarker))
	f.Add([]byte("not a DLL"))
	f.Fuzz(func(t *testing.T, data []byte) { _, _ = pluginRequiresWindowsLoader(data) })
}

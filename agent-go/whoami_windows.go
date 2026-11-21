//go:build windows

package main

import (
	"encoding/binary"
	"fmt"
	"os"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var whoamiSecur32 = syscall.NewLazyDLL("secur32.dll")
var whoamiKernel32 = syscall.NewLazyDLL("kernel32.dll")

// whoamiUnicode mirrors the pointer-aligned LSA_UNICODE_STRING.
type whoamiUnicode struct {
	Length, MaximumLength uint16
	Buffer                *uint16
}

// whoamiLogonData mirrors SECURITY_LOGON_SESSION_DATA through its UPN field.
type whoamiLogonData struct {
	Size                   uint32
	LogonID                [2]uint32
	User, Domain, Package  whoamiUnicode
	LogonType, Session     uint32
	SID                    *syscall.SID
	LogonTime              int64
	Server, DNSDomain, UPN whoamiUnicode
}

// whoamiTicket mirrors KERB_TICKET_CACHE_INFO_EX (metadata only).
type whoamiTicket struct {
	Client, ClientRealm, Server, ServerRealm whoamiUnicode
	Start, End, Renew                        int64
	Encryption                               int32
	Flags                                    uint32
}

// whoamiText copies a bounded counted Unicode string owned by LSA.
func whoamiText(s whoamiUnicode) string {
	if s.Length == 0 {
		return ""
	}
	if s.Buffer == nil || uintptr(unsafe.Pointer(s.Buffer))%2 != 0 || s.Length%2 != 0 || s.Length > s.MaximumLength {
		return "<invalid Unicode string>"
	}
	return syscall.UTF16ToString(unsafe.Slice(s.Buffer, int(s.Length/2)))
}

// whoamiTime formats Windows UTC FILETIME values without overflowing nanoseconds.
func whoamiTime(value int64) string {
	if value == 0 {
		return "not supplied"
	}
	return time.Unix(value/10000000-11644473600, value%10000000*100).UTC().Format(time.RFC3339)
}

// whoamiTokenInfo reads a bounded token information buffer with growth retries.
func whoamiTokenInfo(token syscall.Token, class uint32) ([]byte, error) {
	proc := advapi32.NewProc("GetTokenInformation")
	return whoamiQueryTokenInfo(class, func(buf []byte, n *uint32) (bool, error) {
		var ptr unsafe.Pointer
		if len(buf) > 0 {
			ptr = unsafe.Pointer(&buf[0])
		}
		ok, _, err := proc.Call(uintptr(token), uintptr(class), uintptr(ptr), uintptr(len(buf)), uintptr(unsafe.Pointer(n)))
		return ok != 0, err
	})
}

// whoamiQueryTokenInfo preserves API errors and retries bounded buffer growth.
func whoamiQueryTokenInfo(class uint32, query func([]byte, *uint32) (bool, error)) ([]byte, error) {
	var size uint32
	ok, err := query(nil, &size)
	if !ok && err != syscall.ERROR_INSUFFICIENT_BUFFER {
		return nil, fmt.Errorf("token information class %d: %w", class, err)
	}
	for attempt := 0; attempt < 4; attempt++ {
		if size == 0 || size > 1<<20 {
			return nil, fmt.Errorf("token information class %d: invalid buffer size %d", class, size)
		}
		if size < 4 {
			size = 4
		} // BOOLEAN results still receive DWORD-sized, zeroed storage.
		buf := make([]byte, int(size))
		returned := size
		ok, err = query(buf, &returned)
		if ok {
			if returned == 0 || returned > uint32(len(buf)) {
				return nil, fmt.Errorf("token information class %d: invalid returned size %d (capacity %d)", class, returned, len(buf))
			}
			return buf[:returned], nil
		}
		if err != syscall.ERROR_INSUFFICIENT_BUFFER {
			return nil, fmt.Errorf("token information class %d: %w", class, err)
		}
		if returned <= size {
			return nil, fmt.Errorf("token information class %d: non-growing buffer requirement: %w", class, err)
		}
		size = returned
	}
	return nil, fmt.Errorf("token information class %d: buffer kept growing: %w", class, syscall.ERROR_INSUFFICIENT_BUFFER)
}

// whoamiBufferSID validates a SID pointer and all SID bytes inside a token buffer.
func whoamiBufferSID(buf []byte, offset int) string {
	width := int(unsafe.Sizeof(uintptr(0)))
	if offset < 0 || len(buf) < width || offset > len(buf)-width {
		return "unavailable: short SID pointer buffer"
	}
	sid := *(**syscall.SID)(unsafe.Pointer(&buf[offset]))
	if sid == nil {
		return "not supplied"
	}
	base, ptr := uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(sid))
	if ptr < base || ptr-base > uintptr(len(buf)) || uintptr(len(buf))-(ptr-base) < 8 {
		return "unavailable: SID outside token buffer"
	}
	at := int(ptr - base)
	if buf[at] != 1 || buf[at+1] > 15 || len(buf)-at < 8+4*int(buf[at+1]) {
		return "unavailable: invalid SID buffer"
	}
	text := whoamiSID(sid)
	runtime.KeepAlive(buf)
	return text
}

// whoamiTextWithin validates an LSA string lies wholly inside its response.
func whoamiTextWithin(s whoamiUnicode, response unsafe.Pointer, size uint32) string {
	if s.Length == 0 {
		return ""
	}
	if s.Buffer == nil || s.Length%2 != 0 || s.Length > s.MaximumLength {
		return "<invalid Unicode string>"
	}
	base, ptr := uintptr(response), uintptr(unsafe.Pointer(s.Buffer))
	if ptr%2 != 0 || ptr < base || ptr-base > uintptr(size) || uintptr(s.Length) > uintptr(size)-(ptr-base) {
		return "<Unicode string outside response>"
	}
	return whoamiText(s)
}

// whoamiSID reports the stable SID and a best-effort account name.
func whoamiSID(sid *syscall.SID) string {
	if sid == nil {
		return "not supplied"
	}
	text, err := sid.String()
	if err != nil {
		return fmt.Sprintf("SID unavailable: %v", err)
	}
	name, domain, _, err := sid.LookupAccount("")
	if err == nil {
		return fmt.Sprintf("%s\\%s (%s)", domain, name, text)
	}
	return text
}

// whoamiGroups renders token groups with their effective security attributes.
func whoamiGroups(out *strings.Builder, token syscall.Token, class uint32, label string) {
	buf, err := whoamiTokenInfo(token, class)
	if err != nil {
		fmt.Fprintf(out, "%s: unavailable: %v\n", label, err)
		return
	}
	type group struct {
		SID        *syscall.SID
		Attributes uint32
	}
	var layout struct {
		Count uint32
		First group
	}
	offset, stride := int(unsafe.Offsetof(layout.First)), int(unsafe.Sizeof(group{}))
	if len(buf) < 4 {
		fmt.Fprintf(out, "%s: unavailable: short group buffer\n", label)
		return
	}
	count := uint64(binary.LittleEndian.Uint32(buf))
	fmt.Fprintf(out, "%s (%d):\n", label, count)
	if count > 0 && (len(buf) < offset || count > uint64((len(buf)-offset)/stride)) {
		fmt.Fprintln(out, "  invalid group buffer")
		return
	}
	for i := 0; i < int(count); i++ {
		entry := (*group)(unsafe.Pointer(&buf[offset+i*stride]))
		a := entry.Attributes
		fmt.Fprintf(out, "  %s enabled=%t default=%t deny-only=%t integrity=%t logon-id=%t attributes=0x%08x\n", whoamiBufferSID(buf, offset+i*stride), a&4 != 0, a&2 != 0, a&16 != 0, a&32 != 0, a&0xc0000000 == 0xc0000000, a)
	}
	runtime.KeepAlive(buf)
}

// whoamiPrivileges compares the complete privilege catalog against the token.
func whoamiPrivileges(out *strings.Builder, token syscall.Token) {
	buf, err := whoamiTokenInfo(token, 3)
	if err != nil {
		fmt.Fprintf(out, "Privileges: unavailable: %v\n", err)
		return
	}
	if len(buf) < 4 {
		fmt.Fprintln(out, "Privileges: unavailable: short buffer")
		return
	}
	count := uint64(binary.LittleEndian.Uint32(buf))
	if count > uint64((len(buf)-4)/12) {
		fmt.Fprintln(out, "Privileges: invalid buffer")
		return
	}
	held := map[uint64]uint32{}
	for i := 0; i < int(count); i++ {
		off := 4 + i*12
		held[binary.LittleEndian.Uint64(buf[off:])] = binary.LittleEndian.Uint32(buf[off+8:])
	}
	fmt.Fprintln(out, "Privileges (absent differs from disabled; descriptions use the Windows display language):")
	for _, name := range whoamiPrivilegeCatalog {
		wide, _ := syscall.UTF16PtrFromString(name)
		var luid uint64
		ok, _, lookupErr := advapi32.NewProc("LookupPrivilegeValueW").Call(0, uintptr(unsafe.Pointer(wide)), uintptr(unsafe.Pointer(&luid)))
		if ok == 0 {
			state := "unavailable"
			if lookupErr == syscall.Errno(1313) {
				state = "unsupported on this OS"
			}
			fmt.Fprintf(out, "  %s: %s (%v)\n", name, state, lookupErr)
			continue
		}
		a, present := held[luid]
		description := "description unavailable"
		var size, language uint32
		advapi32.NewProc("LookupPrivilegeDisplayNameW").Call(0, uintptr(unsafe.Pointer(wide)), 0, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&language)))
		if size > 0 && size < 8192 {
			text := make([]uint16, size+1)
			size++
			ok, _, _ = advapi32.NewProc("LookupPrivilegeDisplayNameW").Call(0, uintptr(unsafe.Pointer(wide)), uintptr(unsafe.Pointer(&text[0])), uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&language)))
			if ok != 0 {
				description = syscall.UTF16ToString(text)
			}
		}
		fmt.Fprintf(out, "  %s: %s — %s\n", name, whoamiPrivilegeState(a, present), description)
	}
}

// whoamiLogon reports metadata for the token's authentication LUID.
func whoamiLogon(out *strings.Builder, luid uint64) {
	var data *whoamiLogonData
	status, _, _ := whoamiSecur32.NewProc("LsaGetLogonSessionData").Call(uintptr(unsafe.Pointer(&luid)), uintptr(unsafe.Pointer(&data)))
	if status != 0 {
		fmt.Fprintf(out, "Logon session: unavailable (NTSTATUS 0x%08x)\n", uint32(status))
		return
	}
	if data == nil {
		fmt.Fprintln(out, "Logon session: not supplied")
		return
	}
	defer whoamiSecur32.NewProc("LsaFreeReturnBuffer").Call(uintptr(unsafe.Pointer(data)))
	if uintptr(data.Size) < unsafe.Sizeof(*data) {
		fmt.Fprintln(out, "Logon session: incomplete structure")
		return
	}
	types := map[uint32]string{0: "Undefined", 2: "Interactive", 3: "Network", 4: "Batch", 5: "Service", 7: "Unlock", 8: "NetworkCleartext", 9: "NewCredentials", 10: "RemoteInteractive", 11: "CachedInteractive", 12: "CachedRemoteInteractive", 13: "CachedUnlock"}
	fmt.Fprintf(out, "Logon session: LUID=0x%016x type=%d (%s) Windows-session=%d\n  account=%s\\%s SID=%s\n  authentication-package=%s logon-time=%s\n  logon-server=%s DNS-domain=%s UPN=%s\n", luid, data.LogonType, types[data.LogonType], data.Session, whoamiText(data.Domain), whoamiText(data.User), whoamiSID(data.SID), whoamiText(data.Package), whoamiTime(data.LogonTime), whoamiText(data.Server), whoamiText(data.DNSDomain), whoamiText(data.UPN))
}

// whoamiTickets queries only the caller's effective Kerberos cache metadata.
func whoamiTickets(out *strings.Builder) {
	var handle uintptr
	status, _, _ := whoamiSecur32.NewProc("LsaConnectUntrusted").Call(uintptr(unsafe.Pointer(&handle)))
	if status != 0 {
		fmt.Fprintf(out, "Kerberos tickets: LsaConnectUntrusted NTSTATUS 0x%08x\n", uint32(status))
		return
	}
	defer whoamiSecur32.NewProc("LsaDeregisterLogonProcess").Call(handle)
	name := append([]byte("Kerberos"), 0)
	type ansiString struct {
		Length, MaximumLength uint16
		Buffer                *byte
	}
	pkgName := ansiString{8, 9, &name[0]}
	var pkg uint32
	status, _, _ = whoamiSecur32.NewProc("LsaLookupAuthenticationPackage").Call(handle, uintptr(unsafe.Pointer(&pkgName)), uintptr(unsafe.Pointer(&pkg)))
	if status != 0 {
		fmt.Fprintf(out, "Kerberos tickets: package lookup NTSTATUS 0x%08x\n", uint32(status))
		return
	}
	// Zero LUID selects the effective caller cache without SeTcbPrivilege.
	request := [3]uint32{14, 0, 0} // KerbQueryTicketCacheExMessage
	var response unsafe.Pointer
	var size, protocol uint32
	status, _, _ = whoamiSecur32.NewProc("LsaCallAuthenticationPackage").Call(handle, uintptr(pkg), uintptr(unsafe.Pointer(&request)), unsafe.Sizeof(request), uintptr(unsafe.Pointer(&response)), uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&protocol)))
	if response != nil {
		defer whoamiSecur32.NewProc("LsaFreeReturnBuffer").Call(uintptr(response))
	}
	if status != 0 || protocol != 0 {
		fmt.Fprintf(out, "Kerberos tickets: unavailable (NTSTATUS=0x%08x package=0x%08x)\n", uint32(status), protocol)
		return
	}
	if response == nil || size < 8 || size > 16<<20 {
		fmt.Fprintln(out, "Kerberos tickets: invalid response")
		return
	}
	raw := unsafe.Slice((*byte)(response), int(size))
	count := binary.LittleEndian.Uint32(raw[4:])
	stride := uint32(unsafe.Sizeof(whoamiTicket{}))
	if count > (size-8)/stride {
		fmt.Fprintln(out, "Kerberos tickets: invalid entry count")
		return
	}
	fmt.Fprintf(out, "Kerberos tickets (effective caller cache; metadata only): %d\n", count)
	for i := uint32(0); i < count; i++ {
		t := (*whoamiTicket)(unsafe.Add(response, uintptr(8+i*stride)))
		fmt.Fprintf(out, "  %s@%s -> %s@%s encryption=%d flags=0x%08x\n    start=%s end=%s renew-until=%s\n", whoamiTextWithin(t.Client, response, size), whoamiTextWithin(t.ClientRealm, response, size), whoamiTextWithin(t.Server, response, size), whoamiTextWithin(t.ServerRealm, response, size), t.Encryption, t.Flags, whoamiTime(t.Start), whoamiTime(t.End), whoamiTime(t.Renew))
	}
}

// whoamiDescribeToken emits independent token fields so errors remain explicit.
func whoamiDescribeToken(out *strings.Builder, label string, token syscall.Token) {
	fmt.Fprintf(out, "\n%s\n", label)
	if buf, err := whoamiTokenInfo(token, 1); err == nil {
		fmt.Fprintf(out, "User: %s\n", whoamiBufferSID(buf, 0))
	} else {
		fmt.Fprintf(out, "User: unavailable: %v\n", err)
	}
	for _, field := range []struct {
		class  uint32
		name   string
		values map[uint32]string
	}{
		{8, "Token type", map[uint32]string{1: "primary", 2: "impersonation"}},
		{12, "Windows session ID", nil}, {18, "Elevation type", map[uint32]string{1: "default", 2: "full", 3: "limited"}},
		{19, "Has linked token", nil}, {20, "Elevated", nil}, {21, "Has restrictions", nil}, {23, "UAC virtualization allowed", nil}, {24, "UAC virtualization enabled", nil}, {26, "UIAccess", nil}, {29, "AppContainer", nil},
	} {
		// Linked-token queries allocate a handle; close it without inspecting the linked identity.
		b, err := whoamiTokenInfo(token, field.class)
		if err != nil {
			fmt.Fprintf(out, "%s: unavailable: %v\n", field.name, err)
			continue
		}
		if field.class == 19 {
			if len(b) < int(unsafe.Sizeof(syscall.Handle(0))) {
				fmt.Fprintln(out, "Has linked token: unavailable: short handle buffer")
				continue
			}
			linked := *(*syscall.Handle)(unsafe.Pointer(&b[0]))
			fmt.Fprintf(out, "Has linked token: %t\n", linked != 0)
			if linked != 0 {
				syscall.CloseHandle(linked)
			}
			continue
		}
		value, scalarErr := whoamiScalar(field.class, b)
		if scalarErr != nil {
			fmt.Fprintf(out, "%s: unavailable: %v\n", field.name, scalarErr)
			continue
		}
		fmt.Fprintf(out, "%s: %d %s\n", field.name, value, field.values[value])
	}
	if b, err := whoamiTokenInfo(token, 25); err == nil {
		fmt.Fprintf(out, "Integrity: %s\n", whoamiBufferSID(b, 0))
		runtime.KeepAlive(b)
	} else {
		fmt.Fprintf(out, "Integrity: unavailable: %v\n", err)
	}
	if b, err := whoamiTokenInfo(token, 10); err == nil && len(b) >= 56 {
		luid := binary.LittleEndian.Uint64(b[8:])
		fmt.Fprintf(out, "Token ID: 0x%016x Authentication LUID: 0x%016x Modified ID: 0x%016x\n", binary.LittleEndian.Uint64(b), luid, binary.LittleEndian.Uint64(b[48:]))
		if binary.LittleEndian.Uint32(b[24:]) == 2 {
			level := binary.LittleEndian.Uint32(b[28:])
			names := []string{"anonymous", "identification", "impersonation", "delegation"}
			text := "unknown"
			if level < 4 {
				text = names[level]
			}
			fmt.Fprintf(out, "Impersonation level: %d (%s)\n", level, text)
		}
		whoamiLogon(out, luid)
	} else {
		if err == nil {
			fmt.Fprintln(out, "Token statistics: unavailable: short buffer")
		} else {
			fmt.Fprintf(out, "Token statistics: unavailable: %v\n", err)
		}
	}
	whoamiGroups(out, token, 2, "Groups")
	whoamiGroups(out, token, 11, "Restricted SIDs")
	whoamiPrivileges(out, token)
}

// whoamiAction inspects the process and actual current-thread token without changing either.
func whoamiAction(tickets bool) (string, error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	var out strings.Builder
	path, pathErr := os.Executable()
	fmt.Fprintf(&out, "Process: PID=%d PPID=%d architecture=%s executable=%s\n", os.Getpid(), os.Getppid(), runtime.GOARCH, path)
	if pathErr != nil {
		fmt.Fprintf(&out, "Executable path unavailable: %v\n", pathErr)
	}
	tid, _, _ := whoamiKernel32.NewProc("GetCurrentThreadId").Call()
	fmt.Fprintf(&out, "Inspected OS thread: %d\nConfigured make_token network identity: %s\n", tid, activeNetworkIdentity())
	fmt.Fprintln(&out, "Configured network identity does not prove valid credentials or the identity of other threads.")
	token, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		return "", fmt.Errorf("open process token: %w", err)
	}
	defer token.Close()
	whoamiDescribeToken(&out, "Process token", token)
	var thread syscall.Token
	ok, _, threadErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&thread)))
	if ok != 0 {
		defer thread.Close()
		whoamiDescribeToken(&out, "Effective current-thread token", thread)
	} else if threadErr == syscall.Errno(1008) {
		fmt.Fprintln(&out, "\nEffective current-thread token: none; effective identity uses the process token")
	} else {
		fmt.Fprintf(&out, "\nEffective current-thread token: unavailable: %v (no process fallback assumed)\n", threadErr)
	}
	if tickets {
		whoamiTickets(&out)
	}
	return out.String(), nil
}

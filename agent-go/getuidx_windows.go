//go:build windows

package main

import (
	"encoding/binary"
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

// getuidxPlatform inspects the effective thread token (or falls back to the
// process token) and reports: process SID, make_token network identity, the
// token's logon session type, the logon session identity, and whether local
// and network identities diverge (the type-9 split).
func getuidxPlatform() (string, error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	processUser := processUserIdentity()
	networkIdentity := activeNetworkIdentity()

	var out strings.Builder
	fmt.Fprintf(&out, "process identity: %s\n", processUser)
	if networkIdentity != "" {
		fmt.Fprintf(&out, "network identity: %s (make_token active)\n", networkIdentity)
	} else {
		fmt.Fprintln(&out, "network identity: none (commands use agent identity)")
	}

	// Try the thread token first (set by ImpersonateLoggedOnUser in make_token),
	// fall back to process token.
	var token syscall.Token
	threadToken := false
	ok, _, err := advapi32.NewProc("OpenThreadToken").Call(
		^uintptr(1), // GetCurrentThread pseudo-handle
		0x0008,      // TOKEN_QUERY
		1,           // OpenAsSelf
		uintptr(unsafe.Pointer(&token)),
	)
	if ok != 0 {
		threadToken = true
		defer token.Close()
	} else if err == syscall.Errno(1008) {
		// ERROR_NO_TOKEN — no thread token, use process token.
		processToken, openErr := syscall.OpenCurrentProcessToken()
		if openErr != nil {
			return out.String() + "logon session: unavailable (cannot open process token)\nidentity split: false\n", nil
		}
		token = processToken
		defer token.Close()
	} else {
		return out.String() + fmt.Sprintf("logon session: unavailable (%v)\nidentity split: false\n", err), nil
	}

	if threadToken {
		fmt.Fprintln(&out, "effective token: thread (impersonation)")
	} else {
		fmt.Fprintln(&out, "effective token: process")
	}

	// Get TokenStatistics for the authentication LUID and logon type.
	buf, err := whoamiTokenInfo(token, 10) // TokenStatistics
	if err != nil || len(buf) < 56 {
		return out.String() + "logon session: unavailable (cannot read token statistics)\nidentity split: false\n", nil
	}

	luid := binary.LittleEndian.Uint64(buf[8:])
	fmt.Fprintf(&out, "authentication LUID: 0x%016x\n", luid)

	// Query logon session data for the type and account.
	logonType, logonUser, logonDomain := getuidxLogonSession(luid)

	logonTypes := map[uint32]string{
		0: "Undefined", 2: "Interactive", 3: "Network", 4: "Batch",
		5: "Service", 7: "Unlock", 8: "NetworkCleartext",
		9: "NewCredentials", 10: "RemoteInteractive",
		11: "CachedInteractive", 12: "CachedRemoteInteractive",
	}
	typeName := logonTypes[logonType]
	if typeName == "" {
		typeName = "Unknown"
	}
	fmt.Fprintf(&out, "logon type: %d (%s)\n", logonType, typeName)

	if logonUser != "" {
		sessionIdentity := logonUser
		if logonDomain != "" {
			sessionIdentity = logonDomain + `\` + logonUser
		}
		fmt.Fprintf(&out, "logon session identity: %s\n", sessionIdentity)
	}

	// Determine identity split.
	// Type 9 (NewCredentials) = netonly: the token carries the caller's SIDs
	// but the logon session's network credentials are the alternate. Local
	// operations (file access, registry) use the process identity; network
	// operations (SMB, WinRM) use the alternate identity.
	split := networkIdentity != "" && logonType == 9
	if split {
		fmt.Fprintf(&out, "identity split: true\n")
		fmt.Fprintf(&out, "  WARNING: local commands run as %s, network traffic authenticates as %s\n", processUser, networkIdentity)
		fmt.Fprintln(&out, "  Spawned children from echelon 1/2 inherit this split behavior.")
		fmt.Fprintln(&out, "  Echelon 3 (CreateProcessWithLogonW) creates a full type-2 logon instead — creds cached in LSASS.")
	} else {
		fmt.Fprintln(&out, "identity split: false")
	}

	return out.String(), nil
}

// getuidxLogonSession queries LsaGetLogonSessionData for the logon type and account.
func getuidxLogonSession(luid uint64) (logonType uint32, user, domain string) {
	var data *whoamiLogonData
	status, _, _ := whoamiSecur32.NewProc("LsaGetLogonSessionData").Call(
		uintptr(unsafe.Pointer(&luid)),
		uintptr(unsafe.Pointer(&data)),
	)
	if status != 0 || data == nil {
		return 0, "", ""
	}
	defer whoamiSecur32.NewProc("LsaFreeReturnBuffer").Call(uintptr(unsafe.Pointer(data)))
	if uintptr(data.Size) < unsafe.Sizeof(*data) {
		return 0, "", ""
	}
	return data.LogonType, whoamiText(data.User), whoamiText(data.Domain)
}

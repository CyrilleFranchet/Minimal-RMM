//go:build windows

package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"reflect"
	"runtime"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

// wsmanFixtureArgs is the neutral argument set the ABI fixtures pass through
// the counted WSMAN_STRING_SET; the fixture asserts it is echoed unchanged.
var wsmanFixtureArgs = []string{"--lateral-session", "fixture-session"}

// The same session carries receiver startup, byte packets, and EOF. A held
// deployment keeps its handles open; send failures cannot report success and
// must tear the session down.
func TestWSManDeploymentTransferAndHold(t *testing.T) {
	payload := bytes.Repeat([]byte{0, 127, 128, 255}, winrmTransferChunkSize)
	digest := sha256.Sum256(payload)
	for _, scenario := range []string{"success", "send_failure", "send_timeout", "source_truncated", "source_grew"} {
		t.Run(scenario, func(t *testing.T) {
			defer stopHeldWinRMShells()
			f := &wsmanFixture{t: t, deployment: true}
			input := payload
			switch scenario {
			case "send_failure":
				f.fail = "WSManSendShellInput"
			case "send_timeout":
				f.timeout = "WSManSendShellInput"
			case "source_truncated":
				input = input[:len(input)-1]
			case "source_grew":
				input = append(append([]byte(nil), input...), 1)
			}
			transfer := &winrmTransfer{source: bytes.NewReader(input), size: int64(len(payload)), binarySize: int64(len(payload)), binaryDigest: fmt.Sprintf("%x", digest)}
			receiverArgs := []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", "Zml4dHVyZQ=="}
			err := winrmExecuteWithTLSAPI(f, "lab-host", winrmReceiverCommandPath, receiverArgs, time.Millisecond, nil, false, false, transfer, true)
			if (err == nil) != (scenario == "success") {
				t.Fatalf("unexpected deployment result: %v", err)
			}
			if scenario == "success" {
				if !bytes.Equal(f.sent, payload) || !f.sentEOF || f.commands != 1 {
					t.Fatal("binary transfer, EOF, or command count mismatch")
				}
				for _, name := range []string{"WSManCloseCommand", "WSManCloseShell", "WSManCloseSession", "WSManDeinitialize"} {
					if slices.Contains(f.calls, name) {
						t.Fatalf("held deployment closed %s", name)
					}
				}
				winrmHeldShells.Lock()
				held := len(winrmHeldShells.shells)
				winrmHeldShells.Unlock()
				if held != 1 {
					t.Fatal("successful transfer did not hold the shell open")
				}
			} else {
				winrmHeldShells.Lock()
				held := len(winrmHeldShells.shells)
				winrmHeldShells.Unlock()
				if held != 0 {
					t.Fatal("failed transfer held a shell open")
				}
				for _, name := range []string{"WSManCloseCommand", "WSManCloseSession", "WSManDeinitialize"} {
					if !slices.Contains(f.calls, name) {
						t.Fatalf("failed transfer skipped teardown step %s", name)
					}
				}
			}
			for _, handle := range f.sendOperations {
				closed := 0
				for _, candidate := range f.closedOperations {
					if candidate == handle {
						closed++
					}
				}
				if closed != 1 {
					t.Fatalf("input operation %d closed %d times", handle, closed)
				}
			}
			for _, context := range f.contexts {
				if _, ok := wsmanContexts.Load(context); ok {
					t.Fatal("deployment leaked native callback context")
				}
			}
		})
	}
}

// Held shells close in reverse order at shutdown, through the same native
// boundary the fixture asserts.
func TestWinRMHeldShellsCloseAtShutdown(t *testing.T) {
	f := &wsmanFixture{t: t, deployment: true}
	payload := bytes.Repeat([]byte{1, 2, 3}, 100)
	digest := sha256.Sum256(payload)
	transfer := &winrmTransfer{source: bytes.NewReader(payload), size: int64(len(payload)), binarySize: int64(len(payload)), binaryDigest: fmt.Sprintf("%x", digest)}
	receiverArgs := []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", "Zml4dHVyZQ=="}
	if err := winrmExecuteWithTLSAPI(f, "lab-host", winrmReceiverCommandPath, receiverArgs, time.Millisecond, nil, false, false, transfer, true); err != nil {
		t.Fatal(err)
	}
	stopHeldWinRMShells()
	want := []string{"WSManCloseCommand", "WSManCloseShell", "WSManCloseSession", "WSManDeinitialize"}
	tail := f.calls[len(f.calls)-len(want):]
	if !slices.Equal(tail, want) {
		t.Fatalf("held-shell teardown order: %v", f.calls)
	}
	for _, context := range f.contexts {
		if _, ok := wsmanContexts.Load(context); ok {
			t.Fatal("held-shell teardown leaked callback context")
		}
	}
}

func TestWSManSDKLayouts(t *testing.T) {
	// Independently checked against wsman.h by testdata/wsman_layout.c.
	checks := map[string][2]uintptr{
		"async size":     {unsafe.Sizeof(wsmanShellAsync{}), 16},
		"async callback": {unsafe.Offsetof(wsmanShellAsync{}.callback), 8},
		"set size":       {unsafe.Sizeof(wsmanStringSet{}), 16},
		"set values":     {unsafe.Offsetof(wsmanStringSet{}.values), 8},
		"data size":      {unsafe.Sizeof(wsmanData{}), 24},
		"data length":    {unsafe.Offsetof(wsmanData{}.length), 8},
		"data value":     {unsafe.Offsetof(wsmanData{}.value), 16},
		"receive size":   {unsafe.Sizeof(wsmanReceiveData{}), 48},
		"receive data":   {unsafe.Offsetof(wsmanReceiveData{}.streamData), 8},
		"receive state":  {unsafe.Offsetof(wsmanReceiveData{}.commandState), 32},
		"receive exit":   {unsafe.Offsetof(wsmanReceiveData{}.exitCode), 40},
		"auth size":      {unsafe.Sizeof(wsmanAuthCredentials{}), 24},
		"auth mechanism": {unsafe.Offsetof(wsmanAuthCredentials{}.mechanism), 0},
		"auth username":  {unsafe.Offsetof(wsmanAuthCredentials{}.username), 8},
		"auth password":  {unsafe.Offsetof(wsmanAuthCredentials{}.password), 16},
		"error size":     {unsafe.Sizeof(wsmanError{}), 40},
		"error detail":   {unsafe.Offsetof(wsmanError{}.detail), 8},
	}
	for name, pair := range checks {
		if pair[0] != pair[1] {
			t.Errorf("%s: got %d want %d", name, pair[0], pair[1])
		}
	}
}

// Invoke the actual Windows callback trampoline, including all seven slots.
func wsmanTestCallback(async *wsmanShellAsync, flags uintptr, code uint32, data *wsmanReceiveData) {
	fault := wsmanError{code: code}
	syscall.SyscallN(async.callback, async.context, flags, uintptr(unsafe.Pointer(&fault)),
		0x111, 0x222, 0x333, uintptr(unsafe.Pointer(data)))
	runtime.KeepAlive(async)
	runtime.KeepAlive(data)
	runtime.KeepAlive(&fault)
}

// Fault strings must be copied before the native callback storage expires.
func TestWSManCallbackCopiesFaultDetail(t *testing.T) {
	scope := &wsmanScope{}
	defer scope.release()
	pending := scope.pendingCall(false)
	wantDetail := strings.Repeat("fixture ", 32) + "credential context unavailable"
	detail, err := syscall.UTF16FromString(wantDetail)
	if err != nil {
		t.Fatal(err)
	}
	fault := wsmanError{code: 1312, detail: &detail[0]}
	syscall.SyscallN(pending.async.callback, pending.async.context, wsmanEndOfOperation,
		uintptr(unsafe.Pointer(&fault)), 0, 0, 0, 0)
	runtime.KeepAlive(&fault)
	detail[0] = 0
	runtime.GC()
	got, _, _ := pending.result.snapshot()
	if got == nil || !strings.Contains(got.Error(), "WSMan error 1312 (0x00000520): "+wantDetail) {
		t.Fatalf("fault detail was not copied: %v", got)
	}
}

func TestWSManCallbackCopiesValuesAndWaitsForCompletion(t *testing.T) {
	scope := &wsmanScope{}
	defer scope.release()
	pending := scope.pendingCall(true)
	state, _ := syscall.UTF16FromString("http://schemas.microsoft.com/wbem/wsman/1/windows/shell/CommandState/Running")
	data := wsmanReceiveData{commandState: &state[0], streamData: wsmanData{kind: 2}}
	for _, flags := range []uintptr{0, 8} {
		wsmanTestCallback(&pending.async, flags, 0, &data)
		select {
		case <-pending.result.done:
			t.Fatal("intermediate output completed receive")
		default:
		}
	}
	state, _ = syscall.UTF16FromString("http://schemas.microsoft.com/wbem/wsman/1/windows/shell/CommandState/Done")
	data.commandState = &state[0]
	data.exitCode = 7
	wsmanTestCallback(&pending.async, wsmanEndOfOperation, 0, &data)
	// Native buffers can be invalidated as soon as the callback returns.
	data.exitCode = 99
	state[0] = 0
	runtime.GC()
	if !pending.result.wait(time.Second) {
		t.Fatal("Done did not complete receive")
	}
	if err, done, code := pending.result.snapshot(); err != nil || !done || code != 7 {
		t.Fatalf("callback failed to copy result: %v %t %d", err, done, code)
	}
	// Duplicate callbacks must not overwrite results or close the channel twice.
	wsmanTestCallback(&pending.async, wsmanEndOfOperation, 123, nil)
	if err, _, code := pending.result.snapshot(); err != nil || code != 7 {
		t.Fatal("late callback overwrote result")
	}
}

func TestWSManCallbackCopiesErrorAndIgnoresReleasedContexts(t *testing.T) {
	scope := &wsmanScope{}
	pending := scope.pendingCall(false)
	wsmanTestCallback(&pending.async, wsmanEndOfOperation, 0x803380ba, nil)
	runtime.GC()
	if err, _, _ := pending.result.snapshot(); err == nil || !strings.Contains(err.Error(), "0x803380ba") {
		t.Fatalf("missing copied error: %v", err)
	}
	scope.release()
	if _, exists := wsmanContexts.Load(pending.async.context); exists {
		t.Fatal("context was not released")
	}
	wsmanTestCallback(&pending.async, wsmanEndOfOperation, 0, nil)
}

type wsmanFixture struct {
	t                     *testing.T
	fail                  string
	failOption            uintptr
	faultCode             uint32
	timeout               string
	calls                 []string
	contexts              []uintptr
	asyncs                []*wsmanShellAsync
	receiveState          string
	exitCode              uint32
	wantAuth              uint32
	wantURL               string
	wantArgs              []string
	implicitCredentials   bool
	wantImplicitNegotiate bool
	wantCertificateBypass bool
	certificateOptions    []uintptr
	wantIdentity          *winrmCredentials
	passwordBuffer        []uint16
	deployment            bool
	commands              int
	sent                  []byte
	sentEOF               bool
	sendOperations        []uintptr
	closedOperations      []uintptr
}

func (f *wsmanFixture) call(name string, args ...uintptr) uintptr {
	f.t.Helper()
	f.calls = append(f.calls, name)
	arities := map[string]int{
		"WSManInitialize": 2, "WSManDeinitialize": 2, "WSManCreateSession": 6, "WSManCloseSession": 2,
		"WSManCreateShell": 8, "WSManRunShellCommand": 7, "WSManReceiveShellOutput": 6,
		"WSManCloseOperation": 2, "WSManCloseCommand": 3, "WSManCloseShell": 3,
		"WSManSetSessionOption": 3,
		"WSManSendShellInput":   8,
	}
	if len(args) != arities[name] {
		f.t.Fatalf("%s has %d arguments, want %d", name, len(args), arities[name])
	}
	output := func(index int, value uintptr) { *(*uintptr)(unsafe.Pointer(args[index])) = value }
	complete := func(index int, data *wsmanReceiveData) {
		async := (*wsmanShellAsync)(unsafe.Pointer(args[index]))
		if async.context == 0 || async.callback == 0 {
			f.t.Fatalf("%s: missing async structure", name)
		}
		f.contexts = append(f.contexts, async.context)
		f.asyncs = append(f.asyncs, async)
		if name == f.timeout {
			return
		}
		code := uint32(0)
		if name == f.fail {
			code = 5
			if f.faultCode != 0 {
				code = f.faultCode
			}
		}
		wsmanTestCallback(async, wsmanEndOfOperation, code, data)
	}
	switch name {
	case "WSManInitialize":
		if name == f.fail {
			return 5
		}
		if args[0] != 0 {
			f.t.Fatal("unexpected initialize flags")
		}
		output(1, 10)
	case "WSManCreateSession":
		if args[0] != 10 || args[2] != 0 || args[4] != 0 {
			f.t.Fatal("session handle, flags or proxy shifted")
		}
		connection := readWSManString(args[1])
		wantURL := f.wantURL
		if wantURL == "" {
			wantURL = "http://lab-host:5985/wsman"
		}
		if connection != wantURL {
			f.t.Fatalf("WinRM connection string = %q, want %q", connection, wantURL)
		}
		if f.wantIdentity != nil {
			if args[3] == 0 {
				f.t.Fatal("missing explicit authentication credentials")
			}
			credentials := (*wsmanAuthCredentials)(unsafe.Pointer(args[3]))
			user := f.wantIdentity.user
			if f.wantIdentity.domain != "" {
				user = f.wantIdentity.domain + `\` + user
			}
			if credentials.username == 0 || credentials.password == 0 {
				f.t.Fatal("missing explicit credentials")
			}
			if readWSManString(credentials.username) != user || readWSManString(credentials.password) != f.wantIdentity.password {
				f.t.Fatal("explicit credential mismatch")
			}
			encoded, err := syscall.UTF16FromString(f.wantIdentity.password)
			if err != nil {
				f.t.Fatal(err)
			}
			f.passwordBuffer = unsafe.Slice((*uint16)(unsafe.Pointer(credentials.password)), len(encoded))
			wantAuth := f.wantAuth
			if wantAuth == 0 {
				wantAuth = 0x10
			}
			if credentials.mechanism != wantAuth {
				f.t.Fatal("explicit authentication mechanism mismatch")
			}
		} else if f.wantImplicitNegotiate {
			if args[3] == 0 {
				f.t.Fatal("HTTPS process identity must explicitly select Negotiate")
			}
			credentials := (*wsmanAuthCredentials)(unsafe.Pointer(args[3]))
			if credentials.mechanism != 4 || credentials.username != 0 || credentials.password != 0 {
				f.t.Fatal("expected Negotiate with implicit process credentials")
			}
		} else if args[3] != 0 {
			f.t.Fatal("process-default authentication must pass a NULL credential pointer")
		}
		if name == f.fail {
			return 5
		}
		output(5, 20)
	case "WSManSetSessionOption":
		certificateOption := args[1] == 18 || args[1] == 19 || args[1] == 31
		if args[0] != 20 || args[2] == 0 ||
			(certificateOption && !f.wantCertificateBypass) ||
			(!certificateOption && (!f.wantImplicitNegotiate || f.wantIdentity != nil || args[1] != 32)) {
			f.t.Fatal("unexpected implicit-credential session option")
		}
		// Read the SDK's DWORD union member independently of wsmanData.
		if *(*uint32)(unsafe.Pointer(args[2])) != 4 || *(*uint32)(unsafe.Pointer(args[2] + 8)) != 1 {
			f.t.Fatal("session option must be a DWORD with value 1")
		}
		if name == f.fail && (f.failOption == 0 || f.failOption == args[1]) {
			return 5
		}
		if certificateOption {
			f.certificateOptions = append(f.certificateOptions, args[1])
		} else {
			f.implicitCredentials = true
		}
	case "WSManCreateShell":
		if f.wantCertificateBypass && !reflect.DeepEqual(f.certificateOptions, []uintptr{18, 19, 31}) {
			f.t.Fatal("certificate bypass was incomplete before shell creation")
		}
		if f.wantImplicitNegotiate && !f.implicitCredentials {
			f.t.Fatal("Negotiate shell created before enabling implicit credentials")
		}
		if args[0] != 20 || args[1] != 0 || args[3] != 0 || args[4] != 0 || args[5] != 0 {
			f.t.Fatal("shell arguments shifted")
		}
		if readWSManString(args[2]) != wsmanShellCmdURI {
			f.t.Fatal("wrong resource URI")
		}
		output(7, 30)
		complete(6, nil)
	case "WSManRunShellCommand":
		f.commands++
		if args[0] != 30 || args[1] != 0 || args[4] != 0 {
			f.t.Fatal("command arguments shifted")
		}
		wantCommand := `C:\Program Files\fixture.exe`
		if f.deployment {
			wantCommand = winrmReceiverCommandPath
		}
		if readWSManString(args[2]) != wantCommand {
			f.t.Fatal("missing command path")
		}
		set := (*wsmanStringSet)(unsafe.Pointer(args[3]))
		values := unsafe.Slice(set.values, int(set.count))
		actual := make([]string, len(values))
		for i, value := range values {
			actual[i] = readWSManString(uintptr(unsafe.Pointer(value)))
		}
		if f.deployment {
			if len(actual) != 5 || actual[0] != "-NoLogo" || actual[1] != "-NoProfile" || actual[2] != "-NonInteractive" || actual[3] != "-EncodedCommand" || actual[4] == "" {
				f.t.Fatalf("bad receiver arguments: %q", actual)
			}
		} else {
			expected := f.wantArgs
			if expected == nil {
				expected = []string{"--lateral-session", "fixture-session"}
			}
			if !reflect.DeepEqual(actual, expected) {
				f.t.Fatalf("bad counted argument set: %q", actual)
			}
		}
		output(6, 40)
		complete(5, nil)
	case "WSManReceiveShellOutput":
		if args[0] != 30 || args[1] != 40 || args[2] != 0 || args[3] != 0 {
			f.t.Fatal("receive arguments shifted")
		}
		output(5, 50)
		stateName := f.receiveState
		if stateName == "" {
			stateName = wsmanCommandStateDone
		}
		state, _ := syscall.UTF16PtrFromString(wsmanShellCmdURI + stateName)
		data := &wsmanReceiveData{commandState: state, streamData: wsmanData{kind: 2}, exitCode: f.exitCode}
		complete(4, data)
	case "WSManSendShellInput":
		if args[0] != 30 || args[1] != 40 || args[2] != 0 || readWSManString(args[3]) != "stdin" || args[5] > 1 {
			f.t.Fatal("send arguments shifted")
		}
		data := (*wsmanData)(unsafe.Pointer(args[4]))
		if data.kind != 2 || data.length > winrmTransferChunkSize {
			f.t.Fatal("input must be bounded binary data")
		}
		if data.length != 0 {
			f.sent = append(f.sent, unsafe.Slice((*byte)(unsafe.Pointer(data.value)), int(data.length))...)
		}
		operation := uintptr(60 + len(f.sendOperations))
		output(7, operation)
		f.sendOperations = append(f.sendOperations, operation)
		complete(6, nil)
		if args[5] == 1 && f.timeout != name && f.fail != name {
			f.sentEOF = true
		}
	case "WSManCloseCommand", "WSManCloseShell":
		expected := uintptr(40)
		if name == "WSManCloseShell" {
			expected = 30
		}
		if args[0] != expected || args[1] != 0 {
			f.t.Fatal("close arguments shifted")
		}
		complete(2, nil)
	case "WSManCloseOperation", "WSManCloseSession", "WSManDeinitialize":
		if name == "WSManCloseOperation" {
			f.closedOperations = append(f.closedOperations, args[0])
		}
		expected := map[string]uintptr{"WSManCloseOperation": 50, "WSManCloseSession": 20, "WSManDeinitialize": 10}[name]
		if args[0] != expected || args[1] != 0 {
			if name != "WSManCloseOperation" || args[0] < 60 || args[1] != 0 {
				f.t.Fatal("cleanup arguments shifted")
			}
		}
		// Pending state must survive cancellation and session teardown.
		runtime.GC()
		if name == "WSManCloseSession" && f.timeout != "" {
			// Cancellation is allowed to invoke callbacks after the local wait
			// and CloseOperation have returned, up to synchronous session drain.
			for _, async := range f.asyncs {
				wsmanTestCallback(async, wsmanEndOfOperation, 995, nil)
			}
		}
		for _, context := range f.contexts {
			if _, ok := wsmanContexts.Load(context); !ok {
				f.t.Fatal("callback context freed before native teardown")
			}
		}
		if name == f.fail {
			return 5
		}
	}
	// Void APIs deliberately leave a nonzero register value. It is not an HRESULT.
	if strings.Contains(name, "Shell") || name == "WSManCloseCommand" {
		return 0xdeadbeef
	}
	return 0
}

func TestWSManNativeBoundaryAndCleanup(t *testing.T) {
	stages := []string{"", "WSManInitialize", "WSManCreateSession", "WSManCreateShell", "WSManRunShellCommand", "WSManReceiveShellOutput", "WSManCloseOperation", "WSManCloseCommand", "WSManCloseShell", "WSManCloseSession", "WSManDeinitialize"}
	for _, stage := range stages {
		t.Run(fmt.Sprintf("failure_%s", stage), func(t *testing.T) {
			f := &wsmanFixture{t: t, fail: stage}
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second)
			if (err != nil) != (stage != "") {
				t.Fatalf("stage=%s err=%v", stage, err)
			}
			for _, context := range f.contexts {
				if _, ok := wsmanContexts.Load(context); ok {
					t.Fatal("callback context leaked")
				}
			}
			if stage == "" {
				expected := []string{"WSManInitialize", "WSManCreateSession", "WSManCreateShell", "WSManRunShellCommand", "WSManReceiveShellOutput", "WSManCloseOperation", "WSManCloseCommand", "WSManCloseShell", "WSManCloseSession", "WSManDeinitialize"}
				if !reflect.DeepEqual(f.calls, expected) {
					t.Fatalf("cleanup order: %v", f.calls)
				}
			}
		})
	}
	for _, stage := range []string{"WSManCreateShell", "WSManRunShellCommand", "WSManReceiveShellOutput", "WSManCloseCommand", "WSManCloseShell"} {
		t.Run("timeout_"+stage, func(t *testing.T) {
			f := &wsmanFixture{t: t, timeout: stage}
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Millisecond)
			if err == nil || !strings.Contains(err.Error(), "time") {
				t.Fatalf("timeout was lost: %v", err)
			}
			if f.calls[len(f.calls)-1] != "WSManDeinitialize" {
				t.Fatal("timeout skipped teardown")
			}
			for _, context := range f.contexts {
				if _, ok := wsmanContexts.Load(context); ok {
					t.Fatal("timeout leaked context")
				}
			}
		})
	}
}

func TestWSManReceiveRequiresSuccessfulDone(t *testing.T) {
	for _, fixture := range []struct {
		state   string
		exit    uint32
		message string
	}{
		{"/CommandState/Running", 0, "without command completion"},
		{wsmanCommandStateDone, 7, "exited with 7"},
	} {
		t.Run(fixture.message, func(t *testing.T) {
			f := &wsmanFixture{t: t, receiveState: fixture.state, exitCode: fixture.exit}
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second)
			if err == nil || !strings.Contains(err.Error(), fixture.message) {
				t.Fatalf("unexpected result: %v", err)
			}
		})
	}
}

// The selected authentication mechanism follows the host form: Kerberos for
// DNS names, Negotiate (NTLM against an IP) for IP literals. The fixture
// asserts the mechanism on every session creation.
func TestWSManAuthMechanismFollowsHostForm(t *testing.T) {
	for _, tc := range []struct {
		host string
		want uint32
		url  string
	}{
		{"ws01.lab.local", 0x10, "http://ws01.lab.local:5985/wsman"},
		{"ws01", 0x10, "http://ws01:5985/wsman"},
		{"10.0.0.8", 0x4, "http://10.0.0.8:5985/wsman"},
		{"fd00::8", 0x4, "http://[fd00::8]:5985/wsman"},
	} {
		identity := &winrmCredentials{domain: "LAB", user: "fixture", password: "fixture"}
		f := &wsmanFixture{t: t, wantAuth: tc.want, wantURL: tc.url, wantIdentity: identity}
		if err := winrmCreateProcessWithIdentityAPI(f, tc.host, `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second, identity); err != nil {
			t.Fatalf("host %s: %v", tc.host, err)
		}
	}
}

// HTTP Default authentication uses NULL for both DNS and IP targets. HTTPS
// process identity is separately tested as explicit implicit-Negotiate mode.
func TestWSManDefaultProcessIdentity(t *testing.T) {
	for host, url := range map[string]string{
		"lab-host": "http://lab-host:5985/wsman",
		"10.0.0.8": "http://10.0.0.8:5985/wsman",
		"fd00::8":  "http://[fd00::8]:5985/wsman",
	} {
		f := &wsmanFixture{t: t, wantURL: url}
		if err := winrmCreateProcessWithAPI(f, host, `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second); err != nil {
			t.Fatal(err)
		}
	}
}

// Failed option configuration must close the session without opening a shell.
func TestWSManImplicitCredentialsOptionFailure(t *testing.T) {
	f := &wsmanFixture{t: t, fail: "WSManSetSessionOption", wantImplicitNegotiate: true, wantURL: "https://10.0.0.8:5986/wsman"}
	err := winrmCreateProcessWithTransportAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second, nil, true)
	if err == nil || !strings.Contains(err.Error(), "WSManSetSessionOption") {
		t.Fatalf("expected option error, got %v", err)
	}
	want := []string{"WSManInitialize", "WSManCreateSession", "WSManSetSessionOption", "WSManCloseSession", "WSManDeinitialize"}
	if !reflect.DeepEqual(f.calls, want) {
		t.Fatalf("failure cleanup: got %v want %v", f.calls, want)
	}
}

// Explicit identity fixtures verify native arguments and password-buffer
// cleanup without a remote connection or an installed WinRM service.
func TestWSManExplicitIdentity(t *testing.T) {
	for _, host := range []string{"lab-host", "10.0.0.8"} {
		for _, password := range []string{"fixture-password", "", "-unicode-🔒"} {
			for _, fail := range []string{"", "WSManCreateSession", "WSManCreateShell"} {
				identity := &winrmCredentials{domain: "LAB", user: "fixture-user", password: password}
				f := &wsmanFixture{t: t, wantIdentity: identity, wantURL: "http://" + host + ":5985/wsman", fail: fail}
				if host == "10.0.0.8" {
					f.wantAuth = 4
				}
				err := winrmCreateProcessWithIdentityAPI(f, host, `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second, identity)
				if (err != nil) != (fail != "") {
					t.Fatal("unexpected explicit identity result")
				}
				for _, value := range f.passwordBuffer {
					if value != 0 {
						t.Fatal("native password buffer was not cleared")
					}
				}
			}
		}
	}
	f := &wsmanFixture{t: t}
	err := winrmCreateProcessWithIdentityAPI(f, "lab-host", `C:\Program Files\fixture.exe`, wsmanFixtureArgs, time.Second, &winrmCredentials{user: "fixture", password: "invalid\x00password"})
	if err == nil || len(f.calls) != 0 || strings.Contains(err.Error(), "invalid") {
		t.Fatal("invalid credentials must fail before native calls without echoing input")
	}
}

// Real DLL resolution and session configuration do not open a remote shell.
func TestWSManLocalSession(t *testing.T) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded); hr != 0 && hr != 1 {
		t.Fatalf("CoInitializeEx: 0x%x", hr)
	}
	defer procCoUninitialize.Call()
	if err := initializeWMISecurity(); err != nil {
		t.Fatal(err)
	}
	native, err := loadWSMan()
	if err != nil {
		t.Fatal(err)
	}
	var api, session uintptr
	scope := &wsmanScope{}
	scope.keep(&api)
	scope.keep(&session)
	defer scope.release()
	if code := native.call("WSManInitialize", 0, uintptr(unsafe.Pointer(&api))); code != 0 {
		t.Fatalf("initialize: 0x%x", code)
	}
	defer native.call("WSManDeinitialize", api, 0)
	if code := native.call("WSManCreateSession", api, 0, 0, 0, 0, uintptr(unsafe.Pointer(&session))); code != 0 {
		t.Fatalf("create session: 0x%x", code)
	}
	defer native.call("WSManCloseSession", session, 0)
	if session == 0 {
		t.Fatal("missing session handle")
	}
	// Verify the HTTPS implicit-Negotiate configuration against the real DLL.
	// Session configuration does not open a remote connection or validate TLS.
	connection, err := syscall.UTF16PtrFromString("https://127.0.0.1:5986/wsman")
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.KeepAlive(connection)
	var configuredSession uintptr
	scope.keep(&configuredSession)
	credentials := wsmanAuthCredentials{mechanism: 4}
	scope.keep(&credentials)
	if code := native.call("WSManCreateSession", api, uintptr(unsafe.Pointer(connection)), 0, uintptr(unsafe.Pointer(&credentials)), 0, uintptr(unsafe.Pointer(&configuredSession))); code != 0 {
		t.Fatalf("create session with implicit Negotiate: 0x%x", code)
	}
	defer native.call("WSManCloseSession", configuredSession, 0)
	if configuredSession == 0 {
		t.Fatal("missing configured session handle")
	}
	option := wsmanData{kind: 4, length: 1}
	if code := native.call("WSManSetSessionOption", configuredSession, 32, uintptr(unsafe.Pointer(&option))); code != 0 {
		t.Fatalf("enable implicit Negotiate credentials: 0x%x", code)
	}
	runtime.KeepAlive(&option)
}

//go:build windows

package main

import (
	"fmt"
	"reflect"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

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
	}
	for name, pair := range checks {
		if pair[0] != pair[1] {
			t.Errorf("%s: got %d want %d", name, pair[0], pair[1])
		}
	}
}

// Invoke the actual Windows callback trampoline, including all seven slots.
func wsmanTestCallback(async *wsmanShellAsync, flags uintptr, code uint32, data *wsmanReceiveData) {
	syscall.SyscallN(async.callback, async.context, flags, uintptr(unsafe.Pointer(&code)),
		0x111, 0x222, 0x333, uintptr(unsafe.Pointer(data)))
	runtime.KeepAlive(async)
	runtime.KeepAlive(data)
	runtime.KeepAlive(&code)
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
	t                   *testing.T
	fail                string
	timeout             string
	calls               []string
	contexts            []uintptr
	asyncs              []*wsmanShellAsync
	receiveState        string
	exitCode            uint32
	wantAuth            uint32
	wantURL             string
	implicitCredentials bool
}

func (f *wsmanFixture) call(name string, args ...uintptr) uintptr {
	f.t.Helper()
	f.calls = append(f.calls, name)
	arities := map[string]int{
		"WSManInitialize": 2, "WSManDeinitialize": 2, "WSManCreateSession": 6, "WSManCloseSession": 2,
		"WSManCreateShell": 8, "WSManRunShellCommand": 7, "WSManReceiveShellOutput": 6,
		"WSManCloseOperation": 2, "WSManCloseCommand": 3, "WSManCloseShell": 3,
		"WSManSetSessionOption": 3,
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
		if args[3] == 0 {
			f.t.Fatal("missing authentication credentials")
		}
		credentials := (*wsmanAuthCredentials)(unsafe.Pointer(args[3]))
		if credentials.username != 0 || credentials.password != 0 {
			f.t.Fatal("session credentials must keep the implicit thread identity")
		}
		wantAuth := f.wantAuth
		if wantAuth == 0 {
			wantAuth = 0x10 // Existing fixtures use the DNS target lab-host.
		}
		if credentials.mechanism != wantAuth {
			f.t.Fatalf("auth mechanism: got 0x%x want 0x%x", credentials.mechanism, wantAuth)
		}
		if name == f.fail {
			return 5
		}
		output(5, 20)
	case "WSManSetSessionOption":
		if f.wantAuth != 0x4 || args[0] != 20 || args[1] != 32 || args[2] == 0 {
			f.t.Fatal("unexpected implicit-credential session option")
		}
		// Read the SDK's DWORD union member independently of wsmanData.
		if *(*uint32)(unsafe.Pointer(args[2])) != 4 || *(*uint32)(unsafe.Pointer(args[2] + 8)) != 1 {
			f.t.Fatal("session option must be a DWORD with value 1")
		}
		if name == f.fail {
			return 5
		}
		f.implicitCredentials = true
	case "WSManCreateShell":
		if f.wantAuth == 0x4 && !f.implicitCredentials {
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
		if args[0] != 30 || args[1] != 0 || args[4] != 0 {
			f.t.Fatal("command arguments shifted")
		}
		if readWSManString(args[2]) != `C:\Program Files\fixture.exe` {
			f.t.Fatal("missing command path")
		}
		set := (*wsmanStringSet)(unsafe.Pointer(args[3]))
		values := unsafe.Slice(set.values, int(set.count))
		actual := make([]string, len(values))
		for i, value := range values {
			actual[i] = readWSManString(uintptr(unsafe.Pointer(value)))
		}
		expected := []string{"--lateral-detach", "--lateral-session", "fixture-session", "--workspace-id", "fixture-workspace", "--service-name", "fixture-service"}
		if !reflect.DeepEqual(actual, expected) {
			f.t.Fatalf("bad counted argument set: %q", actual)
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
		expected := map[string]uintptr{"WSManCloseOperation": 50, "WSManCloseSession": 20, "WSManDeinitialize": 10}[name]
		if args[0] != expected || args[1] != 0 {
			f.t.Fatal("cleanup arguments shifted")
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
			workspace := "fixture-workspace"
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second)
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
			workspace := "fixture-workspace"
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Millisecond)
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
			workspace := "fixture-workspace"
			err := winrmCreateProcessWithAPI(f, "lab-host", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second)
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
		f := &wsmanFixture{t: t, wantAuth: tc.want, wantURL: tc.url}
		workspace := "fixture-workspace"
		if err := winrmCreateProcessWithAPI(f, tc.host, `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second); err != nil {
			t.Fatalf("host %s: %v", tc.host, err)
		}
	}
}

// Failed option configuration must close the session without opening a shell.
func TestWSManImplicitCredentialsOptionFailure(t *testing.T) {
	f := &wsmanFixture{t: t, fail: "WSManSetSessionOption", wantAuth: 0x4, wantURL: "http://10.0.0.8:5985/wsman"}
	workspace := "fixture-workspace"
	err := winrmCreateProcessWithAPI(f, "10.0.0.8", `C:\Program Files\fixture.exe`, "fixture-session", &workspace, "fixture-service", time.Second)
	if err == nil || !strings.Contains(err.Error(), "WSManSetSessionOption") {
		t.Fatalf("expected option error, got %v", err)
	}
	want := []string{"WSManInitialize", "WSManCreateSession", "WSManSetSessionOption", "WSManCloseSession", "WSManDeinitialize"}
	if !reflect.DeepEqual(f.calls, want) {
		t.Fatalf("failure cleanup: got %v want %v", f.calls, want)
	}
}

// Real DLL resolution and session creation are local operations. No shell,
// remote command, alternate credentials, or running WinRM service is needed.
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
	// The credentials structure must also be accepted by the real client
	// with the implicit thread identity; session creation stays local.
	connection, err := syscall.UTF16PtrFromString("http://127.0.0.1:5985/wsman")
	if err != nil {
		t.Fatal(err)
	}
	defer runtime.KeepAlive(connection)
	credentials := wsmanAuthCredentials{mechanism: wsmanAuthMechanism("127.0.0.1")}
	scope.keep(&credentials)
	var configuredSession uintptr
	scope.keep(&configuredSession)
	if code := native.call("WSManCreateSession", api, uintptr(unsafe.Pointer(connection)), 0, uintptr(unsafe.Pointer(&credentials)), 0, uintptr(unsafe.Pointer(&configuredSession))); code != 0 {
		t.Fatalf("create session with auth mechanism: 0x%x", code)
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

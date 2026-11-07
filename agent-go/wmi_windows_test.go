//go:build windows

package main

import (
	"runtime"
	"strings"
	"syscall"
	"testing"
	"unsafe"
)

// TestWMIAuthentication checks the native BSTR length and the shared SPN
// without connecting to a remote host or creating a process.
func TestWMIAuthentication(t *testing.T) {
	for _, host := range []string{"ws01", "ws01.lab.local", "10.0.0.8", "fd00::8"} {
		t.Run(host, func(t *testing.T) {
			service, authority, principal, err := wmiAuthentication(host)
			if err != nil {
				t.Fatal(err)
			}
			defer sysFreeString(authority)
			defer runtime.KeepAlive(principal)
			if host == "10.0.0.8" || host == "fd00::8" {
				if service != 10 || authority != 0 || principal != nil {
					t.Fatal("IP target must use NTLM with no authority or principal")
				}
				return
			}
			if service != 16 || authority == 0 || principal == nil {
				t.Fatal("DNS target must use Kerberos with an authority and principal")
			}
			want := "kerberos:HOST/" + host
			length, _, _ := oleaut32.NewProc("SysStringLen").Call(authority)
			if length != uintptr(len(want)) {
				t.Fatalf("BSTR length = %d, want %d", length, len(want))
			}
			if got := syscall.UTF16ToString(unsafe.Slice((*uint16)(unsafe.Pointer(authority)), int(length))); got != want {
				t.Fatalf("authority = %q, want %q", got, want)
			}
			if got := syscall.UTF16ToString(unsafe.Slice(principal, len("HOST/"+host)+1)); got != "HOST/"+host {
				t.Fatalf("principal = %q", got)
			}
		})
	}
	if _, authority, _, err := wmiAuthentication("bad\x00host"); err == nil || authority != 0 {
		t.Fatal("embedded NUL must fail without allocating an authority")
	}
}

// TestWMICreateProcess exercises the real local WMI provider and COM ABI.
// The loopback IP keeps the connection on the NTLM path: a DNS name would
// select the Kerberos authority, which WMI rejects for local connections.
func TestWMICreateProcess(t *testing.T) {
	if err := wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`); err != nil {
		t.Fatalf("local WMI process creation: %v", err)
	}
	// A nonexistent executable must reach ExecMethod and return provider failure.
	err := wmiCreateProcess("127.0.0.1", `C:\__rmm_wmi_nonexistent__\missing.exe`)
	if err == nil || !strings.Contains(err.Error(), "WMI Win32_Process.Create returned ") {
		t.Fatalf("expected provider ReturnValue failure, got %v", err)
	}
}

//go:build windows

package main

import (
	"bytes"
	"errors"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"unsafe"
)

func TestWhoamiNativeLayouts(t *testing.T) {
	var u whoamiUnicode
	var l whoamiLogonData
	var ticket whoamiTicket
	if unsafe.Sizeof(uintptr(0)) == 8 {
		if unsafe.Sizeof(u) != 16 || unsafe.Offsetof(l.User) != 16 || unsafe.Offsetof(l.LogonTime) != 80 || unsafe.Offsetof(l.UPN) != 120 || unsafe.Sizeof(l) != 136 || unsafe.Sizeof(ticket) != 96 || unsafe.Offsetof(ticket.Start) != 64 {
			t.Fatalf("invalid 64-bit native layouts: Unicode=%d logon=%d ticket=%d", unsafe.Sizeof(u), unsafe.Sizeof(l), unsafe.Sizeof(ticket))
		}
	} else {
		if unsafe.Sizeof(u) != 8 || unsafe.Offsetof(l.User) != 12 || unsafe.Offsetof(l.LogonTime) != 48 || unsafe.Offsetof(l.UPN) != 72 || unsafe.Sizeof(l) != 80 || unsafe.Sizeof(ticket) != 64 || unsafe.Offsetof(ticket.Start) != 32 {
			t.Fatalf("invalid 32-bit native layouts: Unicode=%d logon=%d ticket=%d", unsafe.Sizeof(u), unsafe.Sizeof(l), unsafe.Sizeof(ticket))
		}
	}
	if got := whoamiTime(116444736000000000); got != "1970-01-01T00:00:00Z" {
		t.Fatal(got)
	}
}

func TestWhoamiReadOnlySnapshot(t *testing.T) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	process, err := syscall.OpenCurrentProcessToken()
	if err != nil {
		t.Fatal(err)
	}
	defer process.Close()
	before, err := whoamiTokenInfo(process, 10)
	if err != nil {
		t.Fatal(err)
	}
	beforePriv, err := whoamiTokenInfo(process, 3)
	if err != nil {
		t.Fatal(err)
	}
	var thread syscall.Token
	open := func() (syscall.Token, error) {
		var h syscall.Token
		ok, _, err := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 8, 1, uintptr(unsafe.Pointer(&h)))
		if ok == 0 {
			return 0, err
		}
		return h, nil
	}
	thread, threadErr := open()
	var beforeThread []byte
	if thread != 0 {
		defer thread.Close()
		beforeThread, err = whoamiTokenInfo(thread, 10)
		if err != nil {
			t.Fatal(err)
		}
	} else if threadErr != syscall.Errno(1008) {
		t.Fatal(threadErr)
	}
	output, err := whoamiAction(true)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"=== Process ===\nPID:", "=== Process token ===", "Authentication LUID:", "Integrity:", "Groups (", "Restricted SIDs (", "SeDebugPrivilege:\n    ", "Kerberos tickets"} {
		if !strings.Contains(output, want) {
			t.Errorf("missing report field %q", want)
		}
	}
	after, err := whoamiTokenInfo(process, 10)
	if err != nil {
		t.Fatal(err)
	}
	afterPriv, err := whoamiTokenInfo(process, 3)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) || !bytes.Equal(beforePriv, afterPriv) {
		t.Error("process token statistics or privileges changed")
	}
	current, currentErr := open()
	if current != 0 {
		defer current.Close()
	}
	if (current == 0) != (thread == 0) || (current == 0 && currentErr != threadErr) {
		t.Fatal("thread token presence changed")
	}
	if current != 0 {
		afterThread, err := whoamiTokenInfo(current, 10)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(beforeThread, afterThread) {
			t.Error("thread token statistics changed")
		}
	}
}

// TestWhoamiImpersonatedReadOnlySnapshot exercises the thread-token branch with
// the current account only; no credentials or additional privileges are needed.
func TestWhoamiImpersonatedReadOnlySnapshot(t *testing.T) {
	runtime.LockOSThread()
	var saved syscall.Token
	ok, _, captureErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 8|4, 1, uintptr(unsafe.Pointer(&saved)))
	if ok == 0 && captureErr != syscall.Errno(1008) {
		runtime.UnlockOSThread()
		t.Fatal(captureErr)
	}
	if saved != 0 {
		defer saved.Close()
	}
	// Always restore the exact ambient token before making this thread reusable.
	defer func() {
		var restored uintptr
		if saved != 0 {
			restored, _, _ = procImpersonateLoggedOnUser.Call(uintptr(saved))
		} else {
			restored, _, _ = procRevertToSelf.Call()
		}
		if restored == 0 {
			t.Error("failed to restore test thread token; keeping thread pinned")
			return
		}
		runtime.UnlockOSThread()
	}()
	ok, _, err := advapi32.NewProc("ImpersonateSelf").Call(2)
	if ok == 0 {
		t.Fatalf("ImpersonateSelf: %v", err)
	}
	TestWhoamiReadOnlySnapshot(t)
	output, err := whoamiAction(false)
	if err != nil || !strings.Contains(output, "Effective current-thread token\n") || !strings.Contains(output, "Impersonation level: 2 (impersonation)") {
		t.Fatalf("missing impersonation snapshot: %v\n%s", err, output)
	}
}

func TestWhoamiTokenQueryBounds(t *testing.T) {
	t.Run("initial-error-preserved", func(t *testing.T) {
		calls := 0
		_, err := whoamiQueryTokenInfo(21, func(_ []byte, n *uint32) (bool, error) { calls++; return false, syscall.ERROR_ACCESS_DENIED })
		if !errors.Is(err, syscall.ERROR_ACCESS_DENIED) || calls != 1 {
			t.Fatalf("error=%v calls=%d", err, calls)
		}
	})
	t.Run("one-byte-boolean", func(t *testing.T) {
		calls := 0
		got, err := whoamiQueryTokenInfo(21, func(buf []byte, n *uint32) (bool, error) {
			calls++
			*n = 1
			if buf == nil {
				return false, syscall.ERROR_INSUFFICIENT_BUFFER
			}
			if len(buf) < 4 {
				t.Fatal("BOOLEAN buffer must have DWORD storage")
			}
			buf[0] = 1
			return true, nil
		})
		if err != nil || len(got) != 1 || got[0] != 1 || calls != 2 {
			t.Fatalf("got=%v err=%v calls=%d", got, err, calls)
		}
	})
	t.Run("growth-and-actual-size", func(t *testing.T) {
		calls := 0
		got, err := whoamiQueryTokenInfo(2, func(buf []byte, n *uint32) (bool, error) {
			calls++
			switch calls {
			case 1:
				*n = 4
				return false, syscall.ERROR_INSUFFICIENT_BUFFER
			case 2:
				*n = 16
				return false, syscall.ERROR_INSUFFICIENT_BUFFER
			default:
				if len(buf) != 16 {
					t.Fatalf("growth: %d", len(buf))
				}
				*n = 8
				return true, nil
			}
		})
		if err != nil || len(got) != 8 {
			t.Fatalf("got=%v err=%v", got, err)
		}
	})
	for _, tc := range []struct {
		name     string
		size     uint32
		returned uint32
		ok       bool
	}{
		{"zero-size", 0, 0, false}, {"excessive-size", 1 << 21, 0, false}, {"short-success", 4, 0, true}, {"oversized-success", 4, 8, true}, {"non-growing", 4, 4, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := whoamiQueryTokenInfo(2, func(buf []byte, n *uint32) (bool, error) {
				if buf == nil {
					*n = tc.size
					return false, syscall.ERROR_INSUFFICIENT_BUFFER
				}
				*n = tc.returned
				return tc.ok, syscall.ERROR_INSUFFICIENT_BUFFER
			})
			if err == nil {
				t.Fatal("invalid response accepted")
			}
		})
	}
}

func TestWhoamiNativeBufferBounds(t *testing.T) {
	for _, buf := range [][]byte{nil, {1}, {0, 0, 0}} {
		if got := whoamiBufferSID(buf, 0); !strings.Contains(got, "unavailable") {
			t.Fatalf("short SID buffer: %q", got)
		}
	}
	buf := make([]byte, 64)
	ptr := unsafe.Pointer(&buf[0])
	wide := (*uint16)(unsafe.Pointer(&buf[16]))
	*wide = 'A'
	if got := whoamiTextWithin(whoamiUnicode{2, 2, wide}, ptr, uint32(len(buf))); got != "A" {
		t.Fatal(got)
	}
	for _, tc := range []whoamiUnicode{{2, 2, nil}, {3, 4, wide}, {4, 2, wide}, {2, 2, (*uint16)(unsafe.Pointer(&buf[63]))}} {
		if got := whoamiTextWithin(tc, ptr, uint32(len(buf))); !strings.HasPrefix(got, "<") {
			t.Fatalf("invalid Unicode accepted: %q", got)
		}
	}
	// A pointer-sized field alone cannot supply a SID header.
	sidBuf := make([]byte, int(unsafe.Sizeof(uintptr(0))))
	*(*unsafe.Pointer)(unsafe.Pointer(&sidBuf[0])) = unsafe.Pointer(&sidBuf[len(sidBuf)-1])
	if got := whoamiBufferSID(sidBuf, 0); !strings.Contains(got, "unavailable") {
		t.Fatal(got)
	}
}

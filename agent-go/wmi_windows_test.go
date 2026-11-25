//go:build windows

package main

import (
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
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
	if err := wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`, nil, 0, 0); err != nil {
		t.Fatalf("local WMI process creation: %v", err)
	}
	// A nonexistent executable must reach ExecMethod and return provider failure.
	err := wmiCreateProcess("127.0.0.1", `C:\__rmm_wmi_nonexistent__\missing.exe`, nil, 0, 0)
	if err == nil || !strings.Contains(err.Error(), "WMI Win32_Process.Create returned ") {
		t.Fatalf("expected provider ReturnValue failure, got %v", err)
	}
}

// TestWMICreateProcessWorkerAmbientCycle exercises the worker's per-operation
// identity machinery with two distinct network-only logons and a final
// process-identity operation — the account-switching mechanics of the
// original regression at the loopback level. The loopback connects under
// fixture credentials fail authentication by design (their effective
// outcome is the connect error, keeping the test account-independent); the
// assertion is the final process-identity operation succeeding through the
// same worker, which requires every earlier operation to have applied and
// reverted its identity for real: a leaked thread token would fail the
// next operation's entry check, and the worker's held lifetime means the
// operations share one continuous apartment, mirroring what repeated
// remote deployments now do.
func TestWMICreateProcessWorkerAmbientCycle(t *testing.T) {
	tokenA, _, err := captureLateralExplicitIdentity(winrmCredentials{
		domain: "LAB", user: "wmi-fixture-a", password: "fixture-a-unused",
	})
	if err != nil {
		t.Fatalf("create the first network-only logon: %v", err)
	}
	defer tokenA.Close()
	tokenB, _, err := captureLateralExplicitIdentity(winrmCredentials{
		domain: "LAB", user: "wmi-fixture-b", password: "fixture-b-unused",
	})
	if err != nil {
		t.Fatalf("create the second network-only logon: %v", err)
	}
	defer tokenB.Close()

	err = wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`, nil, uintptr(tokenA), 30*time.Second)
	if err == nil {
		t.Fatal("a loopback connect under unvalidated fixture credentials must not succeed")
	}
	err = wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`, nil, uintptr(tokenB), 30*time.Second)
	if err == nil {
		t.Fatal("a loopback connect under unvalidated fixture credentials must not succeed")
	}
	// The account-switch proof: the worker thread must be clean after two
	// failed ambient operations, and this process-identity operation must
	// succeed — on the same held apartment the earlier operations used.
	if err := wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`, nil, 0, 30*time.Second); err != nil {
		t.Fatalf("process-identity operation after two ambient cycles failed (identity not cleanly reverted?): %v", err)
	}
}

// TestWMICreateProcessCleanupFailureInvalidatesResult pins the review
// finding that a cleanup failure must never let the operation report
// success: with the deferred verification reporting a residual thread
// token, the chain returns an error naming the enforcement failure even
// though the operation itself succeeded, and the identity-boundary fault
// is raised. The test resets the process-global boundary flag so it does
// not poison the rest of the Windows CI run.
func TestWMICreateProcessCleanupFailureInvalidatesResult(t *testing.T) {
	defer func() {
		wmiCleanupTokenPresent = wmiThreadTokenPresent
		identityBoundaryFault.Store(false)
	}()
	wmiCleanupTokenPresent = func() (bool, error) { return true, nil }
	token, _, err := captureLateralExplicitIdentity(winrmCredentials{
		domain: "LAB", user: "wmi-fixture", password: "fixture-unused",
	})
	if err != nil {
		t.Fatalf("create the network-only logon: %v", err)
	}
	defer token.Close()
	err = wmiCreateProcess("127.0.0.1", `cmd.exe /c exit 0`, nil, uintptr(token), 30*time.Second)
	if err == nil {
		t.Fatal("a failed cleanup verification must invalidate the operation result")
	}
	if !strings.Contains(err.Error(), "thread token after the operation") {
		t.Fatalf("the error must name the cleanup enforcement failure, got %v", err)
	}
	if !identityBoundaryFault.Load() {
		t.Fatal("a failed cleanup verification must raise the identity-boundary fault")
	}
}

// TestCocreateWbemLocatorRetriesKeyDeleted drives the bounded retry of the
// local WbemLocator activation with a scripted seam: two ERROR_KEY_DELETED
// results then success, proving the production chain rides out the
// delete-pending class-store window instead of failing the deployment. The
// sleep seam keeps the test instantaneous.
func TestCocreateWbemLocatorRetriesKeyDeleted(t *testing.T) {
	defer func() {
		cocreateWbemLocatorRaw = wmiCocreateRaw
		wmiCocreateRetrySleep = time.Sleep
	}()
	attempts := 0
	cocreateWbemLocatorRaw = func() (uintptr, uintptr) {
		attempts++
		if attempts <= 2 {
			return 0, hrErrorKeyDeleted
		}
		return 0x42, 0
	}
	slept := 0
	wmiCocreateRetrySleep = func(time.Duration) { slept++ }
	locator, err := cocreateWbemLocator()
	if err != nil {
		t.Fatalf("the transient activation must succeed on retry: %v", err)
	}
	if locator != 0x42 {
		t.Fatalf("locator = 0x%x, want the scripted out pointer", locator)
	}
	if attempts != 3 || slept != 2 {
		t.Fatalf("attempts = %d sleeps = %d, want 3 attempts and 2 bounded sleeps", attempts, slept)
	}
}

// TestCocreateWbemLocatorFailsFastOnDeterministicHR pins that nothing but
// the transient HRESULT is retried: a class-not-registered failure must
// surface on the first attempt with the raw HRESULT and no retry context.
func TestCocreateWbemLocatorFailsFastOnDeterministicHR(t *testing.T) {
	defer func() {
		cocreateWbemLocatorRaw = wmiCocreateRaw
		wmiCocreateRetrySleep = time.Sleep
	}()
	calls := 0
	cocreateWbemLocatorRaw = func() (uintptr, uintptr) {
		calls++
		return 0, 0x80040154 // REGDB_E_CLASSNOTREG
	}
	wmiCocreateRetrySleep = func(time.Duration) { t.Fatal("a deterministic failure must not sleep") }
	_, err := cocreateWbemLocator()
	if err == nil || !strings.Contains(err.Error(), "0x80040154") {
		t.Fatalf("the raw HRESULT must surface, got %v", err)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want exactly one attempt", calls)
	}
}

// TestCocreateWbemLocatorExhaustedRetriesKeepsHR pins the bounded schedule's
// terminal path: an unresolvable delete-pending state still reports the raw
// HRESULT with the retry context, after exactly the scheduled attempts.
func TestCocreateWbemLocatorExhaustedRetriesKeepsHR(t *testing.T) {
	origProfileLoader := wmiEnsureProfileLoaded
	defer func() {
		cocreateWbemLocatorRaw = wmiCocreateRaw
		cocreateWbemLocatorFallback = wmiDirectRaw
		wmiCocreateRetrySleep = time.Sleep
		wmiEnsureProfileLoaded = origProfileLoader
	}()
	wmiEnsureProfileLoaded = func() error { return fmt.Errorf("stubbed out") }
	cocreateWbemLocatorFallback = func() (uintptr, uintptr) { return 0, 0x80004002 }
	calls := 0
	cocreateWbemLocatorRaw = func() (uintptr, uintptr) {
		calls++
		return 0, hrErrorKeyDeleted
	}
	slept := 0
	wmiCocreateRetrySleep = func(time.Duration) { slept++ }
	_, err := cocreateWbemLocator()
	if err == nil || !strings.Contains(err.Error(), "0x800703fa") {
		t.Fatalf("the exhausted transient HRESULT must surface, got %v", err)
	}
	if !strings.Contains(err.Error(), "retries") {
		t.Fatalf("the exhaustion error must name the retries, got %v", err)
	}
	if !strings.Contains(err.Error(), "fallback both failed") {
		t.Fatalf("the exhaustion error must name the failed fallback, got %v", err)
	}
	if calls != 1+len(wmiCocreateRetryDelays) || slept != len(wmiCocreateRetryDelays) {
		t.Fatalf("calls = %d sleeps = %d, want %d attempts and %d sleeps",
			calls, slept, 1+len(wmiCocreateRetryDelays), len(wmiCocreateRetryDelays))
	}
}

// TestCocreateWbemLocatorFallsBackToDirectOnKeyDeleted pins the automatic
// fallback: when CoCreateInstance exhausts the retry schedule on
// ERROR_KEY_DELETED, the direct DllGetClassObject export succeeds
// automatically.
func TestCocreateWbemLocatorFallsBackToDirectOnKeyDeleted(t *testing.T) {
	origProfileLoader := wmiEnsureProfileLoaded
	defer func() {
		cocreateWbemLocatorRaw = wmiCocreateRaw
		cocreateWbemLocatorFallback = wmiDirectRaw
		wmiCocreateRetrySleep = time.Sleep
		wmiEnsureProfileLoaded = origProfileLoader
	}()
	// Profile reload fails — the fallback to DllGetClassObject must recover.
	wmiEnsureProfileLoaded = func() error { return fmt.Errorf("profile service unavailable") }
	cocreateCalls := 0
	cocreateWbemLocatorRaw = func() (uintptr, uintptr) {
		cocreateCalls++
		return 0, hrErrorKeyDeleted
	}
	fallbackCalled := false
	cocreateWbemLocatorFallback = func() (uintptr, uintptr) {
		fallbackCalled = true
		return 0xABCD, 0
	}
	wmiCocreateRetrySleep = func(time.Duration) {}
	locator, err := cocreateWbemLocator()
	if err != nil {
		t.Fatalf("the direct fallback must recover the activation: %v", err)
	}
	if locator != 0xABCD {
		t.Fatalf("locator = 0x%x, want the fallback's out pointer", locator)
	}
	if !fallbackCalled {
		t.Fatal("the direct fallback must have been called")
	}
	if cocreateCalls != 1+len(wmiCocreateRetryDelays) {
		t.Fatalf("CoCreateInstance calls = %d, want %d (initial + retries)",
			cocreateCalls, 1+len(wmiCocreateRetryDelays))
	}
}

// TestCocreateWbemLocatorProfileReloadRecovery pins the profile-reload path:
// when CoCreateInstance exhausts the retry schedule on ERROR_KEY_DELETED and
// the profile reload succeeds, the next CoCreateInstance must succeed without
// falling through to the DllGetClassObject bypass.
func TestCocreateWbemLocatorProfileReloadRecovery(t *testing.T) {
	origProfileLoader := wmiEnsureProfileLoaded
	defer func() {
		cocreateWbemLocatorRaw = wmiCocreateRaw
		cocreateWbemLocatorFallback = wmiDirectRaw
		wmiCocreateRetrySleep = time.Sleep
		wmiEnsureProfileLoaded = origProfileLoader
	}()

	profileLoaded := false
	cocreateCalls := 0
	// CoCreateInstance fails until the profile is loaded.
	cocreateWbemLocatorRaw = func() (uintptr, uintptr) {
		cocreateCalls++
		if profileLoaded {
			return 0xBEEF, 0
		}
		return 0, hrErrorKeyDeleted
	}
	wmiEnsureProfileLoaded = func() error {
		profileLoaded = true
		return nil
	}
	fallbackCalled := false
	cocreateWbemLocatorFallback = func() (uintptr, uintptr) {
		fallbackCalled = true
		return 0xDEAD, 0
	}
	wmiCocreateRetrySleep = func(time.Duration) {}

	locator, err := cocreateWbemLocator()
	if err != nil {
		t.Fatalf("profile reload must recover the activation: %v", err)
	}
	if locator != 0xBEEF {
		t.Fatalf("locator = 0x%x, want 0xBEEF from the standard path after profile reload", locator)
	}
	if !profileLoaded {
		t.Fatal("the profile must have been loaded")
	}
	if fallbackCalled {
		t.Fatal("the DllGetClassObject fallback must NOT be called when profile reload succeeds")
	}
	// initial + retries + 1 post-profile-reload attempt
	wantCalls := 1 + len(wmiCocreateRetryDelays) + 1
	if cocreateCalls != wantCalls {
		t.Fatalf("CoCreateInstance calls = %d, want %d (initial + retries + post-reload)",
			cocreateCalls, wantCalls)
	}
}

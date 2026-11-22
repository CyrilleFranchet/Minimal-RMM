//go:build windows

package main

import (
	"errors"
	"strings"
	"syscall"
	"testing"
)

func TestWMIProbePreservesCallerIdentity(t *testing.T) {
	// No target or valid credential is needed: NEW_CREDENTIALS creates a
	// local logon context without validating this fixture against a domain.
	restoreTest, err := suspendServerIdentity()
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		procRevertToSelf.Call()
		if err := restoreTest(); err != nil {
			t.Error(err)
		}
	}()
	token, _, err := captureLateralExplicitIdentity(winrmCredentials{domain: "LAB", user: "probe-fixture", password: "unused"})
	if err != nil {
		t.Fatal(err)
	}
	defer token.Close()
	failure := errors.New("fixture early failure")
	for _, callerToken := range []bool{false, true} {
		for _, runErr := range []error{nil, failure} {
			procRevertToSelf.Call()
			if callerToken {
				if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(token)); ok == 0 {
					t.Fatal(err)
				}
			}
			before := wmiProbeCaptureThreadToken()
			called := false
			result, gotErr := wmiProbeWithCallerIdentity(func() (string, error) {
				called = true
				idle := wmiProbeCaptureThreadToken()
				if idle.err != nil || idle.present {
					t.Errorf("run did not enter clean: %+v", idle)
				}
				// Model a runner applying and clearing a step token before
				// returning either evidence or an early machinery error.
				if ok, _, err := procImpersonateLoggedOnUser.Call(uintptr(token)); ok == 0 {
					t.Fatal(err)
				}
				if ok, _, err := procRevertToSelf.Call(); ok == 0 {
					t.Fatal(err)
				}
				return "fixture evidence", runErr
			})
			after := wmiProbeCaptureThreadToken()
			if !called || result != "fixture evidence" || !errors.Is(gotErr, runErr) {
				t.Fatalf("callerToken=%v: result=%q err=%v", callerToken, result, gotErr)
			}
			if before.err != nil || after.err != nil || before.present != after.present ||
				(before.present && (before.authID != after.authID || before.sid != after.sid || before.impLevel != after.impLevel)) {
				t.Fatalf("caller identity was not preserved: before=%+v after=%+v", before, after)
			}
		}
	}
}

func TestWMIProbeTokenCaptureFailureIsNotAbsence(t *testing.T) {
	snapshot := wmiProbeCaptureToken(syscall.Token(^uintptr(0) - 100))
	if snapshot.err == nil || !strings.Contains(snapshot.identityDetail(wmiProbeToken{}), "token capture failed") {
		t.Fatalf("invalid handle must not report no thread token: %+v", snapshot)
	}
}

func TestWMIProbeProxyImpersonationLabels(t *testing.T) {
	for level, want := range []string{"default", "anonymous", "identification", "impersonation", "delegation", "other"} {
		if got := proxyImpersonationLevelLabel(uint32(level)); got != want {
			t.Errorf("RPC level %d: got %q, want %q", level, got, want)
		}
	}
	if got := impersonationLevelLabel(2); got != "impersonation" {
		t.Errorf("token level 2: got %q", got)
	}
}

func TestWMIProbeDeniedConnectDoesNotReportQuerySuccess(t *testing.T) {
	result := wmiProbeResult{ConnectHR: 0x80070005}
	result.stageHR("connect", result.ConnectHR, 0, "denied")
	result.markRemainingNotRun("connect", "")
	for _, stage := range result.Stages {
		if stage.name == "query" {
			if !stage.notRun || stage.hasHR {
				t.Fatalf("unexecuted query must not have a result: %+v", stage)
			}
			return
		}
	}
	t.Fatal("missing query not_run stage")
}

func TestWMIProbeIdentitySwitchObservationStoresAndCompares(t *testing.T) {
	reset := func() {
		wmiProbeSeen.Lock()
		wmiProbeSeen.state = wmiProbeSeenState{}
		wmiProbeSeen.Unlock()
	}
	reset()
	defer reset()
	accountA := wmiProbeResult{StartUTC: "2026-11-21T10:00:00.000Z", ConnectHR: 0, Before: wmiProbeToken{present: true, authID: 0x1111, sid: "S-1-A"}}
	accountB := wmiProbeResult{StartUTC: "2026-11-21T10:05:00.000Z", ConnectHR: 0x80070005, Before: wmiProbeToken{present: true, authID: 0x2222, sid: "S-1-B"}}
	if got := wmiProbeIdentitySwitchObservation(accountA); got != "" {
		t.Fatalf("first run in a process must not observe a switch, got %q", got)
	}
	if got := wmiProbeIdentitySwitchObservation(accountA); got != "" {
		t.Fatalf("same identity as the previous run must not observe a switch, got %q", got)
	}
	got := wmiProbeIdentitySwitchObservation(accountB)
	if !strings.Contains(got, "identity-switch observation") ||
		!strings.Contains(got, "S-1-A") || !strings.Contains(got, "S-1-B") ||
		!strings.Contains(got, "0x80070005") {
		t.Fatalf("switch observation must name both identities and the current first connect, got %q", got)
	}
	if !strings.Contains(got, "never establishes that switching between accounts behaves correctly") {
		t.Fatalf("observation must keep the reuse caveat, got %q", got)
	}
	process := wmiProbeResult{StartUTC: "2026-11-21T10:10:00.000Z", ConnectHR: 0, Before: wmiProbeToken{}}
	if got := wmiProbeIdentitySwitchObservation(process); !strings.Contains(got, "process identity") {
		t.Fatalf("switching to the process identity must observe too, got %q", got)
	}
}

func TestWMIProbeLifecycleNotRunRemainingStepsMarksEveryRemainingConnect(t *testing.T) {
	steps := []wmiProbeLifecycleStep{{label: "A1"}, {label: "B"}, {label: "A2"}}
	result := wmiProbeResult{}
	result.notRunRemainingSteps(steps, 1, "identity handling failed")
	seen := map[string]bool{}
	for _, stage := range result.Stages {
		if stage.name == "connect@B" || stage.name == "connect@A2" {
			if !stage.notRun {
				t.Fatalf("%s must be marked not_run, got %+v", stage.name, stage)
			}
			seen[stage.name] = true
		}
		if stage.name == "connect@A1" {
			t.Fatalf("steps before the failure index must stay untouched, got %+v", stage)
		}
	}
	if len(seen) != 2 {
		t.Fatalf("both remaining steps must be marked, got %v", seen)
	}
}

func TestWmiProbeVerdictsInvalidatedByCleanupFailure(t *testing.T) {
	// Every verdict must refuse to read HRESULTs whose run failed its
	// identity-cleanup enforcement, before any other branch.
	cases := []struct {
		name   string
		veract string
	}{
		{"wmiProbeVerdict", "default"},
		{"wmiProbeHeldVerdict", "held"},
		{"wmiProbeCycledVerdict", "cycled"},
		{"wmiProbeSwitchVerdict", "switch"},
		{"wmiProbeDualVerdict", "dual"},
	}
	before := wmiProbeToken{present: true, authID: 0x1111, sid: "S-1-A"}
	run := wmiProbeResult{
		ConnectHR: 0, SecondConnectHR: 0x80070005, ThirdConnectHR: 0,
		Before: before, StepBefore: []wmiProbeToken{before, {}, before},
		CleanupFailed: true,
	}
	for _, c := range cases {
		var verdict string
		switch c.veract {
		case "default":
			verdict = wmiProbeVerdict("fixture", run, run)
		case "held":
			verdict = wmiProbeHeldVerdict("fixture", run)
		case "cycled":
			verdict = wmiProbeCycledVerdict("fixture", run)
		case "switch":
			verdict = wmiProbeSwitchVerdict("fixture", run)
		case "dual":
			verdict = wmiProbeDualVerdict("fixture", run)
		}
		if !strings.HasPrefix(verdict, "verdict invalid") && !strings.Contains(verdict, "verdict invalid") {
			t.Fatalf("%s must lead with the cleanup-invalid verdict, got %q", c.name, verdict)
		}
		if !strings.Contains(verdict, "identity-cleanup enforcement") {
			t.Fatalf("%s verdict must name the enforcement failure, got %q", c.name, verdict)
		}
	}
	// Without the flag, the same run keeps its normal branch structure.
	if verdict := wmiProbeSwitchVerdict("fixture", wmiProbeResult{
		ConnectHR: 0, SecondConnectHR: 0x80070005, ThirdConnectHR: 0,
		StepBefore: []wmiProbeToken{before, {}, before},
	}); !strings.HasPrefix(verdict, "verdict: the A → B → A sequence behaved as correct identity switching") {
		t.Fatalf("without cleanup failure the switch verdict must read the sequence, got %q", verdict)
	}
}

func TestWMIProbeVerdictDoesNotClaimTokenStayedUnchanged(t *testing.T) {
	before := wmiProbeToken{present: true, authID: 42, sid: "S-1-5-18", modifiedID: 1}
	after := before
	after.modifiedID = 2
	first := wmiProbeResult{Before: before, After: after}
	second := wmiProbeResult{ConnectHR: 0x80070005, Before: before, After: before}
	verdict := wmiProbeVerdict("fixture", first, second)
	if !strings.Contains(verdict, "cause undetermined") || strings.Contains(verdict, "neither attempt's token changed") {
		t.Fatalf("verdict exceeds observed evidence: %s", verdict)
	}
}

func TestWMIProbeHeldStagesRemainSeparate(t *testing.T) {
	result := wmiProbeResult{}
	result.stageHR("query@1", 0, 0, "first query succeeded")
	result.stageHR("connect@2", 0x80070005, 0, "second connect denied")
	result.markRemainingNotRun("connect@2", "@2")
	for _, stage := range result.Stages {
		if stage.name == "query@2" {
			if !stage.notRun || stage.hasHR {
				t.Fatalf("first sequence's success must not become a second query result: %+v", stage)
			}
			return
		}
	}
	t.Fatal("missing second query's not_run stage")
}

//go:build windows

package main

import (
	"strings"
	"testing"
)

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

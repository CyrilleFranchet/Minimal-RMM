package main

import (
	"testing"
)

// TestCocreateRetryDelay pins the retry policy of the local WbemLocator
// activation: exactly the transient ERROR_KEY_DELETED HRESULT follows the
// bounded schedule, and nothing else is ever retried.
func TestCocreateRetryDelay(t *testing.T) {
	for attempt := 0; attempt < len(wmiCocreateRetryDelays); attempt++ {
		delay, retry := cocreateRetryDelay(hrErrorKeyDeleted, attempt)
		if !retry {
			t.Fatalf("attempt %d must retry the transient HRESULT", attempt)
		}
		if delay != wmiCocreateRetryDelays[attempt] {
			t.Fatalf("attempt %d delay = %s, want %s", attempt, delay, wmiCocreateRetryDelays[attempt])
		}
	}
	if _, retry := cocreateRetryDelay(hrErrorKeyDeleted, len(wmiCocreateRetryDelays)); retry {
		t.Fatal("the schedule must be bounded: the attempt after the last delay must fail")
	}
	// Deterministic failures must surface on the first attempt.
	deterministic := []uintptr{
		0x80040154, // REGDB_E_CLASSNOTREG
		0x80070005, // E_ACCESSDENIED
		0x80004003, // E_POINTER
		0x8000FFFF, // E_UNEXPECTED
		1,
	}
	for _, hr := range deterministic {
		if _, retry := cocreateRetryDelay(hr, 0); retry {
			t.Fatalf("HRESULT 0x%08x must not be retried", hr)
		}
	}
	if _, retry := cocreateRetryDelay(0, 0); retry {
		t.Fatal("success must not be retried")
	}
}

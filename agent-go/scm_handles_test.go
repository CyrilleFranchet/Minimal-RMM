package main

import (
	"errors"
	"reflect"
	"testing"
)

func TestSCMRetainedHandlesDeleteAndCloseOrder(t *testing.T) {
	var calls []uintptr
	h := &scmServiceHandles{
		manager: 11, service: 22,
		deleteService: func(handle uintptr) error {
			calls = append(calls, handle+100)
			return nil
		},
		closeHandle: func(handle uintptr) error {
			calls = append(calls, handle)
			return nil
		},
	}
	deleted, err := h.delete()
	if !deleted || err != nil {
		t.Fatalf("delete failed: %t %v", deleted, err)
	}
	if err := h.close(); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(calls, []uintptr{122, 22, 11}) {
		t.Fatalf("expected delete, service close, manager close exactly once: %v", calls)
	}
	if h.service != 0 || h.manager != 0 {
		t.Fatal("successful cleanup retained a handle")
	}
}

func TestSCMRetainedHandlesFailures(t *testing.T) {
	deleteFailure := errors.New("delete denied")
	closeFailure := errors.New("close failed")
	for _, scenario := range []struct {
		name       string
		deleteErr  error
		closeFails uintptr
	}{
		{"delete", deleteFailure, 0},
		{"service close", nil, 22},
		{"manager close", nil, 11},
		{"delete and close", deleteFailure, 22},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			var closed []uintptr
			failed := false
			h := &scmServiceHandles{
				manager: 11, service: 22,
				deleteService: func(uintptr) error { return scenario.deleteErr },
				closeHandle: func(handle uintptr) error {
					closed = append(closed, handle)
					if handle == scenario.closeFails && !failed {
						failed = true
						return closeFailure
					}
					return nil
				},
			}
			deleted, err := h.delete()
			if deleted != (scenario.deleteErr == nil) || err == nil {
				t.Fatalf("incorrect partial result: deleted=%t err=%v", deleted, err)
			}
			if scenario.deleteErr != nil && !errors.Is(err, deleteFailure) {
				t.Fatalf("delete failure was lost: %v", err)
			}
			if scenario.closeFails != 0 && !errors.Is(err, closeFailure) {
				t.Fatalf("close failure was lost: %v", err)
			}
			if !reflect.DeepEqual(closed, []uintptr{22, 11}) {
				t.Fatalf("failure skipped a close attempt: %v", closed)
			}
			if scenario.closeFails == 22 && h.service != 22 || scenario.closeFails == 11 && h.manager != 11 {
				t.Fatal("failed handle was forgotten before retry")
			}
			if err := h.close(); err != nil {
				t.Fatalf("retry failed: %v", err)
			}
			want := []uintptr{22, 11}
			if scenario.closeFails != 0 {
				want = append(want, scenario.closeFails)
			}
			if !reflect.DeepEqual(closed, want) || h.service != 0 || h.manager != 0 {
				t.Fatalf("retry must close only failed handles: %v", closed)
			}
		})
	}
}

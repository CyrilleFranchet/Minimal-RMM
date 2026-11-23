package main

import "time"

// hrErrorKeyDeleted is HRESULT_FROM_WIN32(ERROR_KEY_DELETED): Win32 error
// 1018 (0x3FA), "illegal operation attempted on a registry key that has been
// marked for deletion". Local COM class resolution reads the registry through
// the merged machine and per-user classes view, and one of those reads hit a
// marked-for-deletion key. The failure is purely local: it happens before any
// network traffic, the destination host contributes nothing, and it is
// neither a COM initialization failure nor an authentication failure. A
// deleted key is not removed until the last handle to it closes
// (RegDeleteKey), so the tombstone can outlive any bounded retry schedule:
// exhausted retries never imply a different cause, and which key was
// tombstoned — the per-user classes hive, a machine classes key, or a handle
// cached against a stale view — cannot be read out of the HRESULT. That
// question takes registry-level evidence; see
// docs/wmi-key-deleted-diagnosis.md.
const hrErrorKeyDeleted uintptr = 0x800703FA

// wmiCocreateRetryDelays bounds the retry schedule of the local WbemLocator
// activation. CoCreateInstance(CLSID_WbemLocator) runs before any connection
// to the destination, so these sleeps only ever extend the pre-connection
// phase of a deployment; the worst case stays small next to the operator's
// deployment deadlines.
var wmiCocreateRetryDelays = []time.Duration{250 * time.Millisecond, time.Second, 3 * time.Second}

// cocreateRetryDelay reports the delay before the next CoCreateInstance
// attempt after an activation returned hr, where attempt counts the retries
// already spent. Only HRESULT_FROM_WIN32(ERROR_KEY_DELETED) is retried:
// every other activation failure is deterministic at this layer (missing
// class registration, class-store access, invalid arguments) and retrying it
// would only delay the operator's real error. The returned retry flag goes
// false once the bounded schedule is exhausted.
func cocreateRetryDelay(hr uintptr, attempt int) (delay time.Duration, retry bool) {
	if hr != hrErrorKeyDeleted || attempt >= len(wmiCocreateRetryDelays) {
		return 0, false
	}
	return wmiCocreateRetryDelays[attempt], true
}

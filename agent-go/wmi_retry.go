package main

import "time"

// hrErrorKeyDeleted is HRESULT_FROM_WIN32(ERROR_KEY_DELETED): local COM
// class resolution read a registry key marked for deletion. CoCreateInstance
// resolves the CLSID through the merged machine and per-user classes view,
// and the per-user half tracks the caller's profile: a profile hive unloading
// underneath a long-lived process (an in-memory agent deployed over WinRM
// runs with the user's token without owning the profile) leaves keys in the
// delete-pending state for as long as the registry handles pinning them stay
// open. The failure is purely local and transient: it happens before any
// network traffic, the destination host contributes nothing, and a later
// call ordinarily succeeds once the deferred deletion completes. It is not a
// COM initialization failure and not an authentication failure.
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

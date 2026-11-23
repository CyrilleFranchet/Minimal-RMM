# Diagnosing 0x800703FA (ERROR_KEY_DELETED) in local COM activation

## Scope and constraints

A standalone, benign local application calls
`CoCreateInstance(CLSID_WbemLocator, …, CLSCTX_INPROC_SERVER, IID_IWbemLocator, …)`.
The call returns `0x800703FA`, including after three retries. The failure
happens during local activation, before any remote WMI connection. This
report defines how to identify the failing registry operation and what caused
its key to become unavailable, from evidence.

Standing rules for the whole investigation:

- The reproducer is standalone, read-only, and local: it never writes the
  registry and contacts no remote machine. It is separate from the
  lateral-movement loader chain, whose bounded-retry mitigation
  (`agent-go/wmi_retry.go`) is intentionally not part of this tool so the
  tool observes raw, unmitigated HRESULTs.
- No registry or policy modification is made or proposed before the failing
  key is identified with a supported mechanism.
- A "profile that was never loaded" and a "key marked for deletion" are
  distinct observations; the report never substitutes one for the other.
- Exhausted retries are not proof of a different cause, and the HRESULT
  carries no hive-scope (HKCU versus HKLM) information — both require trace
  evidence.
- Where evidence is insufficient, the report says so explicitly; conclusions
  with unknowns are labeled provisional.

## 1. Decoding the HRESULT — what it establishes

`0x800703FA` decomposes as severity `1` (failure), facility `0x0007`
(FACILITY_WIN32), code `0x03FA` = Win32 error **1018**,
`ERROR_KEY_DELETED`:

> "Illegal operation attempted on a registry key that has been marked for
> deletion." — Microsoft,
> [System Error Codes (1000-1299)](https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--1000-1299-)

| ID | Fact | Basis |
| --- | --- | --- |
| F1 | A registry operation delivered error 1018 inside the local class activation, at least once per attempt (four known attempts). | HRESULT decode; the only documented producer of 1018 is a registry operation on a key marked for deletion |
| F2 | The failure is local and pre-network. | Given; `CoCreateInstance` takes no `COSERVERINFO`, and `CLSCTX_INPROC_SERVER` activates an in-process DLL from the local registry |
| F3 | Three retries failed. | Given |
| F4 | A deleted key is not removed until the last handle to it closes, so the marked-for-deletion state lasts exactly as long as a handle pins the key. | [`RegDeleteKey`](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdeletekeya) remarks |

Corollaries of F4, stated because they are often misread:

- Persistence across retries proves only that a handle still pinned the
  tombstone during the retry window. It is not evidence of a different cause.
- The failure class is not COM initialization and not authentication:
  initialization failures surface at `CoInitializeEx` with COM status codes,
  authentication failures surface later, on the remote leg, with different
  codes.

Unknowns that no amount of HRESULT reading can resolve: which key was
affected, whether it lived under `HKCU` or `HKLM`, what marked it for
deletion and when, whether profile unloading or retained handles contributed,
and which layer generated the error.

## Facts versus hypotheses

| ID | Statement | Status | Basis / citation |
| --- | --- | --- | --- |
| F1–F4 | As above | Fact | Given + WinError.h + RegDeleteKey remarks |
| H1 | Profile unloading: a per-user classes key (`HKU\<SID>\Software\Classes\…`) became unavailable when the profile hive unloaded underneath a process that still holds the user token | Hypothesis | [LoadUserProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-loaduserprofilea) remarks: impersonation does not load the profile; [UnloadUserProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-unloaduserprofile) remarks: open handles block the unload; [HKEY_CLASSES_ROOT](https://learn.microsoft.com/en-us/windows/win32/sysinfo/hkey-classes-root-key): HKCR merges `HKLM\Software\Classes` with the user's `Software\Classes` |
| H2 | Registration replacement or deletion: an install, uninstall, or COM re-registration (MSI, `regsvr32`, repair tooling) deleted or replaced the key — machine or per-user store | Hypothesis | [HKEY_CLASSES_ROOT](https://learn.microsoft.com/en-us/windows/win32/sysinfo/hkey-classes-root-key): both stores feed the merged view; deletions leave delete-pending keys while handles pin them |
| H3 | Retained invalid handle: a handle retained across a registry-state transition (advapi32 predefined-handle cache, or a handle cached inside the COM runtime) serves an unavailable view | Hypothesis | [RegDisablePredefinedCache](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdisablepredefinedcache) remarks: the predefined HKCU handle is cached per process; with caching disabled, HKCU access operates on `HKEY_USERS\<SID>` — "or on `HKEY_USERS\.DEFAULT` if the current user's hive is not loaded" |
| H4 | Component-internal failure: the failing registry operation belongs to the WbemLocator component's own initialization, not to COM's registration lookup | Hypothesis | No documentation covers provider-internal registry access at init; only stack attribution can support or reject it |
| NC | COM initialization failure, authentication failure, destination-host fault, hive-scope or layer inference from the HRESULT | Rejected / out of scope | F1–F4 and the differential above |

## 2. Reproducer and baseline

The reproducer lives in `tools/wmi-keydeleted-repro/` (source
`keydeleted_repro.cpp`, build and run matrix in its README). It is standalone,
read-only, local, and separate from the loader chain.

**Baseline (recorded before anything else):** Windows build number, native and
process architecture, PID and TID of every reporting thread, process token
SID, elevation state, thread impersonation state, UTC millisecond timestamps,
and the exact HRESULT — the tool prints all of these per iteration, so each
failure binds to the acting identity at trace-correlation time.

**Independent reproduction check:** the tool performs the exact field-failing
call — a bare
`CoCreateInstance(CLSID_WbemLocator, CLSCTX_INPROC_SERVER, IID_IWbemLocator)`,
no credentials, no remote target — followed, on success only, by a read-only
local query (`SELECT Name FROM Win32_ComputerSystem` against local
`ROOT\CIMV2`). Two outcomes:

- The standalone activation reproduces `0x800703FA` under a controlled
  condition: the investigation proceeds on that reproduction.
- It does not: **document that limitation explicitly** — the failure then
  depends on state the standalone process does not hold, and every conclusion
  stays provisional until the original process is traced under its real
  conditions. An unreproduced failure must never be presented as explained.

## 3. Diagnostic steps

Start every capture before reproducing: the key may have been marked for
deletion long before the failure, so preceding activity must be in scope.

1. **Captures on.** Process Monitor (registry events, stack capture enabled,
   symbols configured), WPR registry/ETW capture including kernel registry
   events with call stacks, and the event logs of record (User Profile
   Service operational, MsiInstaller, Application).
2. **Reproduce and correlate.** Run the reproducer under the chosen condition
   from the run matrix; for each reported failure timestamp, find the
   corresponding failing registry operation in the trace. Preserve the
   operation, the result code (1018), the full registry path, the PID, the
   TID, and the stack — these six fields are the core artifact of the whole
   investigation.
3. **Retrieve preceding activity.** Search *backwards* from the failing
   operation for the `RegDeleteKey`/`RegDeleteTree` (or key-replacement
   sequence) on that exact path, and identify the issuing process and its
   context (installer events, registration tooling, profile unload). The
   deletion may predate the failure by an unbounded time; widen the capture
   window until the deletion is found or the search is exhausted.
4. **Locate the error's origin.** Attribute the failing operation to a layer
   using the stack and the trace ordering — registration lookup (frames in
   the COM activation runtime reading the merged class store), DLL loading
   (registry reads that belong to resolving and loading the `InprocServer32`
   image), or component initialization (frames inside the component's own
   initialization performing its own registry access). **A returned HRESULT
   never identifies the generating layer** — all three surfaces produce the
   same `0x800703FA`; only the stack and operation ordering discriminate.
5. **Correlate identity and profile events.** Bind the failing operation to
   the reproducer's logged token SID and elevation at the same timestamp, and
   to User Profile Service events in the window. Distinguish the two states
   explicitly: a **profile never loaded** shows as
   [RegOpenUserClassesRoot](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regopenuserclassesroot)
   failing for the token (its documented behavior when the profile is not
   loaded) with no hive mounted under `HKU\<SID>`; a **key marked for
   deletion** shows as a specific key returning 1018 while the hive is
   otherwise reachable. Record which one the evidence shows.
6. **Test the handle hypothesis.** Compare a failing iteration's operations
   with a fresh lookup of the same path: if the failure surfaces with **no
   new open operation for that path** in the trace, the access rode an
   existing (retained) handle; if a fresh `RegOpenKeyEx` on the path returns
   1018, the key itself is tombstoned. These are different mechanisms with
   different evidence — never collapse them.
7. **Controlled comparisons** (below) for the hypothesis under test.
8. **Classify** with the decision table, and state the causal finding or its
   provisional status (both below).

## 4. Competing explanations and the evidence needed

| Hypothesis | Evidence needed to support | Additional discriminating evidence |
| --- | --- | --- |
| Profile unloading (H1) | The failing key lies under the caller's `HKU\<SID>\…`, plus evidence connecting the key's unavailability to a profile-unload operation in the window | The acting token keeps the SID while the hive state changes; reproducer's `HKU\<SID>` probe diverges in the same window |
| Registration replacement or deletion (H2) | A preceding modification affecting the same key — the deleting process, its trigger, and its event-log context identified in step 3 | Works for both stores; for the machine store, installer/registration events and no profile correlation |
| Retained invalid handle (H3) | Evidence distinguishing access through an existing handle from a fresh lookup (step 6) | The `--no-hkcu-cache` A/B run changing the outcome supports the advapi32 predefined-cache variant; an unchanged outcome does not automatically refute the class (the retained handle may live elsewhere) |
| Component-internal failure (H4) | A stack locating the failing registry operation inside component initialization (step 4) | No registration-lookup frames; the failure follows the DLL load in operation ordering |

## 5. Controlled comparisons

- Change **one variable per experiment**, on a disposable environment
  (disposable local lab account), per the run matrix in the tool README
  (R0 baseline; R1 token without profile; R2 profile load/unload cycle; R3
  logoff of the account's other session; R4 predefined-handle caching A/B).
- Always start **fresh processes** — never reuse a process across variants:
  retained handles inside a live process are themselves a hypothesis (H3).
- Record **actual API results** for every run, including the runs that do not
  fail. A difference between runs supports the hypothesis the variable
  targets; an unchanged result does not automatically refute it, because the
  failing condition may involve state outside the varied variable.

## 6. Assessment of the four API explanations

| Claim | Verdict | Documentation |
| --- | --- | --- |
| `LogonUser` does not automatically load the user profile | **Accurate.** The system loads the profile at interactive logon; impersonation does not; `CreateProcessAsUser` does not load it into HKEY_USERS either | [LoadUserProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-loaduserprofilea) remarks |
| `CLSCTX_NO_CUSTOM_MARSHAL` does not bypass HKCU | **Accurate.** The value (0x1000) means "activation fails if it uses custom marshaling"; nothing in `CLSCTX` selects registration scope or the merged view | [CLSCTX enumeration](https://learn.microsoft.com/en-us/windows/win32/api/wtypesbase/ne-wtypesbase-clsctx) |
| `COSERVERINFO` does not select HKLM-only registration | **Accurate.** It identifies a remote machine for remote activation; `CoCreateInstance` accepts no such parameter; a local in-proc activation resolves through the local registry regardless | [COSERVERINFO](https://learn.microsoft.com/en-us/windows/win32/api/objidl/ns-objidl-coserverinfo); [CoGetClassObject](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-cogetclassobject) |
| `RegDisablePredefinedCache` controls handle caching, not registry merging | **Accurate.** It disables the per-process caching of the predefined HKCU handle; HKCU access then operates on `HKEY_USERS\<SID>` — or `.DEFAULT` when the hive is not loaded; the HKCR merged view is untouched | [RegDisablePredefinedCache](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdisablepredefinedcache); [RegDisablePredefinedCacheEx](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdisablepredefinedcacheex) |

Consequence: none of the four is a cause or a fix on its own. `LogonUser` and
the predefined cache describe conditions and measurement switches; the other
two describe mechanisms that do not touch this failure. Any proposal built on
them as remedies is unsupported.

## 7. Decision table

| Observation (from steps 2–6) | Conclusion |
| --- | --- |
| Failing key under `HKU\<SID>\…`; a profile-unload operation in the window explains its unavailability; acting token keeps the SID | **H1 supported** — report the profile unload as the triggering event |
| A preceding modification (delete, replace) on the same key by an identified process/installer; no profile correlation needed | **H2 supported** — report the modification and its trigger as the cause |
| Failure surfaces with no fresh open operation on the path; the `--no-hkcu-cache` A/B changes the outcome | **H3 supported (predefined cache variant)** — remeasure with caching disabled |
| Failure surfaces through a retained handle unaffected by R4, with fresh lookups of the same path succeeding | **H3 supported (non-advapi32 retained handle)** — locate the handle owner from the stack |
| Stack locates the failing registry operation inside component initialization; registration lookup completed cleanly | **H4 supported** — the error belongs to the component, not to activation |
| Probes show the hive not mounted and `RegOpenUserClassesRoot` failing for the token, with no 1018 in traces | The condition is a **profile never loaded** — a distinct failure class; do not treat it as a tombstone |
| Failure reproduces or persists but no 1018 operation is captured, or attribution is inconclusive | **Evidence insufficient** — say so explicitly; extend continuous capture and re-run; no registry or policy change |

## 8. Causal finding and completion criteria

The investigation is complete only when the report states, with trace
evidence: the **exact failing operation**, the **affected key** (full path,
hence hive scope), the **responsible component** (layer attribution from the
stack), the **triggering event** (what marked the key, by which process), and
the **reproduction conditions** (which run-matrix condition, consistently).

If any element remains unknown, the conclusion is **provisional** and is
labeled as such, listing exactly what is missing.

Completion criterion: a benign reproduction plus its trace explain the failure
consistently. A successful retry, or a temporal correlation alone, is
insufficient: temporal co-occurrence with a profile unload is not causation,
and a call that later succeeds does not retroactively identify the key or the
deleter.

Companion implementation notes for the agent-side bounded retry, including
why the retry proves nothing about the cause, live in
[wmi-com-lifecycle.md](wmi-com-lifecycle.md).

## References

- [System Error Codes (1000-1299)](https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--1000-1299-) — ERROR_KEY_DELETED 1018 (0x3FA)
- [RegDeleteKey](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdeletekeya) — "A deleted key is not removed until the last handle to it is closed"
- [LoadUserProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-loaduserprofilea) — profile loading rules for logon, impersonation, and `CreateProcessAsUser`
- [UnloadUserProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-unloaduserprofile) — open handles block the unload
- [HKEY_CLASSES_ROOT Key](https://learn.microsoft.com/en-us/windows/win32/sysinfo/hkey-classes-root-key) — merged per-user and per-machine classes view, user priority, non-interactive access, elevation behavior
- [RegOpenUserClassesRoot](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regopenuserclassesroot) — fails when the profile is not loaded
- [RegDisablePredefinedCache](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdisablepredefinedcache) and [RegDisablePredefinedCacheEx](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regdisablepredefinedcacheex) — predefined-handle caching
- [CLSCTX enumeration](https://learn.microsoft.com/en-us/windows/win32/api/wtypesbase/ne-wtypesbase-clsctx) — CLSCTX_NO_CUSTOM_MARSHAL
- [COSERVERINFO](https://learn.microsoft.com/en-us/windows/win32/api/objidl/ns-objidl-coserverinfo)
- [CoGetClassObject](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-cogetclassobject)
- [CoCreateInstance](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-cocreateinstance)
- [RegOpenKeyEx](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regopenkeyexa)
- [Process Monitor](https://learn.microsoft.com/en-us/sysinternals/downloads/process-monitor)

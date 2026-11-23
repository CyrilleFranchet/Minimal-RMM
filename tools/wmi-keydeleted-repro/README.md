# wmi-keydeleted-repro

Standalone, read-only local COM reproducer for investigating
`HRESULT 0x800703FA` (`HRESULT_FROM_WIN32(ERROR_KEY_DELETED)`, Win32 error
1018) returned by
`CoCreateInstance(CLSID_WbemLocator, ..., CLSCTX_INPROC_SERVER, IID_IWbemLocator, ...)`
in a long-lived process. Lab diagnostic tool for authorized use on a
disposable Windows environment.

It is deliberately **separate from the Minimal-RMM lateral-movement loader
chain** and shares no code with it: the loader keeps its bounded-retry
mitigation (`agent-go/wmi_retry.go`), while this tool records raw HRESULTs
without retrying or interpreting anything, so a failure it observes can be
correlated with external traces. Procedure and decision tables:
[ERROR_KEY_DELETED diagnostic report](../../docs/wmi-key-deleted-diagnosis.md).

## Safety contract

- **Read-only**: every registry probe opens with `KEY_READ` and closes the
  handle immediately; the program writes no registry value, creates no file,
  and registers nothing.
- **Local only**: the WMI connection targets the local `ROOT\CIMV2` namespace;
  the only query is a read-only `SELECT Name FROM Win32_ComputerSystem`.
- **One intentional behavior switch**: `--no-hkcu-cache` calls
  `RegDisablePredefinedCacheEx` in this process — a documented, process-local
  handle-caching control used for A/B comparison, not a registry or policy
  change.

## Build

Windows x64 Native Tools command prompt:

```text
cl /nologo /W4 /EHsc keydeleted_repro.cpp
```

MinGW-w64 alternative (GCC ignores the MSVC-only `#pragma comment(lib, …)`
lines in the source, hence the explicit `-l` flags):

```text
g++ -std=c++17 -Wall -Wextra -O2 -o keydeleted_repro.exe keydeleted_repro.cpp -lole32 -loleaut32 -ladvapi32 -lwbemuuid -Wno-unknown-pragmas
```

The source is validated by cross-compilation (mingw-w64, `-Wall -Wextra`,
zero warnings outside the intentional MSVC pragmas) before every commit.

## Usage

```text
keydeleted_repro.exe [interval_seconds] [--iterations N] [--no-hkcu-cache]
```

Defaults: 5-second interval, unlimited iterations (Ctrl+C to stop). Each
iteration prints, with UTC millisecond timestamps:

- identity: PID, TID, process token SID, elevation, thread impersonation state;
- read-only scope probes: `HKCU\Software\Classes\CLSID\{4590F811-…}` and the
  `HKLM` twin, `RegOpenUserClassesRoot`, and the mount state of the caller's
  `HKU\<SID>` hive;
- the exact field-failing call — `cocreate-wbemlocator`;
- on success: local `root\cimv2` connect, the read-only query, one row.

The baseline block (printed once) records the Windows build number, native
architecture, process architecture, and PID.

## Run matrix for controlled comparisons

Change one variable per experiment, on a disposable local account; always
start a fresh process and record the actual API results. A difference between
runs supports a hypothesis; an unchanged result does not automatically refute
one (the failing condition may involve state outside the varied variable).

| Run | Condition | Variable |
| --- | --- | --- |
| R0 | Baseline interactive session | reference |
| R1 | Token from `LogonUser`/`CreateProcessAsUser`, profile never loaded (documented Windows behavior) | profile lifetime |
| R2 | R1 plus a `LoadUserProfile`/`UnloadUserProfile` cycle while the probe lives | hive lifetime vs process lifetime |
| R3 | Probe alive during logoff of a second interactive session of the same account | suspected field condition |
| R4 | `--no-hkcu-cache` variant of any of the above | predefined-handle caching |

## Capturing

Start all captures **before** reproducing — the key may have been marked for
deletion long before the failure, so preceding activity must be in scope:

- Process Monitor, registry events for this process and any process touching
  the identified path, with stack capture enabled;
- WPR registry/ETW capture including kernel registry events with call stacks;
- Event logs: User Profile Service operational, MsiInstaller, Application.

The goal is to preserve, for the failing operation: the operation, result
code, full registry path, PID, TID, timestamp, and stack — then locate the
deletion of that key in the preceding activity and attribute the failing
access to a layer (COM registration lookup, DLL loading, or component
initialization). See the decision table in the
[diagnostic report](../../docs/wmi-key-deleted-diagnosis.md) for mapping
observations to conclusions.

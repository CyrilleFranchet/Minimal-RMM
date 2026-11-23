// keydeleted_repro.cpp — standalone, read-only local COM reproducer for
// HRESULT 0x800703FA (ERROR_KEY_DELETED: Win32 error 1018) observed during
// CoCreateInstance(CLSID_WbemLocator, ..., CLSCTX_INPROC_SERVER,
// IID_IWbemLocator, ...).
//
// Lab diagnostic tool for authorized use on a disposable Windows
// environment. It is deliberately separate from the Minimal-RMM
// lateral-movement loader chain and shares no code with it.
//
// Safety contract:
//   - Read-only: it never writes the registry, creates no files, and
//     registers nothing. Every registry probe opens with KEY_READ and closes
//     the handle immediately.
//   - Local only: the WMI connection targets the local ROOT\CIMV2 namespace;
//     no remote machine is contacted.
//   - The only intentional behavior switch is --no-hkcu-cache, which calls
//     RegDisablePredefinedCacheEx in this process (documented, process-local
//     handle-caching control, not a registry change) for A/B comparison.
//
// Build (Windows, x64 Native Tools command prompt):
//   cl /nologo /W4 /EHsc keydeleted_repro.cpp
//
// Usage:
//   keydeleted_repro.exe [interval_seconds] [--iterations N] [--no-hkcu-cache] [--forever]
//
// The iteration count is bounded by default (60 iterations): an unbounded
// loop has no diagnostic value over a bounded run, and a run launched
// through a management channel that cannot deliver Ctrl+C would otherwise
// block that channel until its watchdog fires. --forever opts back into the
// long-lived observation mode for interactive consoles only.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <objbase.h>
#include <oleauto.h>
#include <wbemidl.h>
#include <sddl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "wbemuuid.lib")

namespace {

// ---------------------------------------------------------------- helpers

void PrintTimestampPrefix(void) {
    SYSTEMTIME st;
    GetSystemTime(&st);
    printf("[%04u-%02u-%02uT%02u:%02u:%02u.%03uZ] ",
           st.wYear, st.wMonth, st.wDay,
           st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
}

void PrintHexLine(const char* label, HRESULT hr) {
    PrintTimestampPrefix();
    printf("%s = 0x%08lX\n", label, static_cast<unsigned long>(hr));
}

void PrintHexLineWithDetail(const char* label, HRESULT hr, const char* detail) {
    PrintTimestampPrefix();
    printf("%s = 0x%08lX (%s)\n", label, static_cast<unsigned long>(hr), detail);
}

// ------------------------------------------------------------- baseline

void PrintBaseline(void) {
    // Windows build: RtlGetVersion is not subject to the compatibility
    // manifest lie that GetVersionEx can apply.
    typedef LONG(WINAPI* RtlGetVersionFn)(PRTL_OSVERSIONINFOW);
    RTL_OSVERSIONINFOW info;
    ZeroMemory(&info, sizeof(info));
    info.dwOSVersionInfoSize = sizeof(info);
    if (HMODULE ntdll = GetModuleHandleW(L"ntdll.dll")) {
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wcast-function-type"
#endif
        if (RtlGetVersionFn fn =
                reinterpret_cast<RtlGetVersionFn>(GetProcAddress(ntdll, "RtlGetVersion"))) {
#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif
            fn(&info);
        }
    }
    PrintTimestampPrefix();
    printf("baseline windows-build %lu.%lu.%lu\n",
           info.dwMajorVersion, info.dwMinorVersion, info.dwBuildNumber);

    SYSTEM_INFO si;
    GetNativeSystemInfo(&si);
    const char* arch = "unknown";
    if (si.wProcessorArchitecture == PROCESSOR_ARCHITECTURE_AMD64) {
        arch = "x64";
    } else if (si.wProcessorArchitecture == PROCESSOR_ARCHITECTURE_ARM64) {
        arch = "arm64";
    } else if (si.wProcessorArchitecture == PROCESSOR_ARCHITECTURE_INTEL) {
        arch = "x86";
    }
    PrintTimestampPrefix();
    printf("baseline process-arch %s (%s process)\n", arch,
#ifdef _WIN64
           "64-bit"
#else
           "32-bit"
#endif
    );

    PrintTimestampPrefix();
    printf("baseline pid %lu tid %lu\n",
           GetCurrentProcessId(), GetCurrentThreadId());
}

// ------------------------------------------------------------- probes

void PrintOpenProbe(const char* label, HKEY root, const char* subkey) {
    HKEY key = nullptr;
    const LSTATUS rc = RegOpenKeyExA(root, subkey, 0, KEY_READ, &key);
    if (rc == ERROR_SUCCESS) {
        RegCloseKey(key);
    }
    PrintTimestampPrefix();
    printf("probe %s open-key 0x%08lX (%ld)\n", label,
           static_cast<unsigned long>(rc), static_cast<long>(rc));
}

void PrintScopeProbes(void) {
    // Identity: process token user SID and elevation, plus thread
    // impersonation state — the fields required to correlate the failure
    // with the acting identity at trace time.
    HANDLE token = nullptr;
    std::string sidText = "<unavailable>";
    DWORD elevated = 0;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        DWORD size = 0;
        GetTokenInformation(token, TokenUser, nullptr, 0, &size);
        if (size > 0) {
            if (BYTE* buffer = static_cast<BYTE*>(LocalAlloc(LPTR, size))) {
                if (GetTokenInformation(token, TokenUser, buffer, size, &size)) {
                    const TOKEN_USER* user = reinterpret_cast<const TOKEN_USER*>(buffer);
                    LPSTR sidString = nullptr;
                    if (ConvertSidToStringSidA(user->User.Sid, &sidString)) {
                        sidText = sidString;
                        LocalFree(sidString);
                    }
                }
                LocalFree(buffer);
            }
        }
        DWORD elevationSize = 0;
        TOKEN_ELEVATION elevation{};
        if (GetTokenInformation(token, TokenElevation, &elevation, sizeof(elevation), &elevationSize)) {
            elevated = elevation.TokenIsElevated;
        }
        CloseHandle(token);
    }
    PrintTimestampPrefix();
    printf("identity pid %lu tid %lu sid %s elevated %lu\n",
           GetCurrentProcessId(), GetCurrentThreadId(), sidText.c_str(), elevated);

    HANDLE threadToken = nullptr;
    if (OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &threadToken)) {
        CloseHandle(threadToken);
        PrintTimestampPrefix();
        printf("identity thread-impersonation present\n");
    } else {
        PrintTimestampPrefix();
        printf("identity thread-impersonation absent (%lu)\n", GetLastError());
    }

    // Scope probes, all read-only, one handle at a time, closed immediately:
    // user hive mount state, per-user classes key, machine classes key, and
    // the documented per-user merged view accessor.
    PrintOpenProbe("hkcu-classes-clsid", HKEY_CURRENT_USER,
                   "Software\\Classes\\CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}");
    PrintOpenProbe("hklm-classes-clsid", HKEY_LOCAL_MACHINE,
                   "SOFTWARE\\Classes\\CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}");

    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        HKEY userClasses = nullptr;
        const LSTATUS rc = RegOpenUserClassesRoot(token, 0, KEY_READ, &userClasses);
        if (rc == ERROR_SUCCESS) {
            RegCloseKey(userClasses);
        }
        PrintTimestampPrefix();
        printf("probe reg-open-user-classes-root 0x%08lX (%ld)\n",
               static_cast<unsigned long>(rc), static_cast<long>(rc));
        CloseHandle(token);
    }

    if (sidText != "<unavailable>") {
        PrintOpenProbe("hku-user-hive", HKEY_USERS, sidText.c_str());
    }
}

// ------------------------------------------------------------ iteration

// RunIteration performs the exact field-failing call, then — only on
// success — a local read-only WMI query. Every HRESULT is printed raw; the
// program never interprets, retries, or summarizes.
void RunIteration(void) {
    PrintScopeProbes();

    IWbemLocator* locator = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_WbemLocator, nullptr, CLSCTX_INPROC_SERVER,
                                 IID_IWbemLocator, reinterpret_cast<void**>(&locator));
    PrintHexLine("cocreate-wbemlocator", hr);
    if (FAILED(hr) || locator == nullptr) {
        if (locator) locator->Release();
        return;
    }

    IWbemServices* services = nullptr;
    BSTR namespaceName = SysAllocString(L"ROOT\\CIMV2");
    if (namespaceName == nullptr) {
        PrintHexLineWithDetail("connect-local-root-cimv2", E_OUTOFMEMORY,
                               "namespace BSTR allocation failed");
        locator->Release();
        return;
    }
    hr = locator->ConnectServer(namespaceName, nullptr, nullptr, nullptr, 0,
                                nullptr, nullptr, &services);
    SysFreeString(namespaceName);
    PrintHexLineWithDetail("connect-local-root-cimv2", hr,
                           "read-only local query, no remote target");
    if (FAILED(hr) || services == nullptr) {
        if (services) services->Release();
        locator->Release();
        return;
    }

    BSTR language = SysAllocString(L"WQL");
    BSTR query = SysAllocString(L"SELECT Name FROM Win32_ComputerSystem");
    IEnumWbemClassObject* enumerator = nullptr;
    hr = services->ExecQuery(language, query,
                             WBEM_FLAG_FORWARD_ONLY | WBEM_FLAG_RETURN_IMMEDIATELY,
                             nullptr, &enumerator);
    PrintHexLine("execquery-win32-computersystem", hr);

    if (SUCCEEDED(hr)) {
        IWbemClassObject* object = nullptr;
        ULONG returned = 0;
        hr = enumerator->Next(WBEM_INFINITE, 1, &object, &returned);
        PrintHexLineWithDetail("enum-next", hr, returned == 1 ? "row read" : "no row");
        if (SUCCEEDED(hr) && object != nullptr && returned == 1) {
            VARIANT value;
            VariantInit(&value);
            hr = object->Get(L"Name", 0, &value, nullptr, nullptr);
            if (SUCCEEDED(hr) && value.vt == VT_BSTR) {
                PrintTimestampPrefix();
                printf("query-result name %ls\n", value.bstrVal);
            }
            VariantClear(&value);
        }
        if (object) object->Release();
    }

    if (enumerator) enumerator->Release();
    SysFreeString(query);
    SysFreeString(language);
    services->Release();
    locator->Release();
}

}  // namespace

int main(int argc, char** argv) {
    DWORD intervalMs = 5000;
    DWORD iterations = 60;  // bounded default; --forever opts into unlimited
    bool forever = false;
    bool disableHkcuCache = false;

    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--forever") {
            forever = true;
        } else if (arg == "--no-hkcu-cache") {
            disableHkcuCache = true;
        } else if (arg == "--iterations" && i + 1 < argc) {
            iterations = static_cast<DWORD>(strtoul(argv[++i], nullptr, 10));
        } else {
            intervalMs = static_cast<DWORD>(strtoul(argv[i], nullptr, 10) * 1000);
            if (intervalMs == 0) {
                intervalMs = 5000;
            }
        }
    }

    PrintBaseline();
    if (disableHkcuCache) {
        // Documented process-local switch: disables handle caching for the
        // predefined registry handles; HKCU access then operates directly on
        // HKEY_USERS\<SID> (or .DEFAULT when that hive is not loaded). This
        // is a measurement condition for A/B comparison, not a fix.
        const LSTATUS rc = RegDisablePredefinedCacheEx();
        PrintTimestampPrefix();
        printf("baseline reg-disable-predefined-cache-ex 0x%08lX (%ld)\n",
               static_cast<unsigned long>(rc), static_cast<long>(rc));
    }

    // The COM apartment is initialized once and released only on a normal
    // finite exit, mirroring the long-lived process condition of the field
    // failures: an interrupted run keeps the apartment until termination.
    const HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    PrintHexLine("coinitialize-mta", hr);
    if (FAILED(hr)) {
        return 1;
    }

    for (DWORD i = 0; forever || i < iterations; ++i) {
        PrintTimestampPrefix();
        printf("iteration %lu begin\n", static_cast<unsigned long>(i));
        RunIteration();
        PrintTimestampPrefix();
        printf("iteration %lu end\n", static_cast<unsigned long>(i));
        fflush(stdout);
        Sleep(intervalMs);
    }

    CoUninitialize();
    return 0;
}

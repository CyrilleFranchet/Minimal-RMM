#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <thread>
#include <windows.h>

static std::string greeting;
static thread_local std::string threadGreeting = "thread-tls-ready";
struct Initializer { Initializer() { greeting = "msvc-ready"; } };
static Initializer initializer;

extern "C" __declspec(dllexport) int RMMRequiresWindowsLoader(void) { return 1; }

extern "C" __declspec(dllexport) int Run(const char *input, char *output, int outputCap) {
    if (input == nullptr || output == nullptr || outputCap < 32) return 1;
    try {
        std::string result = greeting;
        if (std::strcmp(input, "unwind") == 0) {
            DWORD64 imageBase = 0;
            result = RtlLookupFunctionEntry(reinterpret_cast<DWORD64>(&Run), &imageBase, nullptr) ? "registered" : "missing";
        } else if (std::strcmp(input, "module") == 0) {
            HMODULE module = nullptr;
            result = GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                reinterpret_cast<LPCSTR>(&Run), &module) ? "registered" : "missing";
        } else if (std::strcmp(input, "throw") == 0) {
            throw std::runtime_error("caught");
        } else if (std::strcmp(input, "thread") == 0) {
            threadGreeting = "caller";
            std::thread worker([&result]() { result = threadGreeting; });
            worker.join();
            if (threadGreeting != "caller") return 2;
        } else if (std::strcmp(input, "fail") == 0) {
            std::snprintf(output, static_cast<size_t>(outputCap), "fixture failure");
            return 9;
        } else if (std::strcmp(input, "crash") == 0) {
            TerminateProcess(GetCurrentProcess(), 7);
        } else if (std::strncmp(input, "wait:", 5) == 0) {
            wchar_t path[4096];
            if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, input + 5, -1, path, 4096)) return 3;
            HANDLE ready = CreateFileW(path, GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (ready == INVALID_HANDLE_VALUE) return 4;
            CloseHandle(ready);
            Sleep(INFINITE);
        }
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", result.c_str());
    } catch (const std::runtime_error &error) {
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", error.what());
    }
    return 0;
}

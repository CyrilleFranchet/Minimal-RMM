#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <windows.h>

static std::string greeting;

struct Initializer {
    Initializer() { greeting = "msvc-ready"; }
};

static Initializer initializer;

extern "C" IMAGE_DOS_HEADER __ImageBase;
struct _ThrowInfo;

// MSVC's static CRT cannot discover a manually mapped image with
// RtlPcToFileHeader. Supply the image base in the C++ exception record.
extern "C" __declspec(noreturn) void __stdcall _CxxThrowException(
    void *object, _ThrowInfo *throwInfo) {
    const ULONG_PTR parameters[] = {
        0x19930520,
        reinterpret_cast<ULONG_PTR>(object),
        reinterpret_cast<ULONG_PTR>(throwInfo),
        reinterpret_cast<ULONG_PTR>(&__ImageBase),
    };
    RaiseException(0xE06D7363, EXCEPTION_NONCONTINUABLE, 4, parameters);
    __assume(0);
}

extern "C" __declspec(dllexport) int Run(const char *input, char *output, int outputCap) {
    if (output == nullptr || outputCap < 32) {
        return 1;
    }
    try {
        if (input != nullptr && std::strcmp(input, "unwind") == 0) {
            DWORD64 imageBase = 0;
            const auto entry = RtlLookupFunctionEntry(
                reinterpret_cast<DWORD64>(&Run), &imageBase, nullptr);
            std::snprintf(output, static_cast<size_t>(outputCap), "%s",
                          entry != nullptr ? "registered" : "missing");
            return 0;
        }
        if (input != nullptr && std::strcmp(input, "module") == 0) {
            void *imageBase = nullptr;
            const auto found = RtlPcToFileHeader(
                reinterpret_cast<void *>(&Run), &imageBase);
            std::snprintf(output, static_cast<size_t>(outputCap), "%s",
                          found != nullptr ? "registered" : "unregistered");
            return 0;
        }
        if (input != nullptr && std::strcmp(input, "throw") == 0) {
            throw std::runtime_error("caught");
        }
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", greeting.c_str());
    } catch (const std::runtime_error &error) {
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", error.what());
    }
    return 0;
}

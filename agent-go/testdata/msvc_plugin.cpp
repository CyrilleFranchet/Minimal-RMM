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

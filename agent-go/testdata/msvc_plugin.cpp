#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>

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
        if (input != nullptr && std::strcmp(input, "throw") == 0) {
            throw std::runtime_error("caught");
        }
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", greeting.c_str());
    } catch (const std::runtime_error &error) {
        std::snprintf(output, static_cast<size_t>(outputCap), "%s", error.what());
    }
    return 0;
}

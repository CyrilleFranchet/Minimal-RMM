#include <windows.h>

__declspec(dllexport) int Run(const char *input, char *output, int outputCap) {
    if (output == NULL || outputCap < 16) return 1;
    lstrcpynA(output, "legacy-ready", outputCap);
    return 0;
}

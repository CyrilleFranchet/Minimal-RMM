#include <windows.h>

extern IMAGE_DOS_HEADER __ImageBase;

// The static CRT cannot discover a manually mapped DLL with
// RtlPcToFileHeader. Supply this image base in its C++ exception record.
__declspec(noreturn) void __stdcall _CxxThrowException(
    void *object, const void *throwInfo) {
    const ULONG_PTR parameters[] = {
        0x19930520,
        (ULONG_PTR)object,
        (ULONG_PTR)throwInfo,
        (ULONG_PTR)&__ImageBase,
    };
    RaiseException(0xE06D7363, EXCEPTION_NONCONTINUABLE, 4, parameters);
    __assume(0);
}

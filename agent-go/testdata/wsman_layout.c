// Compile-only SDK assertions shared with the Go Windows ABI tests.
#define WSMAN_API_VERSION_1_0
#include <windows.h>
#include <wsman.h>
#include <stddef.h>

// Newer Microsoft SDK headers contain enums requiring a C++ parser. Keep
// MinGW C compilation available for host-side layout verification as well.
#ifdef __cplusplus
#define SDK_ASSERT static_assert
#else
#define SDK_ASSERT _Static_assert
#endif

SDK_ASSERT(sizeof(void *) == 8, "WinRM supports Windows amd64 and arm64");
SDK_ASSERT(sizeof(WSMAN_SHELL_ASYNC) == 16, "async size");
SDK_ASSERT(offsetof(WSMAN_SHELL_ASYNC, completionFunction) == 8, "callback offset");
SDK_ASSERT(sizeof(WSMAN_COMMAND_ARG_SET) == 16, "argument set size");
SDK_ASSERT(offsetof(WSMAN_COMMAND_ARG_SET, args) == 8, "argument pointer offset");
SDK_ASSERT(sizeof(WSMAN_STREAM_ID_SET) == 16, "stream set size");
SDK_ASSERT(sizeof(WSMAN_DATA) == 24, "data size");
SDK_ASSERT(offsetof(WSMAN_DATA, binaryData) == 8, "data union offset");
SDK_ASSERT(offsetof(WSMAN_DATA, binaryData.data) == 16, "data pointer offset");
SDK_ASSERT(sizeof(WSMAN_RECEIVE_DATA_RESULT) == 48, "receive size");
SDK_ASSERT(offsetof(WSMAN_RECEIVE_DATA_RESULT, streamData) == 8, "receive data offset");
SDK_ASSERT(offsetof(WSMAN_RECEIVE_DATA_RESULT, commandState) == 32, "state offset");
SDK_ASSERT(offsetof(WSMAN_RECEIVE_DATA_RESULT, exitCode) == 40, "exit offset");

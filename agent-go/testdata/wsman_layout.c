// Compile-only SDK assertions shared with the Go Windows ABI tests.
#define WSMAN_API_VERSION_1_0
#include <windows.h>
#include <wsman.h>
#include <stddef.h>

_Static_assert(sizeof(void *) == 8, "WinRM supports Windows amd64 and arm64");
_Static_assert(sizeof(WSMAN_SHELL_ASYNC) == 16, "async size");
_Static_assert(offsetof(WSMAN_SHELL_ASYNC, completionFunction) == 8, "callback offset");
_Static_assert(sizeof(WSMAN_COMMAND_ARG_SET) == 16, "argument set size");
_Static_assert(offsetof(WSMAN_COMMAND_ARG_SET, args) == 8, "argument pointer offset");
_Static_assert(sizeof(WSMAN_STREAM_ID_SET) == 16, "stream set size");
_Static_assert(sizeof(WSMAN_DATA) == 24, "data size");
_Static_assert(offsetof(WSMAN_DATA, binaryData) == 8, "data union offset");
_Static_assert(offsetof(WSMAN_DATA, binaryData.data) == 16, "data pointer offset");
_Static_assert(sizeof(WSMAN_RECEIVE_DATA_RESULT) == 48, "receive size");
_Static_assert(offsetof(WSMAN_RECEIVE_DATA_RESULT, streamData) == 8, "receive data offset");
_Static_assert(offsetof(WSMAN_RECEIVE_DATA_RESULT, commandState) == 32, "state offset");
_Static_assert(offsetof(WSMAN_RECEIVE_DATA_RESULT, exitCode) == 40, "exit offset");

# Go agent debug tracing

Return to the [documentation index](README.md).

Use a separate debug executable to investigate an agent that disappears during
remote service cleanup. Normal agent builds exclude the diagnostic logger,
crash-capture implementation, and diagnostic event strings. Debug builds use
the `rmmdebug` Go build tag; this is a separate artifact from the same source
tree, not a runtime switch that adds logging to the normal executable.

## Enable debug when building the main agent

Select **Debug build** in the WebUI Go generator, or use:

```text
rmm_cli agent go-build --debug --goarch amd64
```

Download and extract the returned ZIP, then deploy it as the main agent using
`run-agent.ps1`. There is no companion executable to place beside it. Spawned
children reuse the current diagnostic executable, including after renaming it.
For an arm64 source agent, build with `--goarch arm64`.

Debug builds are full-agent ZIPs. They use separate output filenames and are
excluded from the normal payload lookup used by lateral deployment. The REST
build request uses `debug: true` with `stageless: true`; CLI and WebUI select
the full-agent format automatically.

The server and source agent must both include build-mode inheritance support.
Already-running normal agents do not gain diagnostics: replace them with the
new debug build. To return to normal operation, build and deploy with debug
unchecked. Normal executables exclude the diagnostic implementation entirely.

## All descendant agents inherit the build mode

Select the main debug session and use the ordinary commands:

```text
/spawn
/spawnas -i toto
/lateral psexec <host>
/lateral wmiexec <host>
/lateral winrm <host>
```

Per-command debug flags are not accepted. Local spawn/spawnas and WMI/WinRM reuse the
source debug image. PsExec selects a debug artifact on the server and writes
`minimal-rmm-agent-debug.exe` on the target. Its current server payload lookup
supports Windows amd64: an arm64 source still needs a separate amd64 debug
build on the server for that method. Missing debug payloads fail without
falling back to a normal payload.

The agent registers its compiled build mode as `debug_build`; session metadata
exposes `agent_debug`. The server inherits that mode when selecting deployment
payloads. Deployment commands and their REST/MCP/AI interfaces have no debug
option. Diagnostics are a build-level mode that cannot be changed by command
flags or inherited environment settings.

Caller and target write separate logs on their respective hosts. Inheritance
continues when a spawned agent itself spawns or deploys another agent.

## Find the trace

The child writes `minimal-rmm-debug-<PID>.log` in its process account's profile
under `AppData\Local\Temp`. Use its registered PID or the PID in the spawn
result. If the account profile cannot be resolved, it falls back to the child's
`%TEMP%`, which may have been inherited from its parent. The startup message in
the existing agent log reports the actual path. The debug file is separate from
the existing `RMM_LOG_FILE` agent log.

Each debug record includes a timestamp and PID. Check the final records for
service polling, deletion, handle closure, identity restoration, and orderly
shutdown. Diagnostic events omit credentials, URLs, command bodies, plugin
inputs, and plugin results. Win32 failures use numeric error codes.

Builds made with Go 1.23 or later also direct Go runtime panic and crash output
to this file. Older Go builds report that runtime crash capture is unavailable.
Runtime crash output is separate from the sanitized diagnostic events. Raw
panic messages and stack arguments may contain sensitive values; treat the
file as a sensitive diagnostic artifact.

An external process kill or abrupt machine shutdown may leave only the last
completed checkpoint. The file does not establish who terminated the process.
For those cases, correlate its timestamps with Windows Application Error and
Windows Error Reporting events.

Debug builds enable tracing in the main agent, descendants, and cleanup helpers.
Debug mode has no runtime environment switch. Remove the trace after
collecting the evidence; the files are not automatically uploaded or rotated.

## Implementation reference

| Function | Purpose |
| --- | --- |
| `rejectCommandDebugOption` | Reject removed deployment JSON overrides before a command can launch an agent |
| `initAgentDebug` | Create an exclusive per-process trace and install crash capture |
| `closeAgentDebug` | Flush/close the event writer while retaining runtime crash capture |
| `debugEvent` | Write static checkpoints, UTC timestamps, PID, and sanitized error classes |
| `installDebugCrashOutput` | Use Go 1.23 runtime crash capture or report its absence in older builds |
| `debugDirectory` | Resolve the Windows process account profile temp directory, with a temp fallback |
| `lateralServiceArguments` | Request the distinct debug target filename from the C service worker |

The normal `debug_disabled.go` build supplies empty diagnostic hooks; compiler
optimization removes their calls and event strings. The `rmmdebug` build selects
the logger, account-directory resolution, and runtime-specific crash writer.
`make go-test` checks both variants and cross-builds both Windows architectures.
The Windows identity workflow also runs the diagnostic tests on Go 1.21 and
Go 1.27, including the legacy fallback and supported runtime crash capture.

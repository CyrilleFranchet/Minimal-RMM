# Go agent debug tracing

Return to the [documentation index](README.md).

Use a separate debug executable to investigate an agent that disappears during
remote service cleanup. Normal agent builds exclude the diagnostic logger,
crash-capture implementation, and diagnostic event strings. Debug builds use
the `rmmdebug` Go build tag; this is a separate artifact from the same source
tree, not a runtime switch that adds logging to the normal executable.

## Build and place the companion executable

Rebuild and redeploy the normal source agent to obtain the `spawn -d`,
`spawnas -d`, and `lateral -d`
companion selection support. An older agent does not implement it.

Build the separate debug ZIP through **Separate debug agent** in the WebUI
generator, or use:

```text
rmm_cli agent go-build --debug --goarch amd64
```

Download and extract the returned ZIP. Place
`minimal-rmm-agent-debug-windows-amd64.exe` beside the normal source agent's
executable. For an arm64 source agent, build `--goarch arm64` and use the
matching `minimal-rmm-agent-debug-windows-arm64.exe` companion.

Debug builds are full-agent ZIPs. They use separate output filenames and are
excluded from the normal payload lookup used by lateral deployment. The REST
build request uses `debug: true` with `stageless: true`; CLI and WebUI select
the full-agent format automatically.

## Start a debug agent

From the interactive CLI or WebUI shell, select the source Go session and run:

```text
/spawnas -i toto -d
/spawn -d
```

The standalone CLI accepts the same flag:

```text
rmm_cli spawnas -i toto -d --session <source-session>
```

`spawn -d` uses the current process identity; `spawnas -i toto -d` uses the
stored credential. `--debug` is an alias for `-d`. REST, MCP, and AI tools for
spawn, spawnas, and lateral accept `debug: true`. The default is false.

Select the newly spawned session and reproduce the failing operation there.
The flag selects the separate companion executable and enables tracing in
that child, including subsequent remote service
polling and cleanup. It does not turn tracing on in the parent or automatically
enable tracing in agents launched on another host.
If the companion executable is absent, the command fails instead of launching
the normal executable with debug requested.

## Deploy a debug agent laterally

```text
/lateral psexec <host> -d
/lateral wmiexec <host> -d
/lateral winrm <host> -d
```

For psexec, first build the separate debug amd64 artifact on the server. The
server stages that artifact only for a debug request; if it is absent, the
request fails without selecting the normal payload. The dedicated C service
worker uses a separate `minimal-rmm-agent-debug.exe` target filename.
The current server payload lookup for psexec supports amd64.

For wmiexec and winrm, place the matching debug companion beside the source
executable. The source stages that companion instead of its own image.
The separate debug target enables its diagnostic writer on startup.

The `-d` flag selects the target agent. It does not enable diagnostic code in
the source's normal executable. To capture the caller's remote-service cleanup
too, run `/spawn -d`, select that new session, and run `/lateral psexec <host> -d`
from it. Caller and target write separate logs on their respective hosts.

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

Debug tracing is enabled only for the requested child and its cleanup helpers.
Normal builds ignore the debug environment switch. Remove the trace after collecting
the evidence; the files are not automatically uploaded or rotated.

## Implementation reference

| Function | Purpose |
| --- | --- |
| `resolveDebugAgentExecutable` | Select the exact debug companion beside the source executable, without fallback |
| `spawnasDebugEnvironment` | Replace inherited debug flags with the explicit request |
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

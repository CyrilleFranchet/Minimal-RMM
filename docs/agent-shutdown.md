# PowerShell agent shutdown

Registration and beacon execution share an outer `finally`, so graceful exit,
server termination, or pipeline interruption stops the SOCKS worker and the
keylogger job created by that agent invocation. Other host jobs are not removed
by this finalizer.

`Stop-RmmChildProcess` closes a process handle on every path. If its directly
managed child is still running, it kills that child and waits up to five seconds
before disposal. Both `Invoke-RmmHiddenProcessWait` and the rclone progress
wrapper use this helper in `finally`; process polling yields periodically so
PowerShell can observe pipeline interruption. Temporary transfer logs retain
their existing removal behavior.

This does not guarantee cleanup after forced host termination or containment
of grandchildren spawned by operator commands. Native Windows shutdown and
console-close behavior must be verified on Windows. The executable test
`pwsh -NoProfile -File tests/test_agent_shutdown.ps1` validates running/exited
process cleanup and a child-exit race without running the beacon.

The matching SOCKS PowerShell bundle must be updated from the plugins
repository to obtain runspace cancellation and connection-finalizer fixes.

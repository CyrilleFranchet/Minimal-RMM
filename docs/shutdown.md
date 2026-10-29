# Service shutdown and resource ownership

The central server and workspace service clean up on ordinary exit, Ctrl-C,
SIGTERM, and failures after resource creation. This includes transport connector
process groups, pivot helpers, sockets, synchronization workers, and databases.
Repeated shutdown calls are safe. In the embedded server console, Ctrl-C still
cancels the current input; use EOF or `exit` to leave the console.

## Central server

`run_server` owns both listeners from the moment each bind succeeds. If the
second bind fails, the first socket is closed without calling `shutdown()` on a
server that never entered `serve_forever()`; that call would otherwise deadlock.
A `finally` block also runs if profile startup or console history saving fails.

Shutdown stops accepting requests, interrupts accepted sockets, closes event
subscribers, cancels pending session broadcasts and command waiters, and closes
the profile, TUN, SOCKS, and compiler process managers. Terminal manager closure
prevents late requests from restarting resources. Compiler groups are terminated
and reaped, including child compilers, on timeout or service shutdown.

Request workers receive up to five seconds per listener to finish after socket
cancellation. If a worker is still executing an external operation, the server
logs that the grace period expired and leaves databases open until process exit,
rather than closing SQLite beneath that worker. This is bounded process shutdown,
not a guarantee that every in-flight AI operation finishes successfully.

An already-running profile start in another request thread can delay shutdown
until its startup handshake returns (normally bounded by 30 seconds). Allow a
graceful termination window before sending a forced kill; Compose uses 45 seconds.

## Workspace service

SIGINT/SIGTERM request shutdown without blocking inside the signal handler.
`WorkspaceHTTPServer` can stop before serving or from its own serving thread.
Accepted sockets and central HTTP connections are canceled, request handlers and
sync/pivot workers are joined, and only then is local SQLite closed. Poll sleeps
are interruptible. Startup failures clean up resources already created.

Central connection setup is capped at five seconds; established upstream sockets
are tracked so response-header and response-body reads can be interrupted.
Python cannot forcibly cancel a native DNS resolver call. File-system operations
can also delay shutdown when the underlying device is unresponsive.

## Shared transports and clients

- WebSocket closure releases the socket even after a failed send or receive has
  marked the connection closed. It does not wait to send a close frame to a
  stalled peer. Closing the event hub rejects future subscribers.
- SOCKS closure includes clients that have connected but not finished their
  handshake. Socket shutdown wakes workers before their descriptors are closed.
- TUN and profile shutdown terminate process groups and reap children, including
  startup-failure paths. Manager `close()` is terminal; `stop_all()` remains a
  reusable operator operation where supported.
- MCP sessions use asynchronous exit stacks. A failure partway through opening
  transports, initializing sessions, or assembling multiple backends unwinds
  resources already acquired in reverse order.

See [agent shutdown](agent-shutdown.md) for Go/PowerShell behavior and the limits
of forced Windows process termination. No client plugin lifecycle logging is
introduced by these changes.

## Maintainer reference

| Symbols | Responsibility |
| --- | --- |
| `run_server`, its `terminate` and `make_http_server` helpers | Own listeners, signal handling, and unconditional finalization |
| `RMMServer.close`, `close_databases`, `_start_profile` | Idempotent resource cleanup, storage drain boundary, serialized profile startup |
| `RMMHTTPServer.__init__`, `process_request`, `process_request_thread`, `shutdown_request` | Track accepted sockets and active handlers |
| `RMMHTTPServer.abort_requests`, `join_requests`, `handle_error` | Interrupt reads, bound drain time, suppress expected shutdown disconnects |
| `BuildProcessManager.__init__`, `run`, `terminate`, `close` | Own compiler process groups, reject late work, kill and reap on exit |
| `WorkspaceHTTPServer.__init__`, `serve_forever`, `shutdown`, `server_close` | Avoid same-thread/pre-start shutdown deadlocks and drain handlers |
| `WorkspaceHTTPServer.process_request`, `process_request_thread`, `handle_error` | Track handlers and quiet expected shutdown disconnects |
| `WorkspaceUpstream.close`, `request` connection/HTTP adapters | Track and cancel central HTTP/HTTPS sockets |
| `WorkspaceService.request_stop`, `start`, `stop`, `serve_forever` | Signal-safe stop request, partial-start rollback, ordered cleanup |
| `WorkspacePivotManager.stop_all`, `_sync_loop` | Interrupt polling, reject late starts, join the data-plane worker |
| `OperatorEventHub.close`, `WebSocketConnection.close` | Close subscribers and release failed or blocked sockets |
| `SocksRelay._serve_client`, `SocksManager.close`, `TunManager.close`, `ProfileManager.close` | Track pending connections and prevent resource restarts after terminal shutdown |
| MCP session `__aenter__` / `__aexit__` methods | Unwind partial initialization and close every acquired async context |

Regression tests cover real process signals and connector children, failed binds,
idle HTTP/SOCKS clients, blocked upstream reads, compiler cancellation, terminal
manager closure, repeated stops, and partial MCP initialization failures.

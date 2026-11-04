# WebUI connection lifecycle

Asynchronous operator actions must remain associated with the login that started
them. `app.js` increments `window.rmmState.authGeneration` when connecting or
disconnecting, including reconnects using the same token. Its shared `rmmApi`
discards responses from an older authentication generation.

The Go agent builder and asset controls also use local `connectionContext` and
`isCurrent` helpers. They capture the generation and token before starting work
and check them after asynchronous boundaries. This prevents an upload decoded
under one workspace from being submitted under another, old asset buttons from
mutating a new workspace, and an old build response from restoring its download
link after reconnect. Direct build and download requests use the captured token.
The completed download link also retains its originating context and refuses
clicks from a later login.
These browser checks complement server authorization; they do not replace it.

In `go-agent.js`, `currentBeaconUrl` verifies the originating login before updating
the cached beacon URL. `jitterValue` distinguishes an explicit zero from an empty
or invalid field, preserving zero jitter in preferences and build requests.

In `assets.js`, profile actions refresh transport state before displaying any
start or stop error. The refresh therefore cannot immediately overwrite the
failure with a generic inactive-profile message. Upload and installation errors
are displayed only if their originating connection remains current.

## Regression coverage

Run `node tests/test_go_agent_ui.cjs` and `node tests/test_assets_ui.cjs`.
Each test file's `fixture` executes the production module with a small DOM
substitute (`Element`) and controlled asynchronous responses. Tests verify:

- Zero jitter reaches saved preferences and the actual build request.
- Switching accounts during transport lookup prevents configuration submission.
- A same-token reconnect during response parsing suppresses an old build result.
- Profile failure messages survive the subsequent transport refresh.
- Old transport replies cannot restore an earlier login's beacon URL or status.
- Switching accounts during file decoding prevents an upload into the new workspace.
- Asset buttons rendered by an earlier login cannot act under a later one.

These are behavioral Node tests, not graphical browser or live deployment tests.

## PowerShell beacon redirect boundary

`Invoke-RmmRestMethod` and `Save-RmmBeaconFile` disable automatic HTTP redirects
and explicitly reject 3xx responses. A custom beacon-token header otherwise
survives a cross-origin redirect in `HttpWebRequest`. Configure the final beacon
URL directly; redirects must not count as successful registration/results or
overwrite a downloaded artifact with a redirect response body. No new agent
logging was introduced for this check.

`tests/test_agent_redirects.cjs` starts two loopback HTTP servers, then invokes
`tests/test_agent_redirects.ps1`. The PowerShell harness extracts the production
functions through the parser AST without running the agent's main loop. It
checks GET, POST, and file downloads for 301, 302, 303, 307, and 308 responses,
using only a fake token, and verifies that an existing output file is preserved.
The destination must receive no request. Set `RMM_TEST_PWSH` to the PowerShell
executable if needed; the test reports an explicit skip when PowerShell is absent.
This suite and both UI suites run under `make check-web-contracts`.

The PowerShell beacon loop registers before polling `/cmd`. Registration errors
therefore cannot discard a command that central already dequeued. Terminated
sessions still exit through the existing registration error handler. The offline
`test_agent_poll_order.ps1` executes the production polling block with controlled
registration results and verifies both failure and successful command handoff.
This does not add acknowledgments: a connection failure after central dequeues a
command, or an agent crash before execution/results, can still lose that command.
Operators must not assume durable or exactly-once command delivery.

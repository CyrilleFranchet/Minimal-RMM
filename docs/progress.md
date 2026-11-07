# Progress Log

For current runtime behavior, use the [documentation wiki](README.md) and its
[architecture overview](architecture.md). This file remains the chronological
implementation log.

> Living document — update at the end of every work session.  
> Claude reads this via `/catchup` (`/.claude/commands/catchup.md`).

Current AI behavior is documented in [Playbook execution](web-ai-skills.md).
Older entries about automatic skill selection, preparation gates, optional
Exegol, or a direct AI fallback are historical and superseded by the explicit
playbook runtime. They are retained as implementation history, not setup advice.

## Current Status

### 2026-11-07 — PsExec lifecycle review fixes

- Publish and retain exact service/binary cleanup authority as soon as SCM
  acknowledges the start. `awaitLateralResult` preserves that acknowledgment
  when the command deadline expires and prefers an already completed result.
- `lateralMoveWithProgress` publishes progress before waiting. `launcherFailure`
  distinguishes a stopped, failed worker from a running or unreachable service:
  stopped failures permit cleanup and report deployment failure to the operator.
- Retain deployment names in failed-worker results and retry owned cleanup even
  without registration. Reserve beacon delivery and response time within the
  server timeout. Remove the duplicate post-wait ownership write so successful
  cleanup cannot be undone by a stale timeout acknowledgment.
- Bound C console diagnostics without concatenating an ANSI temp path into a
  fixed buffer, and size the launcher command line for a 128-character session.
- Add regressions for deadline results and typed worker failures. Native Windows
  runtime validation remains required.
- Validation: full host Go tests, `go vet ./...`, Windows amd64/arm64 test
  cross-compilation, and `git diff --check` pass. Host tests required permission
  to open localhost listeners outside the restricted sandbox.
- Final validation also covers the Python failed-worker cleanup and reserved
  timeout-budget regressions, full repository checks, and both C compile checks.
- Serialize the shared service embed through Go compilation so concurrent
  amd64/arm64 builds cannot embed another target's service. Cover both
  architectures in the concurrent build regression.

### 2026-11-07 — Dedicated PsExec service host and caller cleanup

- Keep the normal C/Go stage1 launchers free of SCM handling. Add a separate
  compact C service host that runs linked stage1 code on a native worker thread.
  Build and embed it independently as lateral-service.bin, including stageless
  agent builds; no Go standalone fallback or stale service image is substituted.
- The caller passes its listener URL and staging/session IDs, waits for successful
  service completion, deletes the temporary service and host binary, and reports
  the result. Sharing errors receive a bounded retry. Keep exact-artifact cleanup
  authority only when immediate cleanup is pending; retain the server retry path.
- Add service completion/ownership/build-isolation regressions and native Windows
  CI checks for SCM imports, service handshake, and caller-side artifact deletion.
- Review pass: aligned stale comments with the caller-owned cleanup flow (embed,
  psexec move, cleanup retry, service host), converted the service-host build
  timeout into a clean build error like the stage1 build, and normalized the
  Makefile recipe and placeholder test naming. `make go-test` (host tests,
  amd64/arm64 Windows cross-builds, stage1 and lateral-service C compile checks),
  `make check-agent-deployment`, `make check-parity`, and `make lint` pass.
  Native Windows runtime execution remains pending; the windows-agent-identity
  CI workflow builds the service host with MSVC and runs the SCM handshake and
  import-boundary checks.

### 2026-11-06 — Preserve spawn fallback diagnostics

- Return every attempted method's success or numeric Windows error and message,
  even when a later fallback succeeds. Add process identity, active token type,
  credential availability, creation flags, and read-only privilege snapshots.
- Send diagnostics only in the normal command result, not the agent log. Do not
  include passwords, alternate credential values, token handles, or raw errors.
  The API validates and projects known fields; WebUI escapes failure details.
- Regression checks cover wrapped Windows errors, successful fallback, malformed
  diagnostic payloads, and HTML escaping. `make go-test check` passes; native
  Windows tests compile for amd64 and arm64 but have not been executed here.
- The original run's discarded errors cannot be recovered. A fresh spawn with a
  rebuilt agent is needed to identify the failures on the affected host.

### 2026-11-06 — Remove deployment response probes

- Confirm spawn, spawnas, and lateral deployment through the expected session's
  registration under the source workspace. Remove automatic echo probes; no
  replacement internal ping or agent capability is needed.
- Keep `verified` as a compatibility alias for registration; omit response-probe
  fields and remove their CLI/WebUI display. Accept legacy verify_timeout options
  without sending or waiting for probe commands.
- Regressions assert that spawn queues only its launch request and lateral
  queues only launch and existing artifact cleanup, with no target probe.

### 2026-11-06 — Fix identity review findings

- Explicit rev2self now checks the OS result, closes and clears the operator
  token, and cannot be undone by later restoration helpers. Token replacement
  reuses the original thread lock. Worker impersonation is cleared on exit.
- Callback capture/revert/restore failures are terminal: block later network
  requests, operator commands, and spawn attempts; exit the main loop. Keep
  threads with uncertain impersonation pinned until their goroutine exits.
- Test the production spawn selector, permanent Windows revocation, logon-session
  restoration, and a local proxy Negotiate challenge. Add Windows identity CI.
  Native Windows runtime and successful integrated-proxy validation remain
  pending; netonly process credentials remain a separate documented boundary.
- Host Go tests, Windows cross-builds, project regression checks, and lint pass.
  Windows identity test binaries compile for amd64 and arm64. The new native
  suite also launches a short-lived detached fixture; it never starts a beacon.

### 2026-11-06 — Guard all Go server requests against thread impersonation

- Replace the result-only global-state guard with actual calling-thread token
  capture and checked RevertToSelf at the shared request boundary. Pin the
  goroutine to its OS thread until response-body close and token restoration.
- Block requests when capture/revert fails, return restoration failures, and
  preserve explicit rev2self without reactivating a retained token.
- Add success/failure isolation tests and a native Windows thread-token smoke
  test. Document inherited process-token and native plugin boundaries; neither
  is removed by reverting the calling thread.
- Validation: `make go-test` passes; the Windows test binaries compile for
  amd64 and arm64. The native thread-token smoke test and real proxy handshake
  have not been executed on Windows.

### 2026-11-06 — Ordered detached spawn fallback

- Try CreateProcessAsUser, CreateProcessWithTokenW, CreateProcessWithLogonW
  (netonly), then CreateProcessA. Stop after the first successful launch.
- Retain successful make_token credentials in memory for the logon fallback and
  clear references on explicit rev2self. Never include them in child arguments,
  environment, logs, or method diagnostics.
- Report the successful method and identity downgrade through the API and WebUI.
  Registration verification remains separate from network identity fidelity.
- Add ordering, early-stop, failure, API metadata, and warning regressions.
  `make go-test check` passes, including both Windows cross-builds. Native
  Windows execution remains outstanding.

### 2026-11-06 — Apply active network token to detached spawn

- Apply the active make_token token after detached process configuration when
  spawning a second Go agent. Restore parent thread impersonation after process
  creation on success or failure. Keep netonly semantics and report creation
  failures. The newer ordered-fallback behavior is documented above.
- Document the distinction from spawnas, inherited-token display limitations,
  and the need to rebuild deployed agents. Native Windows validation remains
  outstanding.
- Validation: `make go-test` passes host Go tests, Windows amd64/arm64
  cross-builds, TUN tests, and the C stage1 compile check.

### 2026-11-06 — Complete credential command audit

- Parse interactive CLI credential arguments once, preserving spaces, literal
  backslashes, and empty passwords; reject extra arguments and malformed quoting.
  Add CLI subcommand completion.
- Preserve corrupt credential files, validate stored records, report unavailable
  storage with HTTP 503, and write new credential files with mode 0600.
- Add regressions for workspace isolation, exact password preservation, deletion,
  file permissions, corrupt-store preservation, and CLI parsing. Password storage
  remains plaintext; shell history behavior is unchanged.

### 2026-11-06 — Fix credential creation routing and list response handling

- Match POST `/api/v1/creds` against its single path component, fixing the 404
  returned for credential creation. Keep workspace authentication required.
- Read the store's `creds` array and `label` fields in WebUI and CLI lists.
- Add real path parsing and store round-trip regressions for add/list/delete,
  plus authentication and WebUI list rendering checks. Include route tests in
  `make check-agent-deployment`.

### 2026-11-06 — Repair lateral method completion

- Show the supported lateral methods in the dropdown, filter by prefix, and
  reset selection when entering the submenu. Insert one space after selection
  and show the host argument hint. Exclude host input from history completion.
- Add keyboard, click, submenu, and host preservation regressions; all 26 shell
  completion tests pass, along with JavaScript syntax validation.

### 2026-11-06 — Accept stored or direct identity credentials

- Support credential IDs and direct identity/password arguments for WebUI and
  interactive CLI make_token, spawnas, and runas. Direct runas identities require
  a domain separator to distinguish passwords from command arguments.
- Resolve make_token credential IDs within the authenticated workspace in the
  API; expose the optional ID in the API client, MCP tool, and web AI schema.
- Update help and completion hints; cover both WebUI forms and workspace-scoped
  API credential resolution, including missing entries and missing workspace auth.
- Validation: syntax checks, AI tests, WebUI contracts, CLI tests, identity API
  tests, and operator parity pass. No native Windows execution was performed.

### 2026-11-06 — Repair credential commands and completion

- Bind parsed arguments used by credential and identity command handlers;
  split domain-qualified credential identities for the API and reject extra
  arguments for credential operations.
- Complete credential subcommands in the menu and show required argument hints.
  Exclude credential arguments from history-based completion. Add regressions for
  list/add/delete requests, quoted and empty passwords, validation, and completion.

### 2026-11-06 — Readable WebUI spawn results

- Replace the `/spawn` JSON dump with an escaped text summary of the session,
  parent, PID, host, registration, and response checks. Preserve the full API
  response in a collapsed native details element.
- Distinguish unchecked registration from failed registration. Add rendering
  regressions to `make check-web-contracts`. Rendering and session-routing
  tests, JavaScript syntax validation, and operator parity pass. Browser visual
  validation remains outstanding.

### 2026-11-06 — Fix staged (stage1) manual deployment flow

The staged ZIP delivery (`build_go_agent` default) only worked when the
launcher ran elevated within 10 minutes of the build — both constraints were
undocumented, so a manual `run-stage1.ps1` on a target silently never
launched the Go agent. Fixed end to end:

- **Staging TTL**: staged builds now register their staging entry with a
  dedicated 24-hour TTL (`STAGING_BUILD_TTL_SECONDS`), matching what the
  ZIP README and `docs/staged-deployment.md` always promised. The psexec
  lateral flow keeps the short 600-second TTL. `create_staging` takes a
  `ttl_seconds` parameter.
- **No elevation required**: the C stage1 and the Go stage1 fallback write
  the decoded agent to the account's temp directory
  (`%TEMP%\minimal-rmm-agent.exe`) instead of
  `%SystemRoot%\System32`, so a manual launcher run works from a
  non-administrator PowerShell. The psexec path (SYSTEM) is unaffected.
- **HTTP status check in the C stage1**: a 404 from an unknown, consumed,
  or expired staging entry is now reported as
  `stage1 failed: stage2 download failed: HTTP 404 …` instead of being
  decoded as the payload and failing the MZ check with a misleading error.
- **Operator visibility**: new `GET /api/v1/agent/go/stage/{staging_id}`
  endpoint backed by `RMMServer.staging_info()` surfaces the launcher
  status line, fetch state, and expiry for staged builds (previously only
  the lateral-move flow could read it). Exposed consistently as CLI
  `agent go-stage <staging_id>` and MCP/web-AI tool `stage_status`;
  `scripts/check_operator_parity.py` updated (66 tools).
- **Test hygiene**: build tests (`test_deployment_artifacts`,
  `test_agent_workspace_binding`) ran against the real `agent-go` tree and
  overwrote the repository's embedded `stage1.bin` placeholder with stub
  bytes — any agent built afterwards would embed a garbage launcher for
  psexec. They now build against an isolated minimal agent-go skeleton.
- Status lines from the C stage1 now carry the `stage1 ok:` /
  `stage1 failed:` prefixes, matching the Go stage1 and server tests.
- Docs updated: `docs/staged-deployment.md` (status endpoint, temp path,
  in-memory caveat), `docs/go-agent.md`, `docs/mcp-parity.md`, `README.md`
  API table. Validation: full Python suite (709 tests; one pre-existing
  unrelated `test_rmm_settings` redaction failure also present on HEAD),
  host Go tests, `make check-parity`, C stage1 compile-checked by
  `make check` when MinGW is available.

### 2026-11-06 — Shell feedback, failed-command recall, and Go session labels

- Render identity results only through session events to avoid duplicate API
  response output for uid, make_token, and rev2self. Save submitted shell input
  before validation and requests so failed attempts can be recalled and edited.
- Keep server-reserved deployment IDs and registration correlation while using
  the Go prefix for newly spawned and laterally deployed Go agents.
- Add browser regressions for duplicate identity output and failure history,
  and server regressions for Go ID format, uniqueness, and collision rejection.
- Explain local Windows worker creation and privilege-error semantics in
  `docs/go-agent.md`; no token or remote-execution changes are included.

### 2026-11-06 — Restrict cleanup ownership and report failures

- Require an exact in-memory record of acknowledged deployment artifacts before
  the source agent accepts cleanup. Match the request's host, session, basename,
  and shared deployment suffix; reject arbitrary names and fail closed after
  source restarts or lost acknowledgments.
- Preserve service lookup errors except service-not-found, and return partial
  deletion failures instead of reporting success. Source and server reject
  empty, failed, or unexpected cleanup responses. Keep ownership on failure
  for retries; release it after success.
- Add Go ownership/error regression tests and server response regressions.
  Host Go tests and 32 lateral server tests pass. Native Windows lifecycle
  validation remains outstanding.
- Pre-commit validation: full `make check`, `make go-test` (Windows amd64/arm64
  cross-builds and TUN tests), and host `go test -buildvcs=false ./...` pass.
  Update deployment verification fixtures to initialize staging without local
  build dependencies, and account for both compiler outputs per staged build
  in artifact isolation assertions.

### 2026-11-06 — Public status request body limits

- Reject missing, malformed, zero, negative, and oversized status body lengths
  before reading the public request stream. In particular, negative lengths
  must never reach an unbounded `read(-1)` call.
- `tests/test_public_status_body_limits.py` covers rejection without reading
  the stream or recording a status. Its test class and test method exercise
  the existing HTTP handler; no deployment actions are performed.

### 2026-11-06 — Two-stage psexec launcher, listener staging, native WMI/WinRM

- Replaced the naïve psexec module with a RemCom-faithful two-stage flow: the
  agent embeds a compact **stage1 launcher** (`agent-go/stage1/`, `go:embed
  stage1.bin`, built by `build_go_agent` before the agent compile) and extracts
  it at runtime — the way RemCom extracts its service from itself. stage1 is a
  proper Windows service that carries no beacon secret: its only capability is
  the staging identifier on its command line.
- Added **staging management on the listener**: `lateral_move(psexec)` stages
  the newest Windows agent build in a single-use, TTL-bounded (600 s) entry;
  `GET /tools/stage/<id>` serves the payload masked with a SHA-256 counter
  keystream keyed by the id, so no executable file is ever downloaded from the
  server; `POST /tools/stage/<id>` records stage1's one status line, surfaced
  as `stage_status`. Routes are capability-authenticated on the beacon listener
  without a beacon token. Missing builds answer
  `agent_payload_unavailable` (503).
- stage1 decodes the payload in memory, verifies the `MZ` header, installs the
  agent as a plain visible process with `--lateral-session`, reports, and
  stops. After the target registers, the server sends
  **`__LATERAL_CLEANUP__`** with the deployment names from the handshake
  (`service=… binary=…`); the first agent removes the stage1 service and the
  launcher binary from `ADMIN$` under the active `make_token` identity,
  reporting each outcome honestly.
- Replaced `wmic.exe`/PowerShell in **wmiexec** with a native COM/DCOM chain
  (`wmi_windows.go`): WbemLocator → remote `ConnectServer` →
  `CoSetProxyBlanket` (packet privacy) → `Win32_Process.Create` via
  `GetObject`/`GetMethod`/`SpawnInstance`/`Put`/`ExecMethod` with provider
  `ReturnValue` validation. Removed the dead PowerShell helpers.
- Replaced `winrs.exe`/`wmic.exe` in **winrm** with the native `WsmSvc.dll`
  WSMan client (`wsman_windows.go`): `WSManInitialize` → `CreateSession` →
  `CreateShell` → `RunShellCommand` → receive until the command Done state →
  close. The remote command runs the agent with the new `--lateral-detach`
  mode, which re-registers itself as a local SCM service and exits so the
  agent escapes the WinRM shell job object.
- Docs updated: `docs/go-agent.md` (stage1/staging/cleanup architecture,
  native module table), README (REST, staging routes, beacon tokens).
- Validation: `go test ./...` + Windows/Linux cross-builds pass; stage1 mask
  round-trip and argument tests pass; 35 server tests (staging store,
  single-use fetch, status lifecycle, cleanup ordering, handshake regexes,
  `agent_payload_unavailable`) pass; `make test`, `check-parity`,
  `check-cli`, `check-web-contracts`, and Markdown lint pass. Per the
  supported-platform policy, none of this is native Windows runtime
  validation: the COM/DCOM chain, WSMan calls, SCM flow, and stage1 service
  lifecycle remain to be exercised on real hosts.

### 2026-11-06 — Remove the redundant SMB command-wrapper method

- Remove `smbexec` from the Go agent, API validation, CLI/MCP/AI contracts,
  WebUI help/completion, and current operator documentation. Earlier progress
  entries describing that method are historical and superseded by this removal.
- Delete the command-wrapper service branch and its special SCM timeout handling.
  Keep the native SCM service lifecycle required by `psexec`; failed service
  starts remain errors. Registration is still required for deployment success.
- Add rejection regressions so removed methods cannot reach command dispatch.

### 2026-11-06 — Unique lateral deployment resource names

- Add a shared 128-bit cryptographic random suffix to the service name and
  staged executable basename for every deployment. Preserve identifiable
  prefixes and abort before staging if random generation fails.
- Carry the generated service name to the target through validated startup
  arguments and use it for both service registration calls. Preserve workspace
  inheritance and compatibility with existing startup arguments.
- Add tests for associated names, uniqueness, entropy failure, startup round
  trips, invalid service names, and duplicate flags. Windows amd64/arm64 builds
  and Windows test compilation pass; native Windows execution remains untested.

### 2026-11-06 — PsExec/RemCom implementation audit

- Correct the RemCom terminology: the Go `psexec` method is native SMB/SCM
  deployment of the agent itself as a demand-start service, not a RemCom port.
  Document source locations, HTTP beacon behavior, LocalSystem target identity,
  delayed service deletion, retained executable, and native-test coverage limits.
- Remaining review findings: fixed service/binary name collisions, hardcoded
  remote Windows directory, ignored DeleteService errors, and dispatcher errors
  collapsed into the interactive-start fallback. No runtime fixes in this audit.

### 2026-11-06 — Windows enrollment and authoritative deployment verification

- Declare managed agents Windows-only in repository instructions and public docs;
  restrict build API, CLI, MCP/AI, and WebUI targets to Windows amd64/arm64.
- Embed workspace ownership in configured Go/PowerShell artifacts and registration.
  Authenticated workspace scope wins; unknown IDs and ownership changes are rejected.
  Spawn and lateral deployment carry the source owner and a server-selected ID.
- Require the expected new registration for deployment success. Lost launch
  acknowledgments remain inconclusive until registration is checked. Unique probes
  with exact output matching establish command responsiveness; unrelated output
  cannot set `verified: true`. Skipped checks report acceptance only.
- Classify SMB command-wrapper handshake timeouts as inconclusive. Validate CIM
  provider ReturnValue and process ID independently of the shell exit status.
- Make Go tests safe on Windows without live lateral or credential operations;
  add queue/registration/result integration tests with simulated beacons.
- Bind all PowerShell WebUI output modes and isolate pending output by connection.
  Native Windows service/SCM/WMI/WinRM runtime validation remains outstanding.
- Validation: `make check` passes, including 88 deployment/enrollment Python
  regressions, 29 CLI tests, and the WebUI suites. Full host Go tests, Windows
  amd64/arm64 builds, Windows test compilation, and PowerShell syntax pass.
  Full Python discovery: 691/692 pass; the settings restart/reset test fails
  identically using unchanged HEAD files in a temporary baseline directory.
  Independent code/security and frontend/test review findings were resolved.

### 2026-11-06 — Operator review corrections

- Preserve visible plaintext password commands and CLI/WebUI history per operator
  preference. Align interactive quoting, literal backslashes, and empty passwords
  across CLI and WebUI; malformed quotes never reach the agent.
- Add session-ID completion for CLI `/spawn` and method completion for `/lateral`
  in both CLI completers and the WebUI.
- Size workspace proxy waits from requested execution and verification budgets.
  Forward fractional lateral execution timeouts to the agent worker.
- Preserve `rev2self` agent failure results across REST, MCP, CLI, and WebUI.
- Fix Windows service status calls to use the registered status handle and
  synchronize stop callback access. Windows runtime validation remains required.
- Add regression tests for parsing/history, completion, real workspace HTTP
  dispatch budgets, timeout forwarding, and failure propagation.
- Validation: `make check` passed using the configured Python interpreter;
  all Go agent tests passed with a temporary build cache. Windows amd64 and
  Linux amd64 cross-builds and Windows test compilation passed. The 39 focused
  identity, lateral, spawn, and workspace-timeout Python tests passed. Local
  HTTP socket tests required execution outside the default sandbox.

### 2026-11-06 — Add `__LATERAL__` and `__UID__`; make_token credentials as parameters

- Implemented `/lateral` for the Go agent: `__LATERAL__` stages the agent's
  own binary on `\\host\ADMIN$\System32` and starts it through psexec
  (RemCom/PsExec SCM technique — the agent is now service-aware via
  `StartServiceCtrlDispatcher`), smbexec (`cmd.exe /Q /c` service wrapper),
  wmiexec (`wmic process call create` with PowerShell CIM fallback), or
  winrm (`winrs` wrapping a remote `wmic process call create` so the agent
  escapes the WinRM job object). The move runs in a bounded
  `--lateral-child` process inheriting the active `make_token` identity, so
  the command loop is never blocked and alternate credentials govern SMB,
  SCM, WMI, and WinRM authentication. The target copy registers under a
  fresh `<parent>-lateral-<unix-nano>` session ID passed as
  `--lateral-session`; it needs an agent binary built with embedded
  connection settings.
- Implemented `/uid`: `__UID__` reports `agent user:` (process token
  account) and `network identity:` (active make_token identity or none) so
  operators can verify make_token and rev2self.
- `make_token` credentials are now passed as explicit parameters on every
  surface (CLI `make-token <identity> <password>` and
  `/make_token <identity> <password>`, WebUI `/make_token <identity>
  <password>`, MCP `make_token(password=…)`); the masked `getpass` prompt,
  `--password-file`, and the WebUI inline password form were removed per
  operator decision. The server still records only the identity label in
  operator events.
- Operator parity for both commands: `POST /api/v1/sessions/{id}/lateral`
  and `…/uid`, CLI subcommands + interactive commands, WebUI shell
  commands, MCP `lateral_move` and `session_uid` tools, parity registry
  (60 tools, 11 shell verbs).
- Docs updated: README (REST/beacon/CLI/MCP tables), `docs/go-agent.md`
  (lateral execution model, module table, requirements; uid contract),
  `docs/mcp-parity.md`, `docs/web-shell-completion.md`, and the dispatch AI
  skill.
- Validation: `go test ./...` plus Windows/Linux cross-builds pass; new
  `tests/test_lateral_move.py` (flow, routing, handshake regex) and
  `agent-go/lateral_test.go` (payload validation, session ID tags, uid
  contract, Windows-only enforcement) pass; CLI dispatch tests for
  `/make_token` (parameters), `/lateral`, and `/uid` pass; WebUI fixture
  tests for the three commands pass (completion count 13); `make test`,
  `check-parity`, and Markdown lint pass. Live Windows behavior of every
  lateral module, the service dispatcher, and `CreateProcessAsUser`
  privileges remain unverified on real hosts.

### 2026-11-06 — Add `__MAKE_TOKEN__` / `__REV2SELF__` network identity commands

- Implemented `runas /netonly` style identity switching for the Go agent:
  `__MAKE_TOKEN__` calls `LogonUserW` with `LOGON32_LOGON_NEW_CREDENTIALS`
  (the logon type behind `runas /netonly`), stores the cloned token, and
  applies it to subsequent operator command children via Go's
  `SysProcAttr.Token` (`CreateProcessAsUser`). `__REV2SELF__` closes the
  token; both are idempotent/replacing. No injection into host processes.
- Credentials travel as a JSON body on the beacon command (like `__EXFIL__`);
  the agent never logs them and the server records only the `DOMAIN\user`
  label in operator events. Documented that this logon type does not
  validate credentials, so success does not prove the password is correct.
- Operator parity: `POST /api/v1/sessions/{id}/make_token` and `…/rev2self`,
  CLI `make-token` / `rev2self` subcommands plus interactive `/make_token`
  and `/rev2self`, WebUI `/make_token` and `/rev2self` shell commands, MCP
  `make_token` / `rev2self` tools, parity registry. Credentials are passed
  as explicit parameters on every surface (see the lateral/uid entry above:
  the interactive prompts were removed per operator decision).
- Docs updated: README (REST/beacon/CLI/MCP tables), `docs/go-agent.md`
  (identity contract, result prefixes, privilege caveat),
  `docs/mcp-parity.md`, `docs/web-shell-completion.md`, and the dispatch AI
  skill.
- Validation: `go test ./...` plus Windows/Linux cross-builds pass; new
  `tests/test_make_token.py` (flow, routing, secret redaction) and
  `agent-go/make_token_test.go` pass; CLI interactive dispatch tests
  (getpass mocked) and WebUI fixture tests (masked form, history exclusion,
  completion count 11) pass; `make test`, `check-parity`, and Markdown lint
  pass. Live Windows behavior of the token and `CreateProcessAsUser`
  privileges remain unverified on a real host.

### 2026-11-06 — Add `__SPAWN__` detached second-agent command

- Rejected the injection design: the Go agent is never hidden inside another
  process such as `notepad.exe`. `__SPAWN__` starts a separate, visible agent
  process that is easier to audit and debug.
- Agent side (`agent-go/spawn.go`): `__SPAWN__` re-executes the agent's own
  binary with `exec.Command` (not `newCommand`, so the child is not bound to
  the parent context), detached standard streams (null device), a fresh
  beacon-token-safe `RMM_SESSION_ID` that overrides any inherited identity,
  pinned connection settings, its own log file, and Windows
  `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` / Unix `Setsid` detachment.
  The command returns `spawned session=<id> pid=<pid>` immediately; a
  background goroutine reaps the child.
- Server side: `RMMServer.spawn_session` sends `__SPAWN__`, waits for the new
  session to register, then runs `echo rmm-spawn-verify` on both sessions and
  reports `registered`, `parent_responsive`, `spawned_responsive`, and
  `verified`. PowerShell sessions are rejected with
  `agent_type_unsupported`. New endpoint
  `POST /api/v1/sessions/{id}/spawn`.
- Operator parity: CLI `spawn` subcommand + interactive `/spawn`, WebUI
  `/spawn` shell command, MCP `spawn_session` tool, shared AI tool schema,
  and `scripts/check_operator_parity.py` registry updated.
- Docs updated: README (REST/CLI/MCP tables, beacon command table),
  `docs/go-agent.md` (spawn contract), `docs/mcp-parity.md`,
  `docs/web-shell-completion.md`, and the deployment/dispatch AI skills now
  point to `spawn_session` as the established in-band launch mechanism.
- Validation: `go test ./...` plus Windows/Linux cross-builds pass; new
  `tests/test_spawn_session.py` (flow + routing) and spawn CLI dispatch tests
  pass; `make test`, `check-parity`, `check-cli`, `check-web-contracts`
  (completion count updated to 9), and Markdown lint all pass. Live Windows
  coexistence of the detached child remains unverified on a real host.

### 2026-11-06 — Review foreground agent deployment

- Confirmed synchronous command execution in both agents and foreground
  generated launchers. Starting a second agent through this path can block
  polling indefinitely; this does not prove termination of the first process.
- Identified inherited `RMM_SESSION_ID` reuse when generated launchers omit
  an empty configured identity. Runtime behavior remains unchanged.
- Added deployment and dispatch reference constraints for independent process
  lifetime, standard streams, fresh identity, and verification of both sessions.
  Production AI requires the reference in its explicit playbook.
- No target processes were started. Live Windows reproduction and a runtime
  fix remain outstanding.
- Validation: 24 existing deployment/artifact/Go configuration tests passed;
  Markdown lint passed for the four changed documents. These checks do not
  exercise process coexistence or foreground blocking on Windows.

### 2026-11-06 — Preserve pending MCP connection status

- Fixed the WebUI connection monitor discarding valid HTTP 202 runtime
  snapshots and reporting that the workspace did not respond. HTTP 503
  snapshots now retain the runtime's MCP status and diagnostic error.
- Added UI regressions for checking, starting, failed, and missing snapshots.
  Live endpoint connectivity remains unverified.

### 2026-11-06 — Inspect AI operations from the WebUI

- Added an Operation details dialog for AI jobs with a persisted activity
  timeline, progress, timing, and bounded exception diagnostics, including
  nested exception groups.
- Tool arguments and returned output are available through explicit, paged
  evidence reads scoped to the authenticated job owner. Metadata exports
  exclude raw operational evidence. Historical jobs display only retained data.
- A received tool response does not establish remote command success. Missing
  responses remain uncertain; inspecting details never retries an operation.
- See [Background AI jobs](ai-background-jobs.md) for retention, evidence
  access, and the distinction between job status and execution outcome.
- Validation: full `make check` passed, including Python regressions, UI
  workflow tests, operator parity, syntax, and Markdown/YAML lint. Dialog,
  reconnect, and authentication-reset UI regressions passed after final edits.
  Live browser visual QA and deployed Exegol execution were not performed.

Follow-up review reproduced two activity-inspection defects: tool responses
arriving after Stop could lose their evidence references, and live polling
could replace pending evidence-load controls. The fixes retain late observations
without restarting stopped work and preserve evidence loading/error state across
refreshes, with deterministic regression coverage.

### 2026-11-05 — Update retained skill references and metadata

- Updated default metadata for deployment, BloodHound, pivot, lateral movement,
  NetExec, and Windows reconnaissance/profile references. Added NetExec share
  enumeration guidance and an rclone transfer reference.
- Aligned the legacy catalog regression with NetExec's changed default flag.
  These are reference/catalog changes: production AI still loads only the
  explicitly supplied playbook, regardless of these default flags.
- Markdown validation covers all changed skill files. No target action or
  transfer was executed as part of this update.

### 2026-11-05 — Review MCP embedding projections and copy evidence

- Reviewed shared serialization, event/command/result paths, plugin copy-failure
  output, source-data preservation, and raw export compatibility. No live target
  or provider request was used.
- Fixed a regression-gate gap: `make check-ai` now includes embeddings API and
  projection tests, so `make check` exercises them. Added all-error and
  all-provider follow-up request regressions.
- Documented that counts describe result rows and reported copy paths, not
  independent acceptance checks. Unknown/invalid schemas and historical raw
  context remain outside the projection; stored/wire sizes are unchanged.
- Hardened the projection fallback for invalid Unicode and excessive JSON
  nesting so malformed output does not abort tool serialization.
- Final review: no remaining blocking finding. Full `make check` passed on an
  isolated snapshot excluding untracked logs/custom skill: 148 AI/embeddings
  tests, 15 CLI tests, WebUI checks, parity, syntax, Markdown and YAML lint.
  Live Windows copying and live-provider execution remain untested. No commit
  or deployed-service change was made during this review.

### 2026-11-05 — Keep embedding vectors out of AI/MCP context

- Confirmed saved-profile local-copy fields pass through MCP to the plugin's
  raw Run export. Copy paths refer to the agent filesystem, not the workspace.
- Shared tool serialization projects validated embeddings output to labels,
  scores, paths, copy destinations, errors, dimensions, and counts. Vectors are
  omitted for copy and no-copy runs, including event and saved-result reads.
- Original session output and saved collections remain intact for REST/CLI/UI
  export. Added projection, batch-size, and MCP copy-profile integration tests.
  No live plugin execution or file copy was performed.

### 2026-11-05 — Explain plugin installation and synchronization limits

- Release size errors now include the raw size and active workspace asset
  limit. Install-time synchronization preserves numeric upstream status and
  exact JSON request bytes while excluding upstream response bodies.
- HTTP 413 diagnostics explain the central request limit and base64 overhead;
  the WebUI displays these details and keeps failed items selected for retry.
  Verified local bytes remain available for checksum-verified reuse.
- `PluginRuntimeSyncError` carries safe diagnostics; opt-in `diagnose` on the
  workspace publisher preserves boolean behavior for background reconciliation.
  Documented per-owner settings, restart/retry, and proxy-limit distinctions.
- No deployed limit, installed binary, or target state was changed.
- Validation: full `make check` passed on the intended commit snapshot,
  excluding untracked runtime logs and the custom lab skill. The 44 focused
  release/synchronization/embeddings Python regressions and 21 release UI
  regressions passed. No live download, synchronization, or classification ran.

### 2026-11-05 — Check embeddings prerequisites before dispatch

- Diagnosed the reported missing autonomous DLL and subsequent incompatible
  command-envelope fallback. The strict embeddings decoder expects raw request
  JSON; `command=Run` is not equivalent to `export=Run`.
- Workspace embeddings execution checks the scoped central plugin catalog and
  fails before queueing when the required autonomous artifact is unavailable.
  MCP descriptions explain the ABI, avoid unsupported fallback, distinguish
  queued from completed, and call out saved-profile copy options.
- Added missing/legacy-only plugin and unavailable/malformed catalog regressions
  and preserved raw-input dispatch coverage. Provider reasoning-budget exhaustion
  remains a separate configured limit; no live setting or installed DLL changed.

### 2026-11-05 — Restore screenshot download overlay after image load

- Restored Download to the screenshot's bottom-right corner while keeping its
  label on one line and preserving the bounded preview dimensions.
- Reveal the overlay only after a successful image load. Capture image load
  events on the shell container for live results and restored history; failed
  images leave the button hidden.

### 2026-11-05 — Validate playbook changes for commit

- Updated security regressions to require Exegol even when a legacy request
  asks to disable it. Provider endpoint overrides remain stripped.
- Installed declared project requirements in the configured virtual environment.
  Full `make check` passed on an isolated snapshot of the intended tracked
  changes plus the new playbook template/tests: syntax, 124 AI tests with no
  skips, 15 CLI tests, parity, WebUI regressions, Markdown lint, and YAML lint.
- Kept untracked runtime logs and the custom Northwind skill out of the
  validation snapshot and commit. No live provider or target action was run.

### 2026-11-05 — Stabilize inline screenshot layout

- Reserve responsive preview space for shell screenshots while images load.
  Place Download below the image in normal flow and prevent its label wrapping
  vertically. Preserve screenshot proportions within the bounded thumbnail.
- Existing WebUI contract checks pass. Live visual inspection was unavailable
  because browser access and local computer-use permissions were unavailable.

### 2026-11-05 — Reconcile AI documentation with the playbook runtime

- Updated the README, architecture, background jobs, chat memory, workspace
  reference, service-setting descriptions, and documentation index. Removed
  current-use instructions for skill activation and optional Exegol fallback.
- Reframed legacy skill assets and adaptations as retained reference content.
  Documented the template, API payload, context budgeting, error handling,
  validation boundaries, and absence of incremental phase loading or restart.
- Preserved historical progress entries and marked superseded AI behavior.

### 2026-11-05 — Audit uncommitted AI/playbook changes

- Reviewed AI loop, explicit playbook injection, mandatory Exegol resolution,
  workspace forwarding, hidden legacy skill controls, and template download.
  Left concurrent deployment/rclone work and existing skill files untouched.
- Fixed Markdown template placeholders interpreted as HTML and corrected stale
  fallback documentation. Updated the empty-response regression to exercise
  the production playbook path; added cancellation and real-schema/template
  context-budget regressions across providers.
- AI checks: 124 tests run, one MCP SDK test skipped. Workspace/broker checks:
  29 tests passed. WebUI checks, parity, changed-document Markdown lint, YAML
  lint, and diff whitespace checks passed. Full `make check` remains blocked by
  missing `prompt_toolkit`; full Markdown lint also reports errors in the
  unrelated untracked `ai-skills/northwind-financial-exfil.md` file.
- No commit, live provider request, or target operation was performed. Actual
  Exegol schema size and live MCP connectivity remain deployment checks.

### 2026-11-05 — Consolidate deployment ownership and configuration

- Made `rmm_deployment.py` the supported startup path. Native single mode
  supervises central and workspace; distributed mode generates central-only
  Compose from saved settings. Existing Exegol runs its own workspace process.
  Separate-host overlay deployment remains future work.
- Pinned mutable data to `central/` and `workspaces/<id>/` beneath an explicit
  deployment root. Saved settings are authoritative; inherited service variables
  require an explicit native compatibility flag. Added credential-free `paths`
  diagnostics and owner-relative executable path resolution.
- Kept rclone inventory-based: copying `assets/rclone/profiles.json` does not
  register it. Documented WebUI/API registration and added an offline wrapper
  import for fresh destinations. Disabled/empty assets never revive legacy
  profiles or examples; malformed enabled configuration blocks transfer jobs.
- Replaced competing startup/environment recipes with one deployment guide and
  a complete settings inventory. Removed the old generic workspace sidecar.
- Deployment smoke and rclone storage regressions pass. No live Docker rollout,
  target operation, or credential migration was performed.
- Generated Compose passed `docker compose config --quiet`. Independent code
  and documentation reviews found no remaining blocking defect. Final staged
  snapshot validation passed `make check`, 20 deployment tests, 65 workspace
  tests, and 12 rclone tests (the workspace/rclone sets overlap).
- Validation excludes concurrent AI/playbook edits and unrelated untracked demo
  Markdown. Those working-tree changes remain uncommitted; their previously
  reported broader-suite failures are outside this deployment change.

### 2026-11-05 — Downloadable AI playbook template

- Added a Markdown template download to the Playbook panel using the existing
  static WebUI routes. It defines scope, ordered phases, evidence, checkpoints,
  and completion reporting without loading legacy skills or starting a task.
- Documented that phase headings do not enable incremental loading: the full
  playbook still needs to fit in context. Existing skill files are unchanged.

### 2026-11-05 — Hide remaining legacy skill controls

- Removed AI skill selection from managed uploads and filtered legacy skill
  rows out of the asset list. Removed obsolete refresh hooks and skill-loading
  labels from AI job status. The AI interface exposes one playbook editor.
- Preserved skill files and backend asset APIs. Verified asset rendering and
  AI job UI behavior with the executable UI regressions.

### 2026-11-05 — Make Exegol mandatory for AI execution

- Removed the per-run Exegol toggle. Central and workspace AI routes always
  check Exegol readiness and resolve owner-controlled connection settings.
- AI execution always requests Exegol MCP, ignoring legacy disable flags.
  Missing MCP or unavailable Exegol fails explicitly without direct fallback.
- Updated AI, route, workspace, and WebUI regressions. Skill files remain
  untouched and are not automatically loaded.

### 2026-11-05 — Simplify v2 chat to explicit playbook execution

- Replaced production step selection and skill-policy snapshots with one inline
  Markdown playbook, pinned alongside the operator prompt and tool results.
  No default/core skill injection or ai-skills directory reads occur in chat.
  Existing skill files remain unchanged; legacy IDs no longer activate them.
- Both direct and MCP transports expose operational schemas without preparation
  tools. Workspace and central routes validate zero or one explicit document.
- Replaced WebUI skill checkboxes with a playbook editor and explicit Exegol
  connection toggle. Kept authentication, scope, cancellation, evidence,
  context limits, and bounded empty/truncated-response recovery.
- Added direct/MCP injection, API validation, workspace forwarding, and WebUI
  submission regressions. No live target operation was run.

### 2026-11-05 — Reject empty AI completion after prerequisite checks

- Identified that a provider reply containing neither visible text nor tool
  calls was accepted as success, allowing a timing-only completed response.
- Retry empty generations at most twice within the task round ceiling,
  retaining evidence and preparation rules. Exhaustion reports
  `empty_provider_response` and unfinished work explicitly. Nonempty reports
  remain model judgments; this is not a semantic task-completion validator.
- Added mocked all-provider regressions for continuation from relay checks
  through startup, exhaustion, last-round behavior, and blocker reports.
  No live relay or target command was started.

### 2026-11-05 — Hide agent workspace tabs without a selection

- Hide the Shell, Files & Screenshots, and Pivots tab bar and initial shell
  panel in HTML. Reapply selection-driven visibility before showing the app,
  covering first login and reconnect without a selected agent. Existing live
  and archived selection and close-view behavior use the same visibility logic.
- Follow-up verification fixed stale archived selection after disconnect,
  invalidated pending transcript loads, and prevented hidden panels refreshing
  without a selection. A reconnect regression covers both selection types and
  verifies the previous archive and header do not reappear under a new login.

### 2026-11-05 — Recover truncated provider output on v2

- Investigated the reported four-round failure after recon preparation. The
  error denotes a provider output-limit stop, independently of task rounds
  and local input-context admission. The live provider usage was unavailable.
- Provider loops now discard the whole truncated generation before dispatch,
  including complete-looking calls in an incomplete batch, and retry at most
  twice within the existing round ceiling. Active preparation and prior
  returned results remain intact; no partial text or tool arguments are reused.
- Retained operator control of the per-response output allowance. Exhaustion
  reports numeric output/reasoning counters and the configured limit in the UI,
  with guidance to adjust the central worker's output reserve when appropriate.
- Added all-provider regressions for retained preparation/evidence, discarded
  batches, successful recovery, bounded exhaustion, last-round handling, and
  safe diagnostics. Updated protocol documentation. No target command,
  credential search, or PCAP capture was run during this fix.
- Follow-up review added cancellation, nondefault output reserve, and
  nonconsecutive truncation tests proving the recovery budget is global across
  the task. Moved the output-limit field before context measurement so size
  diagnostics include the complete request. Seven recovery tests cover all
  three providers; UI checks cover the allowlisted counters. Independent review
  found no blocking defect. Recovery deliberately retains the configured output
  cap and does not guarantee success for an undersized allowance.

### 2026-11-05 — Fix context rejection immediately after realistic preparation

- Reproduced the reported failure with real shipped recon/pivot bodies and
  RMM schemas. Pinning both skills while resending all operational schemas
  exhausted the default conservative byte-based admission budget immediately
  after `prepare_step`; this was not a provider-reported context overflow.
- Selection now exposes internal preparation tools and a compact operational
  catalog. Execution exposes only prepared operation schemas plus internal
  tools. The complete registry remains server-side for guards and prerequisite
  checks; consuming preparation restores preparation-only schema exposure.
- Added content-free size measurements to results and context-failure details
  in the WebUI. The configured budget and complete skill bodies are unchanged.
- Added real-body regression coverage for all three provider protocols, large
  unrelated schemas, both phase transitions, and error diagnostic rendering.
  No target commands or network scans run in these tests.
- Validation: `make check` passes (75 AI tests with one MCP-dependent skip,
  26 skill/provider tests, six route tests, 15 CLI tests, UI recovery, syntax,
  parity, WebUI contracts, and lint). All 57 workspace tests pass. Independent
  schema/barrier review found no blocker. No live remote scan was performed.

### 2026-11-05 — Documentation consistency and failure handoff review

- Reconciled README, deployment, provider, background-job, settings, and skill
  adaptation docs with metadata-conditional Exegol requirements and RMM-only
  direct execution. Corrected stale WebUI copy and removed the unconditional
  browser Exegol-enable flag. Documented central-worker context settings.
- Skill instructions and declarative guards now use the same parsed-file
  snapshot instead of two independent reads during submission.
- Provider/tool failures preserve scoped evidence references and the latest
  step state in structured results without raw exception messages, retaining
  HTTP classification. Returned-call references carry into later chat turns.
- Step summaries and active IDs remain in subsequent progress events, rather
  than disappearing as soon as the next model/tool phase starts.
- Added snapshot consistency, provider-failure evidence recovery, persistent
  step-progress, and workspace submission regression tests.
- Final validation: `make check` passes (74 AI tests with one MCP-dependent
  skip, 26 skill/provider tests, six route tests, 15 CLI tests, UI recovery,
  syntax, parity, WebUI contracts, and lint). All 57 workspace tests pass.
  Checked 127 local Markdown links with no missing targets. Independent
  failure-path and documentation review found no remaining concrete blocker.
- `bloodhound-queries/catalog.json` is unchanged. Live model/remote deployment
  acceptance remains unverified; the unrelated baseline settings-test failure
  documented below is unchanged.

### 2026-11-05 — Adversarial follow-up review of general skill runtime

- Found and fixed an explicit invocation gap: `/skill-id` now resolves enabled
  dependencies and pins instructions before the first provider request, even
  when the model returns a tool-free answer. Disabled invocations fail early.
- Closed conditional action-guard bypasses from handler normalization: validate
  action enums before dispatch and compare no-enum guarded actions conservatively.
- Removed full archived blobs from final job responses and persistent-run memory;
  return only artifact manifests. Persist new results in bounded character pages
  to preserve Unicode and embedded NULs without full reads. Existing archives
  remain readable through a backward-compatible schema upgrade.
- Added regression cases for all four findings and legacy archive compatibility.
  The BloodHound query catalog is unchanged.
- Final validation: `make check` passes (72 AI tests, one MCP-SDK-dependent skip;
  25 RMM AI tests; six provider-route tests; 15 CLI tests; syntax, parity,
  WebUI contracts, UI recovery, and lint). All 56 workspace tests pass.
  Live provider/remote Exegol acceptance remains unverified. The unrelated
  baseline settings assertion documented below remains outside this fix.

### 2026-11-05 — General per-step skill activation and bounded context

- Replaced the earlier Exegol/BloodHound eager-loading exceptions with a generic
  `prepare_step` barrier before every operational batch. The model selects
  skills for the immediate action and publishes a brief operational rationale.
- Skill metadata declares dependencies, required tools, priority, and tool or
  backend guards. Disabled dependencies never auto-enable; cycles and missing
  tools block preparation. Rules are forwarded from the workspace owner.
- Core boundaries remain pinned. Active instructions and a structured checkpoint
  survive history eviction; provider requests have a configurable conservative
  input budget and matching output reserve. Complete old message groups and
  large results move to scoped, durable, uniquely identified evidence archives.
- Historical checkpoints persist across chat reloads. Artifact retrieval is
  scoped to authenticated credentials, endpoint and selected session. RMM-only
  chats skip external runtime readiness when no enabled metadata requests it.
- The BloodHound query catalog remains unchanged. Deployment facts stay in
  skill bodies; no workflow-specific skill IDs remain in the generic loader.
- Validation: `make check` passes with the IDE interpreter and temporary CLI
  dependencies in `/private/tmp/rmm-skill-test-deps`; the IDE environment was
  not modified. Its AI target ran 66 tests (one MCP-SDK-dependent skip),
  25 RMM AI tests, six provider-route tests, and UI recovery checks. All
  56 workspace tests pass. Code/security review found no blocking issues.
- Separate settings tests exposed an existing assertion expecting settings
  changes to remain unavailable until restart; reproduced against HEAD's
  unmodified `rmm_settings.py`. That unrelated behavior was not changed.
- Live provider and remote deployment acceptance remain unverified. No local
  RMM service is running; remote host/service details are required to deploy.
  Evidence retention is documented and currently operator-managed.

### 2026-11-04 — Classify AI provider HTTP failures (401/403/429/5xx) in the async job path

- Follow-up to the MCP-mislabeling fix above: the WebUI's actual chat path
  (`asynchronous=True`, `rmm_ai_jobs.AIJobManager._run`) catches every
  exception from the provider call — Claude, OpenAI, or Mistral, same shared
  code — and deliberately replaces it with a generic "AI execution failed..."
  message, since the raw exception may carry credentials or the request body.
  Only an HTTP status code survived, buried in `diagnostics.exception_chain`.
- Added `_classify_failure()` in `rmm_ai_jobs.py`: walks the same
  cause/context chain as `_failure_diagnostics` and maps a known HTTP status
  to one of three bounded, credential-free labels —
  `ai_provider_authentication_failed` (401/403),
  `ai_provider_rate_limited_or_quota_exceeded` (429), or
  `ai_provider_unavailable` (5xx) — each with a fixed message. Unclassified
  failures (no HTTP status in the chain) keep the original generic message.
  `diagnostics.exception_chain` itself is unchanged.
- Tests: `test_provider_http_status_is_classified_without_raw_provider_text`
  (401/403/429/503) and `test_unclassified_failure_keeps_generic_message` in
  `tests/test_ai_jobs.py`; existing `test_failure_does_not_expose_exception_credentials`
  and `test_wrapped_provider_failure_retains_safe_diagnostics` still pass
  unchanged.
- `make check` passes. Not verified against a live provider 429/401 response;
  only unit-tested with a constructed `HTTPError`.

### 2026-11-04 — AI provider errors after MCP connects were relabeled as an MCP install problem

- Reported: hitting an AI provider quota limit (Mistral) in the WebUI chat
  surfaced as `MCP server failed: <provider error>. Install MCP support: pip
  install -r requirements-mcp.txt (Python 3.10+).` — confusing, since MCP was
  installed and connected fine; the real failure was downstream, in the
  provider chat call.
- Root cause: `_run_ai_chat_mcp` in `rmm_ai.py` wrapped **every** exception
  from `run_with_mcp_session(...)` with the same "MCP server failed... install
  MCP support" `RuntimeError`, regardless of whether the MCP session had
  already connected. A provider error raised from inside `chat()` (e.g. a 429
  quota/rate-limit message from `_json_request`, which already carries the
  provider's real error text) was indistinguishable from an actual failure to
  start/reach the MCP server.
- Fix: the `except Exception` handler now re-raises as-is when the local
  `connected` flag is already `True` (set at the top of `chat()`, before any
  provider call) — only a failure before that point still gets the MCP
  install/connection hint. Added
  `test_provider_error_after_connection_is_not_relabeled` and
  `test_connection_failure_before_chat_keeps_mcp_install_hint` in
  `tests/test_rmm_ai.py`.
- The unrelated `BloodHound: authentication_failed` WebUI label raised in the
  same conversation turned out to be expected, by-design behavior from the
  earlier `1b13953` commit (a 401 on the credential-free `/api/version` probe
  is kept as the literal displayed value even though it counts as CE having
  started); no change was needed there.
- `make check` and the full `test_rmm_ai*`/`test_rmm_ai_skills` suite pass.
  Not verified against a live Mistral 429 response; only unit-tested with a
  mocked `RuntimeError`.

### 2026-11-04 — Adapt bloodhound_mcp's reference resources into an ai-skill; defer direct integration

- Evaluated `mwnickerson/bloodhound_mcp` (linked, not published, by SpecterOps'
  "Open-source Tools" page) as a way to expose BloodHound pathfinding and
  collection to the AI assistant. Deferred direct integration: it is not
  Exegol-integrated (would need its own per-workspace process lifecycle), its
  `cypher_query.run` tool bypasses the validated query catalog, and its
  global-per-process credential model would need per-workspace process
  spawning to match `rmm_bloodhound_broker.py`'s workspace isolation. See the
  new backlog item in `docs/tech-plan.md`.
- Adapted its ten MCP resources (schema/Cypher reference, AD/Azure/ADCS/
  OpenGraph guides) into a new `ai-skills/bloodhound-attack-methodology.md`
  skill, reworded and reorganized under this project's existing
  interpretation-table conventions; cross-referenced from
  `bloodhound-analysis.md`. The upstream `cypher/offensive-queries` resource
  (ready-to-run Cypher) was deliberately not adapted — see
  [docs/bloodhound-mcp-adaptation.md](bloodhound-mcp-adaptation.md) for the
  full adaptation map and what was excluded. Upstream GPLv3 license preserved
  at `third_party/bloodhound-mcp/LICENSE`, pinned to revision
  `cdb17097e761c8a8622cb93bc3ba49a9e150bb6e`.
- Documentation-only change: no new executable action, catalog entry, or MCP
  tool. `make check` run before committing; live BloodHound CE validation of
  the adapted prose was not performed.

### 2026-11-04 — Operator `reset_ce` action for taking over a CE RMM did not initialize

- Request: an explicit, guarded operator way to reset BloodHound CE instead
  of running `bloodhound-ce-reset` by hand in the container — the companion
  to the automatic credential bootstrap for an existing instance whose
  initial admin password was never captured.
- `ExegolRuntime.reset_ce(clear_graph)` in `rmm_exegol_runtime.py`: stops CE
  first (a live connection would block the Exegol wrapper's drop), then
  runs `bloodhound-ce-reset` through the same identity-verified MCP exec
  channel as the launch, bounded by exit markers — a lost or failed reply
  fails closed with `ce_reset_command_failed` and keeps the durable intent.
  The neo4j graph survives by default; `clear_graph` wipes it after
  `neo4j stop`, guarded by an existence check. The workspace local proxy
  timeout for `/api/v1/bloodhound` rose from 30s to 120s for the reset.
- `bloodhound_api` action `reset_ce` (options: `clear_graph` boolean only)
  invalidates the three stored CE secret settings on success and re-arms the
  automatic bootstrap, so `ensure` relaunches the fresh database and the new
  initial admin password is captured without any manual configuration.
  Exposed across REST/CLI/MCP/WebUI (a "Reset BloodHound CE…" button with
  confirmation prompts in the workspace connection panel), all four action
  lists updated for `make check-parity`.
- Live follow-up: the first real `reset_ce` against the operator's Exegol
  showed the reset succeeded functionally (DROP/CREATE/ALTER psql tags, then
  postgres and neo4j stopped) but the Exegol wrapper's trailing
  `bloodhound-ce-stop` returns a nonzero exit after probing psql on the
  already-stopped postgres, so the exit-code marker mislabeled a successful
  reset. `RESET_COMMAND` now replicates the wrapper's proven psql sequence
  and judges success on a `RMM_CE_RESET_DONE` marker reachable only through
  the full `&&` chain, with best-effort service stops afterward; the psql
  success tags themselves return in the bounded exec reply.
- Tests: six runtime cases in `tests/test_exegol_runtime.py` (state/intent
  clearing, explicit graph-wipe choice, definite failure with kept intent,
  missing wipe marker, in-progress guard, identity verification) and two
  surface cases in `tests/test_bloodhound_surfaces.py` (option validation +
  credential clearing, bounded `auth` responses). `make test`,
  `check-parity`, `check-web-contracts`, and `lint-md` pass; the only
  failing module tests remain the two pre-existing timing-sensitive
  `test_exegol_runtime` cases. Updated
  [bloodhound-workflow.md](bloodhound-workflow.md) (operator CE reset section).

### 2026-11-04 — Automatic BloodHound CE credential bootstrap

- Request: with a dedicated Exegol workspace, manage the CE API
  authentication internally instead of manual token configuration.
- Verified the exact contracts against the operator's CE build
  (SpecterOps/BloodHound v7.6.2, read from the container's own source): a
  fresh database prints `# Initial Password Set To:    <password>    #` to the
  CE log (charset `[A-Za-z0-9_]`, 32 chars), `POST /api/v2/login` takes
  `{login_method:"secret", username, secret}` and returns a bearer
  `session_token`, and `POST /api/v2/tokens` (bearer session) mints a signed
  token as `{id, key}` shown exactly once. No register-admin endpoint exists
  in this generation.
- New module `rmm_bloodhound_ce_auth.py`: bounded-tail log parsing, login,
  token minting, and a `bootstrap_credentials` orchestration — every failure
  is a secret-free code (`ce_initial_password_unavailable`, `ce_login_rejected`,
  `ce_token_creation_failed`, …). `rmm_bloodhound.py` stores the token id/key
  and the admin password as owner-local secret settings and schedules one
  background bootstrap per process when a ready CE is observed without stored
  credentials; the explicit `auth` action (REST/CLI/MCP, parity-checked)
  retries on demand and reports `already_configured` or the bounded code.
- `rmm_settings.SettingsStore.update()` now applies the saved values to the
  saving process immediately (startup environment overrides still win), so a
  bootstrapped credential works in the same session; the new secret field
  `RMM_BLOODHOUND_ADMIN_PASSWORD` joins the BloodHound group.
- Live follow-up #2: with CE finally running (the host OOM-killed it twice on
  a 3.8 GiB/swap-less box until the operator added swap and the inherited
  graph was wiped), the first real bootstrap reached the login but failed at
  token creation. Root cause in CE v7.6.2 source: the default admin is
  created with an already expired secret (`ExpireNow`), and `ValidateSession`
  restricts such sessions to `AuthManageSelf` only — `AuthCreateToken` is
  stripped, so `POST /api/v2/tokens` is refused. The bootstrap now changes
  the admin password first (`PUT /api/v2/bloodhound-users/{id}/secret`,
  generated policy-compliant password, `needs_password_reset: false`),
  re-logs in with it, and only then mints the token; the stored admin
  password is the final generated one.
- Verified live end to end on the operator's real deployment (dedicated
  Exegol on a remote host, BloodHound CE v7.6.2): after the host was resized
  (the OOM killer had taken CE twice on a swap-less 3.8 GiB box) and the
  Exegol MCP restarted, one `ensure` brought CE up, the automatic bootstrap
  changed the expired initial password, minted and stored the token, and the
  signed workflow API answered `bloodhound_ready: true` with the CE server
  version. Remaining preflight blockers are collection-setup items
  (`ce_collector_missing`, `kerberos_cache_not_configured`), not failures.
- Tests: `tests/test_bloodhound_ce_auth.py` (6 tests: banner tail parsing,
  login/token contract shapes and failure codes, end-to-end bootstrap,
  origin fail-closed) and a live-refresh regression in
  `tests/test_settings_security.py`. `make test`, `make check-parity`,
  and `make lint-md` pass. Updated
  [bloodhound-workflow.md](bloodhound-workflow.md) with the bootstrap section
  (now nine tests including the expired-secret password change and the
  policy-compliant generator).

### 2026-11-04 — Live BloodHound launch diagnosis: a 401 on the readiness probe proves CE started

- Reported live: "Open BloodHound" never worked on the operator's real
  deployment (remote Exegol container, workspace service inside it, Termius
  tunnel). Diagnosis through the workspace API and the container showed the
  launch command executed, postgres + neo4j came up, and the CE API logged
  `Server started successfully` — but the operator's BloodHound CE answers
  401 to the credential-free `GET /api/version` probe, and `probe_ce()`
  classified 401/403 as `authentication_failed`, which no call site
  (`status()`, `_check_and_start()` pre-flight, post-launch poll) treated as
  started. The launch therefore always exhausted `READY_TIMEOUT` and fell
  back to `uncertain` while CE was running fine. An earlier silent failure
  (intent `dispatched` at 08:34 with no wrapper log) was an older service
  process running the pre-logging `LAUNCH_COMMAND`; restarting the service
  loaded the logged command, whose `/var/log/rmm-bloodhound-ce.log` made the
  real behavior visible.
- Fixed by classifying, not by sending credentials: new
  `ce_started(availability)` treats both `ready` and `authentication_failed`
  as "the CE API daemon is up and answering" at all three call sites, so the
  lifecycle reaches `ready` and never restarts CE for an auth demand.
  `bloodhound_status` keeps the raw probe result (`authentication_failed`).
- The 08:34-style stuck `uncertain` also needed the documented operator
  `reset` action (cleared live via the workspace API) before a retry could
  dispatch at all.
- Tests: `test_authenticated_ce_api_counts_as_started_without_launch` and
  `test_launch_poll_treats_authenticated_ce_as_started` added;
  `test_unavailable_or_authenticated_endpoint_is_never_restarted` renamed and
  reduced to the still-failing probe results. The module keeps the same two
  pre-existing timing-sensitive failures as the baseline (verified by
  stashing the change). Updated [bloodhound-workflow.md](bloodhound-workflow.md)
  (401/403 semantics) — "configuration blocker for the signed workflow API,
  not a lifecycle blocker".
- Live follow-up: with the lifecycle fixed, the next live `ui` call reached
  `rmm_bloodhound_proxy.open_ui()` but failed with
  `bloodhound_ui_upstream_unavailable` — the proxy's own upstream check
  required a literal 200 on the same credential-free `/api/version` request.
  `open_ui()` now reuses `probe_ce` + `ce_started`, so a 401/403 answer proves
  the upstream is up and a launch ticket is minted; only an unreachable or
  unhealthy upstream refuses. `test_readiness_and_reuse_without_api_credentials`
  covers the 401-proxied case and the reuse of the started proxy instance.

### 2026-11-04 — Exegol container moves to the Workspace badge

- Reported: the Exegol container name appeared in the MCP control label
  (`MCP · Verified · <container>`). Moved it to the adjacent Workspace badge:
  `showRuntime`/`resetMcpStatus` in `web/bloodhound.js` now publish the
  container through the new `window.setRmmExegolContainer` in `web/config.js`,
  and `renderDeploymentMode` appends it to the `Workspace: <id>` badge only
  (other display modes are unchanged). The MCP control keeps its
  Verified/Checking/Check-needed summary, and the expanded panel still shows
  the container detail. Covered in `tests/test_bloodhound_ui.cjs` and
  `tests/test_plugin_releases_ui.cjs`; updated
  [Web UI configuration](web-ui-configuration.md). No operator-surface
  change, so no `check-parity` entry.

### 2026-11-04 — Workspace WebUI connection logging

- Reported (follow-up to the central-server WebUI connection logging): an
  operator connecting to their workspace WebUI still produced no server-side
  log. The workspace service serves its own WebUI through
  `WorkspaceRequestHandler._static()`, whose `log_message()` is a no-op like
  the central server's, and nothing called `WorkspaceService.log()` on the
  static path. `_static()` now logs `WebUI connection: <client-ip>` once per
  `index.html` page load — not per static asset — mirroring `_serve_web()`.
  Updated [WebUI connection logging](webui-connection-logging.md) and added a
  test in `tests/test_workspace_login.py`. No operator-surface change, so no
  `check-parity` entry.

### 2026-11-04 — WebUI connection logging, and `check_exegol_mcp.py` reads workspace settings

- Reported: nothing in the server log showed WebUI connections. Found that
  `RMMHandler.log_message()` is a no-op by design (suppresses the stdlib
  per-request access log for every route, beacon included), and neither
  `_serve_web()` nor `_handle_websocket_upgrade()` called `server.log(...)`
  the way `/register`, plugin downloads, or SOCKS events already do. Added
  one `server.log()` call per WebUI page load (on `index.html`, not every
  static asset) in `_serve_web()`, and connect/disconnect logging for the
  operator event WebSocket in `_handle_websocket_upgrade()`, both using
  `self.client_address[0]`. No new operator surface, so no `check-parity`
  entry. New doc: [WebUI connection logging](webui-connection-logging.md).
- Reported: `make check-exegol-mcp` ignored config saved from the workspace
  Config dialog. Cause: `scripts/check_exegol_mcp.py` never called
  `rmm_settings.bootstrap(...)`, so `rmm_settings.getenv()` fell through to
  raw `os.environ` only, missing `RMM_EXEGOL_MCP_URL`/`_TOKEN`/
  `RMM_EXEGOL_CONTAINER` saved to the workspace's `settings.json`. Fixed by
  calling `bootstrap("workspace", ...)` — the same call `workspace_service.py`
  makes at startup — before resolving those keys, and added `--config`/
  `--data-dir` flags matching `workspace_service.py`'s. A real environment
  variable still overrides the saved value. Updated
  [Exegol MCP verification](exegol-mcp-verification.md).

### 2026-11-04 — `make check-exegol-mcp` now forwards `--config`/`--data-dir`

- Reported (follow-up to the entry above): `make check-exegol-mcp` still
  ignored the saved config. Cause: the script already resolves
  `--config`/`--data-dir`, but the Makefile target took no arguments, so
  `make check-exegol-mcp` always ran the script with neither flag and fell
  back to the default settings location (`RMM_WORKSPACE_DATA` or
  `~/.minimal-rmm`) — not the file an explicitly-started
  `workspace_service.py --config PATH`/`--data-dir PATH` actually reads from.
  Fixed by forwarding a new `ARGS` make variable to the script, e.g.
  `make check-exegol-mcp ARGS="--config PATH"`. Updated `make help` and
  [Exegol MCP verification](exegol-mcp-verification.md).

### 2026-11-04 — Standalone Exegol MCP verification script (not an operator command)

- Request: CLI slash commands `/mcp_list_containers` and
  `/mcp_list_installed_tools` to validate Exegol MCP on a new deployment —
  explicitly not tied to BloodHound.
- Declined the slash-command shape: it would need REST/CLI/MCP parity for
  something with no operator/agent-session use case
  ([mcp-parity.md](mcp-parity.md)), duplicate the connection/validation
  logic already in `rmm_exegol_runtime.py` outside `ExegolRuntime`'s
  single-flight/identity guarantees, and print raw MCP tool output to an
  operator terminal — the exact thing the rest of this codebase is careful
  never to do.
- Added `scripts/check_exegol_mcp.py` instead: a standalone diagnostic in
  the same family as `scripts/check_operator_parity.py`, run directly by
  whoever holds the Exegol MCP credentials. It validates
  `RMM_EXEGOL_MCP_URL` without requiring `RMM_BLOODHOUND_URL`, lists
  advertised tool names and flags whether `list_exegol_containers`/
  `execute_command_in_container` are present, lists running containers
  through the same `discovery_candidates` parser the runtime uses, and runs
  the exact one-line identity `printf` probe `_check_and_start` runs before
  ever touching `bloodhound-ce` — never starting, stopping, or otherwise
  changing anything. With more than one running container and no
  `--container`, it reports them and stops rather than guessing.
- Added `make check-exegol-mcp` (not part of `make check`: it needs a real
  endpoint to mean anything) and `tests/test_check_exegol_mcp.py` (6 tests:
  single-container success, mismatched identity, ambiguous containers,
  `--no-probe`, classified connection failure, rejected container name).
  New doc: [Exegol MCP verification](exegol-mcp-verification.md), linked
  from `docs/README.md` and `docs/bloodhound-workflow.md`.
- `make check` passes with the new script added to `PY_MODULES`'s syntax
  check; the new test module passes standalone.

### 2026-11-04 — Close the remaining BloodHound/Exegol logging gaps before a live MCP verification

- Follow-up to the server-side lifecycle logging added earlier today: the
  first pass covered the CE startup state machine and successful UI access,
  but left three gaps that would specifically block diagnosing a first real
  Exegol MCP verification.
- `rmm_bloodhound_proxy.open_ui`'s failure paths now log to `runtime.log`:
  `bloodhound_ui_restart_required`, `bloodhound_ui_upstream_unavailable`
  (with the HTTP status or exception), and the catch-all
  `bloodhound_ui_proxy_configuration_or_bind_failed` — for the last one the
  real underlying reason (`bloodhound_ui_proxy_loop`,
  `invalid_bloodhound_ui_port`, a bind error, …) is logged even though the
  API response keeps returning the single generic code, so the operator
  contract is unchanged.
- `rmm_exegol_runtime._check_and_start` now logs `mcp: connected to Exegol
  MCP` as soon as the MCP session opens, a checkpoint distinct from the
  container identification that may fail afterward.
- `discover_container` now takes an optional `root` and logs how many
  running containers were found and how many matched the identity probe
  (names only, already restricted to a safe pattern), so
  `exegol_workspace_not_found`/`exegol_workspace_identity_ambiguous` are
  diagnosable from the log alone instead of only by a bucketed error code.
- All three changes are additive and keep existing call signatures backward
  compatible (`root`/`exc` are new optional trailing parameters). Verified
  manually: a 2-candidate/0-match discovery and a misconfigured
  `RMM_BLOODHOUND_UI_PORT` both produce the expected diagnostic line.
- `make check` and the targeted BloodHound/Exegol test modules pass (same
  two pre-existing timing-sensitive `test_exegol_runtime` failures as
  before, confirmed unrelated).

### 2026-11-04 — Server-side BloodHound lifecycle logging; show identified Exegol container in the WebUI

- Gap: `rmm_exegol_runtime.py`, `rmm_bloodhound_proxy.py`, and `rmm_bloodhound.py`
  had zero server-side logging. A launch failure, an `uncertain` startup, or a
  browser opening the BloodHound UI left no trace anywhere on the RMM server —
  only `/var/log/rmm-bloodhound-ce.log`, written by the wrapper inside the
  Exegol container, which RMM never reads.
- Added `rmm_exegol_runtime.append_log(root, message)`: a best-effort,
  secret-free line appended to `<root>/runtime.log` (`0700`/`0600`). Wired it
  through the single `ExegolRuntime._update` chokepoint via the new
  `_log_transition` helper, which logs once per meaningful change (`state`,
  `container`, or `error` — not per health poll): startup dispatched,
  Exegol container identity confirmed, launch command dispatched,
  `bloodhound_launch_failed` with its exit code, reaching `ready`, falling
  back to `uncertain`, and read-only health reconciling `uncertain` back to
  `ready`. `ExegolRuntime.reset()` (bypasses `_update`) logs the operator
  clear explicitly.
- `rmm_bloodhound_proxy.open_ui` now takes `root` (threaded from
  `rmm_bloodhound.bloodhound_api`'s `ui` action) and passes it to
  `BrowserProxy`, which logs listener startup, ticket issuance, session
  exchange, and session expiry into the same `runtime.log` — never the
  ticket, session token, or launch URL.
- WebUI: `web/bloodhound.js` `showRuntime` now appends the identified Exegol
  container name to the always-visible `mcp-status-label` summary button
  (previously only shown inside the collapsed detail panel), so the operator
  sees which container owns the workspace without expanding it.
- Updated `tests/test_bloodhound_surfaces.py` (`open_ui` is now called with
  `root`). Added a full doc section:
  [server-side lifecycle logging](bloodhound-workflow.md#server-side-lifecycle-logging).
- `make test`, `make check-parity`, `make check-web-contracts`, and the full
  Python test suite pass (one pre-existing `prompt_toolkit`-missing import
  error and two pre-existing timing-sensitive `test_exegol_runtime` failures,
  both unrelated — confirmed present before this change via `git stash`).

### 2026-11-04 — Diagnose permanent `bloodhound_startup_uncertain`; report confirmed dead launches

- Reported symptom: the detached-launch and reset fixes above still left
  every attempt ending in `bloodhound_startup_uncertain`.
- Investigation went down to the real execution path: cloned Exegol-MCP
  (pinned `e9fe4e4`) and the Exegol/Exegol-Images sources. `execute_command_in_container`
  runs commands through `ExegolContainer.exec_raw`: an attached `docker exec`
  of `zsh -c 'autoload -Uz compinit; compinit; source ~/.zshrc; eval "$CMD"'`,
  returning synchronously with exit code and output. Reproduced the exact
  dispatch in a real Ubuntu container: the detached `LAUNCH_COMMAND` mechanics
  are sound — the exec returns within seconds and the `setsid`-detached
  process survives the exec session with output redirected. The Exegol
  `bloodhound-ce` wrapper is a bash script that starts postgresql, starts
  neo4j (its own wait loop allows 60s), then runs the CE API binary in the
  foreground.
- Root cause of the persistent symptom: `rmm_exegol_runtime.py` discarded the
  launch reply entirely (`except Exception: pass`, result never read) and the
  old command redirected all wrapper output to `/dev/null`. Any instant,
  deterministic launch failure inside the container (missing binary, wrapper
  exit on service errors) was therefore invisible: the runtime waited out
  the poll window, reported `uncertain`, persisted the `dispatched` intent,
  and locked every later check into the same `uncertain` state — exactly
  "uncertain every time". A second contributing defect: the 90-second
  readiness window is shorter than a legitimate cold start (the wrapper
  alone budgets 60s for neo4j).
- `LAUNCH_COMMAND` now keeps the wrapper's output in a root-only log inside
  the container (`/var/log/rmm-bloodhound-ce.log`, created 0600) and reports
  bounded markers through the exec reply: `RMM_CE_LAUNCH_RUNNING` after a
  two-second liveness check, or `RMM_CE_LAUNCH_EXITED:<code>` with the
  wrapper's exit status when it died immediately.
- `_check_and_start` reads the reply and classifies: a non-zero EXITED marker
  is a proven dead launch, so the durable intent is cleared and a definite
  `bloodhound_launch_failed` error (with the bounded `launch_exit_code`
  field) is reported; the next `ensure`/`ui` retries directly without a
  manual `reset`. A lost reply, an unrecognized reply shape, or a zero exit
  keeps the existing never-replay `uncertain` semantics. The runtime never
  ingests the log file's content (it may contain CE first-run credentials);
  the operator inspects it inside the container.
- Raised `READY_TIMEOUT` 90→240 and `MCP_TIMEOUT` 120→300 for real cold
  starts; the WebUI open-flow wait budget 120→270 seconds with a friendly
  message for `bloodhound_launch_failed` pointing at the container log.
  Updated the MCP/AI tool description; no new actions, so
  `make check-parity` is unchanged (55 tools).
- Validation: reproduced the healthy (`RMM_CE_LAUNCH_RUNNING`, surviving
  detached listener, 0600 log) and instant-failure (`RMM_CE_LAUNCH_EXITED:7`,
  `:127` for a missing command) marker paths in a real container through the
  exact Exegol zsh wrapper before coding. `tests/test_exegol_runtime.py`
  (24 tests) covers the definite-failure/retry-without-reset path, the
  zero/markerless replies staying `uncertain`, and the new timeout bound
  relationship; `tests/test_bloodhound_ui.cjs` covers the new budget and the
  launch-failure message. Related suites (71 tests), `make check-parity`,
  and the browser contract checks pass. Live Exegol re-verification by the
  operator is still required: retry a BloodHound start after deploying this
  change — an instant failure will now name itself.

### 2026-11-04 — Improve Config and Embeddings dialog usability

- Found and fixed a real visual bug: unlike every other dialog in the app
  (`.ui-config-dialog`, `.session-tools-dialog`, `.beacon-config-dialog`,
  `.server-unreachable-dialog`), `.embedding-dialog` had no `background`,
  `color`, `border`, or `::backdrop` rule. It rendered with the browser's
  default white dialog chrome regardless of the selected theme, clashing hard
  with dark mode. Gave it the same `var(--surface)`/`var(--text)`/
  `var(--border)`/`var(--shadow-modal)` treatment as the other modal dialogs.
- Both dialogs are also long; a flat wall of fields made them hard to scan and
  the only close control could require scrolling to reach.
- Config: the header (`.ui-config-header`) is now sticky, and a new quick-jump
  nav (`#ui-config-nav`, `renderConfigNav` in `web/config.js`) lists every
  top-level section so an operator can open and jump to one directly instead
  of scrolling past four collapsed, unrelated sections. Each pill tracks its
  section's open state via `aria-pressed`, including when the section is
  toggled directly from its own `<summary>`.
- Embeddings: added a sticky header (`.embedding-dialog-header`) with the
  close button, replacing the close button previously only available at the
  bottom of a very long form. Numbered the five workflow steps (profile,
  labels, local copies, run, save results) in their fieldset legends/heading
  for a scannable sequence instead of one undifferentiated page.
- No element `id`s changed; `scripts/check_web_contracts.py`,
  `tests/test_shell_completion.cjs`, and `tests/test_embeddings_shell.cjs`
  still pass. Documented in `docs/web-ui-configuration.md` and
  `docs/document-embeddings.md`.

### 2026-11-04 — Add an operator-authorized reset for stuck BloodHound `uncertain` startups

- Reported symptom: after the detached-launch fix below, **Open BloodHound**
  still immediately failed with "Cannot open BloodHound: bloodhound_startup_uncertain"
  on every click, with the tab opening and then closing right away.
- Root cause: this is the deliberate, tested "never replayed" guarantee
  (`test_lost_reply_uncertain_never_replayed_after_restart` in
  `tests/test_exegol_runtime.py`) working exactly as designed — a dispatch
  attempt before today's detached-launch fix left a durable
  `uncertain`/`dispatched` intent (`ce-startup.json`) on disk, and `ensure()`
  and `_check_and_start()` both refuse to redispatch while that intent stands,
  to guarantee a lost MCP reply can never cause two `bloodhound-ce` processes
  to be launched. There was previously no operator-facing way to clear that
  state short of editing files on the server, so the button stayed permanently
  stuck even after the launch bug itself was fixed.
- Added an explicit `reset` BloodHound action
  (`ExegolRuntime.reset()` in `rmm_exegol_runtime.py`): refuses unless the
  runtime is currently `uncertain` (not mid-startup, not already `ready`),
  then clears the persisted dispatch intent and in-memory snapshot so the next
  `ensure`/`ui` call makes a clean attempt. It never touches `bloodhound-ce`
  itself or its data — only the server's own bookkeeping.
- Exposed consistently per operator-surface parity: REST `/bloodhound`
  action `reset`, CLI `bloodhound reset`, shared `tool_bloodhound`/MCP
  `bloodhound(action="reset")`, workspace broker `ACTIONS`, and WebUI
  `BLOODHOUND_ACTIONS`. Added a **Retry BloodHound startup** button beside
  **Open BloodHound** that appears only while the runtime is `uncertain`;
  clicking it calls `reset` and immediately re-attempts the open flow, and the
  **Open BloodHound** error message for `uncertain` now explains the stuck
  state and points at that button instead of surfacing the raw error code.
- Documented in [BloodHound CE service startup](bloodhound-workflow.md#bloodhound-ce-service-startup)
  and [MCP parity](mcp-parity.md).
- Validation: `tests/test_exegol_runtime.py` and `tests/test_bloodhound_surfaces.py`
  cover `reset()`'s success path, its three refusal cases, and the action's
  presence across CLI/AI/MCP schemas. `tests/test_bloodhound_ui.cjs` covers the
  WebUI retry button's visibility and the reset-then-reopen flow.
  `make check-parity` and `PYTHON=.venv/bin/python3 make check` pass (493+ tests;
  `make check` alone picks up the system Python 3.9, which lacks `prompt_toolkit`
  for `test_cli_slash_commands` and is unrelated to this change — use the
  `.venv` interpreter). No live Exegol/CE verification was performed in this
  session.

### 2026-11-04 — Dispatch bloodhound-ce detached so startup can actually finish

- Reported symptom: BloodHound CE start from the WebUI/Exegol MCP always
  ended in `bloodhound_startup_uncertain` (`rmm_exegol_runtime.py`'s 90-second
  `READY_TIMEOUT` poll never saw CE's `/api/version` answer), even though
  `command -v bloodhound-ce` found the launcher and running it by hand in an
  interactive shell worked and listened on `127.0.0.1:1030` fine.
- Root cause: `_check_and_start()` dispatched the bare `bloodhound-ce` command
  through the Exegol MCP `execute_command_in_container` tool. `bloodhound-ce`
  runs in the foreground and never exits on its own, so the exec call blocked
  for the life of the server; the existing 30-second `wait_for`/`except
  Exception: pass` around that call (added on the assumption that only the
  *reply* could be lost, not the process) then either timed out or had the
  process killed with the exec session, leaving nothing running in the
  container — confirmed live: `ps aux` showed no bloodhound-ce process at all
  after a dispatched run that reported uncertain.
- Fixed by dispatching a new `LAUNCH_COMMAND` constant instead of the bare
  command: `setsid nohup bloodhound-ce >/dev/null 2>&1 </dev/null & disown`,
  fully detaching the process from the exec session so it survives the exec
  tool call returning. Updated the six `tests/test_exegol_runtime.py`
  assertions that pinned the literal `'bloodhound-ce'` command string to use
  the new constant instead. Documented the reasoning in
  [BloodHound CE service startup](bloodhound-workflow.md#bloodhound-ce-service-startup).
- Validation: `tests/test_exegol_runtime.py` (19 tests; one pre-existing,
  unrelated flake — `test_read_only_health_reconciles_uncertainty` fails only
  under the system Python 3.9 outside `.venv`, and reproduces identically on
  unmodified `v2`). Full suite under `.venv` (493 tests) and `make check`
  (parity, web contracts, lint) pass. No live Exegol/CE re-verification of the
  detached dispatch was performed in this session; the operator should retry
  a BloodHound start and confirm it now reaches `ready`.

### 2026-11-04 — Support mcp>=2.0's FastMCP→MCPServer rename

- `requirements-mcp.txt` pins `mcp>=1.6.0` unbounded, so an operator can end up
  with mcp 2.x, which renamed `mcp.server.fastmcp.FastMCP` to
  `mcp.server.mcpserver.MCPServer` and removed the old module. `mcp_rmm_server.py`
  still imported the old name, so every MCP tool call failed to even import
  under mcp 2.x (`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`).
- Fixed with a narrow try/except: import `FastMCP` as before, and only fall
  back to `MCPServer` (aliased as `FastMCP`) when the failing import is
  specifically `mcp.server.fastmcp` — a missing `mcp` package entirely still
  raises its original error. The `.tool()` decorator and `.run()` call used
  throughout the file are unchanged between the two classes, so no other
  edits were needed.
- `mcp.server.mcpserver.Tool` also dropped the `inputSchema` alias for plain
  attribute access (pydantic 2 strict mode); fixed the two tests that read it
  directly off a registered tool (`tests/test_ai_concurrency.py`,
  `tests/test_bloodhound_surfaces.py`) to fall back to `input_schema`.
- Validation: `mcp_rmm_server.py` now imports cleanly and registers all 55
  tools under the installed mcp 2.3.0. `make check-parity` still reports 55
  MCP tools / 6 web shell meta commands (no operator-surface change). Full
  suite (493 tests) now has only the 5 pre-existing process-timing failures
  (`test_deployment_smoke`, `test_rmm_settings`, `test_server_shutdown` x2,
  `test_workspace_shutdown`) that reproduce identically on `v2` with none of
  this branch's changes — confirmed unrelated.

### 2026-11-04 — Finish Exegol MCP client SDK-version compatibility

- Fixed `tests/test_helper_shutdown.py::test_http_initialization_failure_releases_all_contexts`,
  left failing by the prior session: it still faked `httpx.AsyncClient` directly,
  but `McpHttpSession.__aenter__` now builds its client via
  `mcp.shared._httpx_utils.create_mcp_http_client`. The test now fakes that
  function instead, restoring the `enter:http`/`exit:http` event assertions.
- Verified the dual-field schema/result handling in `rmm_mcp_client.py` and
  `rmm_exegol_runtime.py` against an actually-installed MCP SDK (not just the
  skipped fallback path): confirmed the real SDK in this environment exposes
  `Tool.input_schema` (not `inputSchema`) and `CallToolResult.structured_content`
  (not `structuredContent`), so the added fallbacks are exercised for real, not
  just unit-tested against hand-built stand-ins.
- Validation: full suite (`.venv/bin/python3 -m unittest discover -s tests`,
  493 tests) run before and after this fix. Remaining failures
  (`test_deployment_smoke`, `test_rmm_settings`, `test_server_shutdown`,
  `test_workspace_shutdown`, and the `mcp_rmm_server` `FastMCP` import errors
  in `test_ai_concurrency`/`test_bloodhound_surfaces`) reproduce identically on
  `v2` with none of this session's changes applied — confirmed pre-existing
  and out of scope (the latter traces to an incompatible `mcp` SDK build
  present only in the local dev `.venv`, unrelated to the `mcp>=1.6.0` pin in
  `requirements-mcp.txt`). `make check-parity` passed (no operator-surface
  change). Live Exegol container verification still not performed.

### 2026-11-04 — Preserve MCP tool schemas and nested runtime codes

- Read both SDK `input_schema` and legacy `inputSchema` fields when converting
  tools; preserve execution arguments instead of substituting an empty schema.
- Preserve explicit `RuntimeErrorCode` diagnostics inside MCP exception groups.
- Add regression coverage for both schema representations and wrapped errors.
- Validation: 19 runtime tests and the schema regression passed; two SDK result
  tests skipped because the local SDK is absent. `git diff --check` passed.
  Live Exegol verification remains pending.

### 2026-11-04 — Explicit HTTP MCP client dependency

- Declare `httpx` directly in `requirements-mcp.txt`; the Exegol connection
  imports it and must not rely on another package installing it transitively.
- `requirements.txt` includes these requirements, so native setup and the
  Docker image both receive the dependency. Existing environments need to
  reinstall requirements and restart their workspace process.
- Validation: `git diff --check` passed. The local SDK import smoke check
  reproduced missing `httpx`; runtime verification requires installing the
  updated requirements. The Exegol container was not accessed.

### 2026-11-04 — Slash commands and live completion

- Follow-up review fixed event-reloaded history: only raw `queued`/`exec`
  operator events are replayable, using structured command fields when present.
  Incomplete exfiltration, pivot, and configuration audit labels are excluded.
  Added two regression tests; corrected the remaining `/download` parity example.
- Require `/` for internal standalone CLI and WebUI shell commands. Show the
  command menu on `/`, filter while typing, select with arrows, insert with Tab,
  and dismiss with Escape. Unknown slash commands stay local.
- Preserve raw shell dispatch and one-shot CLI, REST, and MCP syntax. Retain
  slash prefixes in new history entries; exclude non-replayable panel labels.
- Update help, command guides, parity checks, and `make check` coverage.
- Validation: 15 CLI tests, including real prompt-toolkit keyboard input;
  16 focused Node tests; full `make check` including parity and Markdown/YAML
  lint passed. Code, security, test, and frontend reviews found no remaining
  blockers. No live browser layout or connected-agent acceptance test was run.
- The configured venv lacked the existing `prompt_toolkit` requirement. Tests
  used an isolated `/tmp/rmm-slash-test-deps` installation through `PYTHONPATH`;
  the project venv was unchanged. Localhost redirect tests required execution
  outside the restricted sandbox. No deployment or commit was performed.

### 2026-11-03 — Second review across RMM, Profiles, and Plugins

- Reject credential-forwarding redirects across operator/workspace clients,
  Go/PowerShell agents, and SOCKS/TUN plugins.
- Fix corrupt asset inventory handling, URL credential redaction, HTTP input
  validation, workspace event pagination, and literal session resolution.
- Validate profile input before transport changes and isolate cloudflared's child
  environment from central/workspace secrets.
- Preserve zero jitter and guard browser build/upload actions across login changes.
- Register PowerShell sessions before command dequeue to avoid discarding work
  when registration fails; command delivery still has no acknowledgment/replay.
- Fix destructive rclone directory synchronization, release filename validation,
  and TUN socket cleanup in the Plugins repository.
- Validation: 485 RMM Python tests, 12 Profiles tests, 23 Plugins Python tests,
  host Go/race suites, Windows/Linux agent cross-builds, PowerShell redirect and
  SOCKS shutdown regressions, Node suites, `make check`, Compose/shell/whitespace
  checks. Checked 303 relative file links across 94 Markdown files.
- See the [cross-repository review](cross-repository-review.md) for findings and
  deployment limitations. Source changes are uncommitted; plugin artifacts and
  remote lab services are unchanged.

### 2026-11-03 — Review architecture, ownership, and operator surfaces

- Fix workspace authorization gaps, deployment identity overrides, event cursor
  handling, session-prefix isolation, and overlapping canceled AI jobs.
- Repair CLI dispatch and JSON output, structured MCP results, stale browser
  responses, and delayed BloodHound launches after logout.
- Reconcile plugin publications periodically with serialized mutations, canonical
  aliases, and corrupt-inventory protection; wait for workers before closing SQLite.
- Preserve hot rollback journals during migration and reject duplicate workspace IDs.
- Update deployment, storage, plugin, history, and embedding documentation.
- Validation: 468 Python tests passed, along with `make check`, additional Node
  suites, Compose validation, shell syntax, whitespace, and 286 relative file links.
- See the [architecture review](architecture-review.md) for findings and remaining
  live-lab, shared-secret, durability, and compatibility limitations. No remote
  deployment was changed and no live Exegol or BloodHound operation was performed.

### 2026-11-03 — Verify same-name plugin delivery and revocation

- Test actual plugin serving for Alice and Bob with different bytes under the
  same filename, including a change of session ownership.
- Fix deletion reporting success when central revocation failed: retain a disabled
  workspace record and return 502 until a retry confirms removal from the cache.
- Document the shared beacon-secret trust boundary and cache revocation delay.
- All 446 Python tests, `make check`, Compose configuration, and whitespace
  checks passed. No live lab execution was performed.

### 2026-11-03 — Make central plugin storage a delivery cache only

- Remove global plugin fallback and central installation/source management.
  Resolve agent downloads and pivot readiness exclusively through session ownership.
- Use `RMM_PLUGIN_CACHE_DIR` (default `central/plugin-cache`) for authenticated
  workspace publications. Old `RMM_PLUGINS_DIR` settings remain readable but ignored.
- Remove the Compose plugin-store mount and central GitHub credential override;
  retain central profile storage. Preserve old files and document workspace migration.
- Add owner isolation, missing-owner, global-fallback rejection, central CRUD
  rejection, and scoped publish/disable regression coverage.
- Validation: all 444 Python tests passed with isolated MCP dependencies;
  `make check`, Compose configuration validation, shell syntax, and whitespace
  checks passed. No live Exegol deployment or artifact migration was performed.

### 2026-11-03 — Verify simplified BloodHound UI and connectivity

- Confirm removal of form markup, event handlers, and obsolete current documentation;
  preserve Open BloodHound, runtime status, and advanced operator commands.
- Ignore canceled requests and responses from a previous connection when updating
  connectivity indicators. Add regressions for cancellation, logout races, failure,
  and recovery, without changing the workspace's HTTP polling transport.

### 2026-11-03 — Remove the raw BloodHound operations form

- Remove the main-page action selector, run ID, JSON editor, and raw response
  form with its unused JavaScript. Keep Open BloodHound and connection status.
- Preserve CLI, MCP, REST, and intercepted WebUI shell actions. Update current
  workflow and parity documentation to reflect the simpler main page.

### 2026-11-03 — Correct the connection dot, not the title

- Restore the original title text color. The adjacent status dot now reflects
  successful HTTP responses in workspace mode, clears after failed requests or
  disconnect, and retains WebSocket status for central administration.
- `setWsStatus` keeps WebSocket transport state separate from the polling
  indicator, so workspace polling remains enabled when the dot is green.

### 2026-11-03 — Verify container-to-host MCP assumptions

- Document that bridge-mode workspace processes require a reachable host MCP
  address and listener; publishing RMM ports does not provide outbound MCP access.
- Classify nested connection, authentication, HTTP, dependency, and timeout
  failures without exposing exception text. Preserve uncertainty after dispatch.
- All 440 Python tests and `make check` passed, including UI status and login
  routing regressions. The remote Ubuntu/Exegol MCP network path remains unverified.

### 2026-11-03 — Compact workspace connection status

- Move Exegol/BloodHound readiness into a collapsible MCP control beside the
  workspace badge, with last-check semantics and explicit on-demand RMM MCP status.
- Clear verified indicators after disconnects or failed checks; retain diagnostic
  codes in the details panel. Restore green login and header titles.
- Add UI regressions for verified, failed, unavailable, and disconnected states.

### 2026-11-03 — Canonical WebUI entry URL

- Redirect `/ui` to `/ui/` on the workspace and central listeners so relative
  CSS and JavaScript URLs resolve correctly. Previously `/ui` displayed unstyled
  HTML and requested assets outside their public static route.
- Extend workspace login HTTP coverage to bare and query-bearing `/ui` URLs.

### 2026-11-03 — Repeat failure-path and deployment verification

- Isolate central and workspace child environments while preserving valid scoped overrides.
- Persist relay journal directory entries before dispatch and after result publication.
- Keep upload staging until the stream receipt commits; reject missing prefixes and
  roll back failed event transactions. Regression tests cover interrupted finalization.
- Move CLI connection diagnostics to stderr so JSON stdout remains parseable.
- Add a real-process single-preset smoke test for authenticated workspace access,
  rejection of the central administrator token, CLI output, and shutdown.
- All 438 Python tests passed with isolated MCP dependencies and no skips.
  `make check` passed, including parity for 55 MCP tools and six WebUI shell
  commands, browser contracts, and Markdown/YAML lint. AI job recovery and all
  20 plugin UI tests passed; whitespace checks were clean.
  Documentation link audit checked 279 relative links with none broken.
  Live Exegol/CE and production migration remain untested.

### 2026-11-03 — Audit unified workspace routing and event continuity

- Fix CLI/external MCP default URL to workspace port 8787 and route central
  WebAI workspace-owned JSON tools through authenticated owner-local dispatch.
  Add a real localhost HTTP relay test, scope checks, bounded results, timeout
  recovery, and durable mutation replay protection.
- Add central event epochs and durable envelope receipts. Workspace transcript
  IDs remain monotonic across migrated history and central restarts; central
  exec response IDs no longer advance the CLI's workspace cursor.
- Derive generated workspace central URLs from imported listener settings;
  reject malformed deployment metadata and mismatched workspace identity.
- Correct current startup/storage/operator documentation; 274 relative file
  links across 78 Markdown files checked without broken links.
- Validation: all 432 Python tests passed with no skips using isolated MCP
  dependencies. `make check`, AI job UI recovery, 20 plugin UI tests, and
  whitespace checks passed. Live Exegol/CE and production migration remain untested.

### 2026-11-03 — Unify single and multiple workspace deployments

- Central runtime now requires workspace ownership; legacy server storage and
  embedded CLI startup fail with migration guidance. Central starts no CE runtime.
- Add `rmm_deployment.py` preparation and process supervision for single/multi
  presets, generated scoped credentials, and optional saved central settings import.
- Assign new sessions immediately when exactly one workspace is registered;
  preserve prior ownership and multi-workspace claiming behavior.
- Add copy-only legacy migration with source archive, readable history/artifact
  imports, compatible settings merge, and explicit archive-only state limitations.
- Central UI presents administration and skips AI/BloodHound polling; operators
  use the workspace UI, CLI, and MCP endpoint. See the deployment and migration guides.
- Validation: all 417 Python tests passed with isolated MCP dependencies;
  `make check` passed including operator parity, UI contracts, and Markdown/YAML lint.
  No live Exegol deployment or operator data migration was performed.

Container discovery validation: all 402 Python tests passed with isolated MCP
dependencies; `make check` and whitespace checks passed. No live Exegol/CE
execution was performed for this change.

### 2026-11-03 — Discover the owning Exegol container automatically

- Make `RMM_EXEGOL_CONTAINER` optional: use the installed MCP container-list
  tool and require exactly one running candidate to pass workspace identity
  verification before starting or reusing CE. Explicit overrides remain verified.
- Bound discovery output and identity probes; expose the selected container
  and distinct discovery failures without starting stopped containers.
- Update deployment guidance and mark discovery complete in the backlog.
  Separate-host execution over Tailscale/WireGuard remains planned; shared
  filesystem/network-namespace requirements are unchanged.

### 2026-11-03 — Plan separate Exegol deployment

- Add same-host container separation and remote Exegol over Tailscale/WireGuard
  to the technical backlog, including workspace identity, optional container
  discovery, reachable endpoints, browser proxy access, and reconnect tests.
- No runtime or deployment behavior changed; current namespace restrictions
  still apply.

### 2026-11-03 — AI concurrency technical and surface audit

- Rechecked scheduler ordering, failure handling, caller-scoped resolution,
  direct execution, and registered MCP handlers. No new implementation defect
  was found; endpoint communication files remain unchanged.
- Clarified numeric limit clamping, conversation-local host ordering, and
  WebUI/CLI/MCP submission, inspection, cancellation, and configuration boundaries.
- All 398 Python tests passed without skips using cached MCP dependencies;
  all 65 JavaScript checks passed. `make check` passed, including operator parity
  (55 MCP tools), WebUI contracts, and Markdown/YAML lint.
- Live endpoint latency and Vectra detection remain untested.

### 2026-11-03 — Bounded AI orchestration concurrency

- Run approved independent tools concurrently within a model round, with a
  default bound of four and environment-only serial rollback.
- Resolve session references through the caller's API scope; preserve order for
  commands sharing a session or hostname. Unknown tools and mutations remain
  barriers. Preserve provider call IDs and transcript order.
- Offload blocking direct calls and approved MCP handlers; preserve endpoint
  polling, jitter, command delivery, and result submission behavior.
- Document scheduling and cooperative cancellation in `ai-background-jobs.md`.
- All 398 Python tests pass with no skips, including fourteen concurrency tests
  and registered MCP dispatch with cached MCP dependencies. `make check` and
  `git diff --check` pass; code and manual security reviews found no remaining
  blockers. Live endpoint/provider performance and Vectra detection remain untested.

### 2026-11-03 — Repeat technical verification

- Correct read-only relay result retention to start thirty seconds at completion,
  not submission; add a slow-reply regression test for health and library actions.
- All 384 Python tests passed with no skips using cached MCP 1.30.0 dependencies
  in an isolated temporary directory. Registered MCP invocation now passes;
  the downstream API is mocked and live Exegol remains untested.
- Rechecked operator parity, browser contracts/tests, documentation lint and
  links, catalog counts/provenance, AI job UI recovery, and whitespace.

### 2026-11-03 — Technical, operator parity, and documentation audit

- Fix BloodHound fallthrough in both interactive shells; add a session-independent
  WebUI operations form and action-set parity checks for all fifteen actions.
- Invalidate Exegol readiness on owner configuration changes, reject malformed
  API options, and prevent partial-file ingestion failures from becoming ready.
- Harden library metadata errors and GUID lookup; add a real localhost HTTP
  discovery test, browser dispatch tests, and a registered MCP invocation test.
- Correct owner configuration, skill loading, workflow, and reference-library
  documentation. See the [audit findings and parity matrix](bloodhound-technical-audit.md).
- Full Python suite: 383 tests, 382 passed, one skipped because the development
  interpreter lacks the MCP SDK. Live Exegol/CE validation remains pending.
- `make check` passed (parity, WebUI contracts, browser/shell tests, lint), as
  did AI job UI recovery tests and `git diff --check`.

### 2026-11-03 — Import the complete official BloodHound query library

- Preserve all 236 queries from the pinned SpecterOps snapshot, including
  original Cypher/metadata, raw source, revision, paths, and checksums.
- Add paginated metadata search and single-entry retrieval across REST,
  CLI, MCP, and AI, without injecting the library into the model prompt.
- Keep originals reference-only and map reviewed adaptations to the separate
  sixteen-template execution allowlist; expose conservative compatibility notes.
- Provide a deterministic local importer and tests; no live queries or
  automatic upstream updates are performed.
- Validation: all 374 regression tests passed, followed by all four catalog
  tests after adding a source-mapping regression check. `make check`, AI job UI
  checks, and `git diff --check` passed. Live Exegol/CE validation remains pending.

### 2026-11-03 — Name web skills explicitly

- Rename all sixteen web skill files and IDs from `offensive-*` to `web-*`;
  update internal skill references while preserving upstream attribution URLs.
- Migrate saved selections and disable flags across server/workspace/browser;
  keep NetExec and privilege-escalation IDs unchanged and optional bodies lazy.

### 2026-11-03 — Expand the DCOnly BloodHound investigation catalog

- Expand four templates to sixteen bounded graph queries using pinned
  SpecterOps query-library and Python collector sources with MIT attribution.
- Cover privileged membership, account flags, delegation, direct object/GPO
  control, replication-right candidates, and foreign group membership.
- Enforce requested-method coverage before network execution and preserve
  query provenance, catalog hash, interpretation, and completeness warnings.
- Expose non-executable deferred ideas for local privileges, sessions/local
  administrators, and ADCS. PR #1999 was closed unmerged; its proposed local
  privilege edges are not assumed available in the deployed CE schema.
- Update analysis guidance; see [query catalog](bloodhound-query-catalog.md).
  Live CE query-engine compatibility remains a deployment validation step.
- Validation: 362 Python tests, `make check`, and whitespace checks passed;
  query tests use fixtures and do not contact the Ludus directory or live CE.

### 2026-11-03 — Separate AD collection and adapt Claude-Red investigation skills

- Split Python BloodHound CE/DCOnly collection from evidence-driven graph
  analysis. Preserve legacy skill selections and explicit disablement.
- Adapt NetExec, Windows/Linux privilege escalation, and all 16 upstream web
  skills as individual on-demand entries with pinned provenance and MIT notice.
- Keep two core skill bodies in the initial prompt; optional bodies remain
  lazy. Add discovery and selection-migration regression coverage.
- Add ldeep as a second-method backlog item with explicit coverage and output
  compatibility work. It is not implemented or used as a fallback.
- See [Claude-Red adaptations](claude-red-adaptations.md). No live lab actions
  or technique validation were performed while editing these skills.
- Validation: 359 Python tests, AI/BloodHound UI logic tests, `make check`,
  and whitespace checks passed. Stale combined-skill overlays are excluded.

### 2026-11-03 — Require Exegol and manage CE startup deterministically

- Require the configured Exegol operator workspace for AI and mutating
  BloodHound operations; preserve configuration and agent-management access
  when prerequisites fail. The separate central server remains independent.
- Start the CE lifecycle in the workspace/standalone service through MCP,
  with container/namespace verification, single-flight startup, and durable
  uncertain-dispatch handling. No model or loaded skill controls startup.
- Add shared `health` and `ensure` actions and WebUI readiness. Reconnects
  inspect health; opening CE can recover a confirmed stopped service.
- Keep DCOnly collection and the dedicated-origin browser proxy. Exegol MCP,
  CE local login, and Ludus behavior still require live deployment validation.
- Resolve central AI MCP settings through a private owner-runtime relay;
  ignore browser overrides and keep credentials out of the disk journal.
- Validation: 355 Python tests, both browser logic suites, `make check`, and
  whitespace checks passed. No live deployment was performed.

### 2026-11-03 — Open BloodHound through the workspace or standalone proxy

- Add an owner-local, separate-origin browser proxy and an Open BloodHound
  WebUI button. REST/CLI/MCP expose the same `ui` launch action.
- Exchange short-lived one-use links for browser admission while retaining
  CE's own login. Keep adapter credentials out of browser requests.
- Document remote workspace port/hostname configuration, local-login scope,
  session lifetime, and the distinction from the central workflow relay.
- Added proxy transport/security, UI launch, and operator-surface tests.
  Live Exegol CE and Ludus browser validation is still pending.

### 2026-11-03 — Route BloodHound CE startup through Exegol MCP

- AD workflow now checks/reuses CE in the selected workspace container and
  starts the existing service through Exegol MCP only when needed.
- Separate service startup from adapter collection/import/query operations;
  require authenticated readiness and retain DCOnly enforcement.
- No automatic install, database reset, credential reset, or live startup.

### 2026-11-03 — Add persisted BloodHound CE collection and graph workflow

- Add owner-local REST, CLI, MCP, and Web AI actions for readiness, run creation,
  asynchronous collection/import, ingestion polling, verification, and queries.
- Relay central AI calls to their authenticated workspace with cached replies
  and durable local intent so request redelivery does not repeat operations.
- Use an existing matching Kerberos cache and CE collector; validate CE v6
  artifacts, hashes, expected identities, and complete ingestion before queries.
- Save bounded query evidence and directed candidate paths. Preserve ambiguous
  operations without automatic replay; keep detection explicitly unverified.
- Add a four-query catalog and rewrite the AD skill around the actual adapter.
  See [workflow](bloodhound-workflow.md) for setup, limits, and live validation.
- Tests cover a signed loopback CE API and failure/recovery cases. No live
  Ludus collection, BloodHound deployment, or detector validation performed.

### 2026-11-03 — Load optional AI skills on demand

- Inject only operator boundaries and completion reporting initially; expose
  optional metadata and a read-only `load_skill` tool for both tool transports.
- Keep per-run snapshots, exact-ID lookup, duplicate-load suppression, and
  truthful loaded-ID progress. Saved exclusions remain respected.
- Without an explicit selection, server and workspace offer all enabled skills.
  Add tests for body isolation, lookup rejection, per-run state, and collisions.

### 2026-11-03 — Align lab skills with demo execution and evidence

- Document the targeted Claude-Red comparison without importing its catalog.
  PCAP recording is a project outcome in the Ludus range, not an AI skill.
- Bound prerequisite discovery to the task, clarify route authorization,
  preserve protocol requirements across collector fallbacks, and retain
  completion evidence before retrying long operations.
- Document deployment trials and runtime limitations in
  [lab skill review](lab-skill-review.md). No live lab validation performed.

### 2026-11-03 — Show AI execution context and enforce Exegol prerequisites

- AI chat shows the resolved tool path, MCP and Exegol status, and loaded skill
  IDs from persisted job progress, including after reconnecting.
- Exegol requests fail explicitly when MCP is disabled or unavailable.
- Resolve skills once per run and distinguish loaded instructions from proven
  execution. Runtime details exclude URLs, credentials, and skill bodies.

### 2026-11-03 — Recover model request timeouts without replaying tools

- Retry model transport timeouts twice with bounded backoff, preserving the
  current conversation and completed tool results for all three providers.
- Check interruption before retrying and retain exception diagnostics on
  exhaustion. HTTP errors and malformed responses still fail without retry.
- Add regression coverage for recovery, exhaustion, and interruption; see
  [background jobs](ai-background-jobs.md) for runtime limits.

### 2026-11-02 — Retune AI skill guardrails to destructive-actions-only consent

- Skills no longer gate movement and progression: target selection, subnet
  probing, reuse of found secrets, lateral technique switching, SOCKS/TUN
  pivot start, directory queries (ADWS/LDAP/legacy fallbacks), BloodHound
  collector switches, agent deployment, and HF classification with copy
  staging and rclone transfer now proceed autonomously within the lab.
- `operator-boundaries` remains the destructive-action gate: settings,
  password/credential changes, and pre-existing file deletion/overwrite/move
  still require explicit consent; agent-created files may be cleaned up freely.
  The no-installation rule on execution hosts is unchanged.
- Added file deletion to the consent list (previously implicit), a
  destructive-only scope paragraph so the AI does not over-ask, and the
  persistence (`install_persistence`) gate is retained.
- Technical verifications (upload delivery, relay readiness, registration
  proof, secret redaction) are unaffected.

### 2026-10-30 — Verify cross-session shell routing and operator parity

- Commands retain their captured full session ID across asynchronous UI work;
  late responses cannot write into another selected shell or clear its input.
- Events and successful exec responses carry session identity. Rejected scoped
  exec lookups fail closed instead of falling back to global session lookup.
- MCP, shared AI tools, CLI, and WebUI support forward transcript pagination;
  CLI `events --history` reads archived transcripts. Added two-session provider,
  browser response-order, and operator surface regressions.
- Documentation distinguishes originating AI chat/job from each tool's target
  session and explains the limits of session-bound job cancellation.

### 2026-10-30 — Recover shell transcripts and interrupt killed-session AI jobs

- Live and archived shells retrieve retained events in forward pages beyond the
  500-event live buffer; reconnect notifications trigger ordered recovery.
- Session deletion interrupts associated AI jobs and releases pending execution
  waits. Concurrent executions are rejected; unrelated echoed results and
  operator audit events cannot complete a command wait.
- Per-session event publication is serialized so workspace cursors cannot skip
  a delayed lower event ID. `_record_event` owns the session event lock;
  `_record_event_ordered` persists and publishes while that lock is held.
- Regression coverage includes transcript pagination, stale UI responses, job
  interruption, waiter release, and concurrent event publication. Remote exit
  remains cooperative at the next server contact. See
  [transcript recovery](shell-transcript-recovery.md) and
  [background jobs](ai-background-jobs.md).

### 2026-10-30 — Confirm artifact delivery before deployment

- Deployment and lateral-movement instructions now require a matching agent
  upload success result before dependent copy, extraction, or execution.
- Record an event cursor before queuing, exclude stale results, and verify the
  staged digest when overlapping transfers make attribution ambiguous.
  Unresolved delivery stops dependent actions without automatic re-upload.

### 2026-10-30 — Autonomous agent deployment for the AI (server-configured artifacts)

- Added `POST /api/v1/agent/deploy/script`: renders `client_rmm.ps1` with the
  active beacon URL and the server's own `RMM_BEACON_SECRET`, stores it under
  `RMM_logs/agent-deploy/` (served by `/artifacts/agent-deploy/`), and returns
  path/size/SHA-256 — never the secret.
- Extended `POST /api/v1/agent/go/build` with `use_server_config`: missing
  connection values are filled from the active transport and server secret; the
  build result now includes the server-local ZIP `path` for staging.
- New operator tools across MCP, web AI, and CLI: `deploy_agent_script`,
  `build_go_agent`, plus CLI `agent script|deploy-script|go-build`; parity
  script updated (52 tools).
- Rewrote the `agent-deployment` skill for the fully autonomous chain:
  render/build → `queue_upload` → lateral copy/execute → registration check,
  with `no_active_beacon_url`/profile prerequisites documented.
- Added `tests/test_agent_deploy.py` (13+4 tests) covering render validation,
  endpoint behavior, secret non-disclosure, and server-config fills.

### 2026-10-30 — Add the agent deployment skill

- Added `ai-skills/agent-deployment.md` (`default: false`) documenting how the
  AI deploys an RMM agent on another authorized machine by composing existing
  tools: `get_agent_script` for the template, operator-supplied or reused
  config, `queue_upload`/lateral copy for staging, a lateral execution method,
  and registration verification through `list_sessions`.
- Guardrails: operator intent and authorized scope required, the beacon secret
  and patched script stay out of chat output, and executed/registered/verified
  are reported as distinct states.
- Cross-referenced from the lateral-movement skill; Go agent builds remain a
  WebUI/REST operator action.

### 2026-10-29 — Add the lateral movement technique-selection skill

- Added `ai-skills/lateral-movement.md` (`default: false`) teaching the AI that
  several techniques can reach the same authorized target and that per-technique
  failures are normal: remote services, WMI, WinRM, scheduled tasks, RDP,
  DCOM, pass-the-hash/ticket from Exegol, and MSSQL.
- Documented failure classification (connectivity, authentication,
  authorization, policy, detection) with the guidance to diagnose, then move to
  the next authorized technique instead of stopping at the first error or
  silently broadening scope.
- Review added completion checks and stop conditions, corrected error-code
  namespaces, and required sanitized evidence and cleanup reporting. Outcomes
  distinguish verified success, partial completion, failure, and uncertainty.

### 2026-10-29 — Compact injected AI skills

- Consolidated repeated instructions in dispatch, identity/profile resolution,
  local reconnaissance, AD method selection, embeddings, and pivot reporting.
- Preserved skill IDs and default/always settings. Kept the skills self-contained
  because the current loader injects selected bodies without reference retrieval.
- Replaced the AD collection query cookbook with schema-aware analysis guidance
  and distinguished in-place analysis from artifact transfer.
- Removed raw-secret output examples and invalid/ambiguous AD identity examples;
  retained scoped collection, ownership, runtime limits, and evidence requirements.
- Checked frontmatter preservation, Markdown fences, and diff whitespace. No
  runtime code or operator surfaces changed.

### 2026-10-29 — Add scoped local secret hunting to the baseline skill

- Extended `ai-skills/local-recon-before-network.md` (default-enabled) with a
  secret-hunt stage after the local baseline: scheduled tasks, services,
  web/app configuration, unattend/sysprep files, GPP `cpassword`, autologon
  registry, command histories, and client tool configs, using built-in
  commands first.
- Deployment of compiled situational-awareness tools such as Seatbelt is
  documented as a last resort with operator intent, scoped command groups,
  and the approved RMM upload/plugin path.
- Guardrails: findings are reported as location/type/unlocked account without
  raw values, unrelated user files stay out of scope, and using a found secret
  for a new action requires the operator's intent.

### 2026-10-29 — Rename and extend the AD collection skill into analysis

- Renamed `ai-skills/python-bloodhound.md` to `ai-skills/ad-collection-analysis.md`
  (id `ad-collection-analysis`, still optional and off by default).
- The skill now explains that Python BloodHound is a good way to collect AD
  data, that the AI must download the collected output and analyze it on the
  operator side, and that BloodHound import is optional — direct JSON
  manipulation in Exegol (`jq`/Python) answers most questions.
- Added a curated catalog of common analysis goals with BloodHound Cypher
  queries and their direct-JSON equivalents: Domain Admins, shortest paths,
  Kerberoastable/AS-REP roastable accounts, delegation, DCSync, ACL abuse,
  GPO control, sessions, and local admin reach.
- Updated cross-references in `docs/web-ai-skills.md` and the
  `local-recon-before-network` skill.

### 2026-10-29 — Require a local baseline before network discovery

- Added the default-enabled `local-recon-before-network` AI skill for identity,
  token groups/privileges, OS, local networking, domain membership, and DC hints.
- Linked the pivot skill to this prerequisite; existing recent evidence can be
  reused, and unknown DC information is distinguished from live discovery.

### 2026-10-29 — Add an optional Python BloodHound preference skill

- Added `ai-skills/python-bloodhound.md` with `default: false` for AD graph
  collection from the operator environment.
- Documented precedence over the ADWS-first collection workflow when selected,
  while retaining current-token identity guidance and explicit task scope.

### 2026-10-29 — Add the RMM AI document embeddings skill

- Added `ai-skills/document-embeddings.md` in the server AI skill format,
  covering purpose, profiles, tool execution, workspace ownership limits,
  WebUI usage, copying, and exact-event results.
- Enabled by default for operators without a saved skill selection; existing
  selections can enable it through AI → Skills.

### 2026-10-29 — Expose embedding panel run failures beside the button

- Run validation, submission progress, API failures, and queue confirmation
  now appear beside the run button instead of only at the dialog header.
- Opening the panel prefills an empty session field from the selected session
  while preserving explicit input.
- All 18 embedding UI/shell tests pass. The reported live execution failure
  still needs the actual validation or API error to establish its cause.

### 2026-10-29 — Execute embeddings directly from the WebUI shell

- Added `embeddings run <profile> --document <path>` for the selected session,
  with help and completion support. It directly queues through the embeddings
  API without opening the panel.
- Invalid arguments stay local instead of reaching the Windows interpreter.
- Quoted Windows and UNC paths preserve backslashes; errors do not show a
  successful queued echo. Regression tests cover routing and failure paths.
- Actual plugin runs continue through the existing audited plugin queue.

### 2026-10-29 — Check Go agent Quick Tunnel connection configuration

- Verified authenticated registration through a local HTTPS reverse proxy;
  the beacon header is preserved and incorrect credentials produce HTTP 403.
- Added pre-build checks for a stale Quick Tunnel hostname and a supplied secret
  that differs from the server's active beacon secret. The current-URL button
  refreshes transport metadata rather than using cached state.
- Clarified embedded settings, launcher behavior, and target environment
  precedence. The reported deployment failure still requires its exact error
  to distinguish credentials from URL/origin connectivity.

### 2026-10-29 — Audit and repair shutdown paths

- Added unconditional central cleanup for signals, console exits, partial bind
  failures, accepted sockets, transport helpers, compiler children, and storage.
- Fixed workspace same-thread shutdown deadlocks, interrupted upstream requests,
  worker draining, polling waits, and SQLite cleanup order.
- Fixed WebSocket/SOCKS socket leaks, helper process reaping, late resource starts,
  and partial MCP context initialization cleanup.
- Added Go cancellation propagation and PowerShell process/runspace finalizers;
  the external SOCKS PowerShell plugin now cleans up interrupted relays.
- See [shutdown behavior](shutdown.md) for ownership, tests, graceful termination
  timing, and forced-kill/platform limitations.
- Validation: all 218 Python tests and `make check` pass. Go tests with the race
  detector, `go vet`, Windows/Linux cross-builds, and both executable PowerShell
  cleanup tests pass. Native Windows shutdown remains unverified.

### 2026-10-29 — Restore workspace registry input compatibility

- Preserve trimmed workspace IDs/tokens and the legacy `workspace_id` fallback
  when validating environment overrides and saved configuration.
- Report secret-safe JSON syntax or entry-validation errors at startup instead
  of an uncaught settings traceback. Malformed credentials remain rejected.
- Added regression coverage for both registry formats, normalization, and
  executable startup diagnostics without credential disclosure.

### 2026-10-29 — Validate persistent settings and named tunnel integration

- Completed startup configuration wiring across service consumers and client
  entry points, including flag precedence and active-versus-saved provenance.
- Restricted central settings and transport mutations to administrators and
  removed administrative credential fallback from workspace AI/MCP calls.
  Internal MCP children cannot load unrelated client configuration; saved
  Exegol credentials follow only their configured endpoint.
- Validation: all 195 Python tests, all 39 JavaScript controller tests, and
  `make check` pass. The external connector's 10 tests also pass. Integration
  coverage starts a real local server, checks saved named-profile autostart
  with a fake connector, rotates authentication, and verifies restart behavior.
- Live Cloudflare routing and browser visual QA have not been performed.

### 2026-10-29 — Persistent service configuration and named tunnels

- Added owner-scoped service configuration through API, UI, CLI, MCP, and an
  offline initialization command. Secrets are write-only in configuration read
  views; saved changes apply after service restart. Explicit environment and
  startup flags remain visible overrides for migration.
- Added existing remotely managed Cloudflare named-tunnel configuration alongside
  Quick Tunnels. Central transport remains separate from workspace ownership.
- Documented environment migration, bootstrap exceptions, configuration
  precedence, and persistent-volume initialization. Removed Compose operational
  defaults that would otherwise override saved settings on every restart.
- Validation results are recorded after implementation checks complete.

### 2026-10-29 — Clarify the embedding dialog introduction

- Replaced the developer-oriented introduction with operator-facing guidance
  on classification, reviewing results, and optional local copies. Retained
  processing location and supported-file limitations in the supporting text.

### 2026-10-29 — Share repository-owned embedding defaults across interfaces

- Moved model settings, financial label descriptions, and copy defaults into
  `examples/embedding-profile.json` as the canonical preset. The WebUI loads
  the validated configuration from `GET /api/v1/embeddings/defaults` and no
  longer contains a separate copy of the preset.
- Added `embeddings profiles defaults [--output FILE]` in the CLI and
  `embedding_profiles(action="defaults")` in MCP. Reading defaults does not
  modify workspace records; saving a profile creates the durable snapshot.
- Missing/invalid repository configuration produces an explicit API error;
  the editor stays disabled until defaults load successfully and can retry.
- Tests cover server/workspace access, CLI/MCP retrieval, saved-profile
  preservation, changed repository defaults, and browser error/retry behavior.
  All 159 Python tests, 34 JavaScript controller tests, and `make check` pass.

### 2026-10-29 — Default new embedding profiles to financial classification

- New WebUI profiles use the requested `strategic-financial` and `other`
  descriptions, enable local copying for `strategic-financial`, and require
  a similarity score of at least 0.4. The selected model remains public MiniLM.
- Existing saved/imported profiles retain their settings. New-profile and
  post-delete resets restore the same defaults, with visible copy guidance.
- Regression coverage verifies exact descriptions and settings through
  save/export and resets, alongside legacy and custom profile compatibility.

### 2026-10-29 — Simplify the embedding profile editor

- Added the tested MiniLM preset and collapsed advanced model controls;
  custom profile imports preserve their model, prefixes, and authentication.
- Replaced label JSON with editable name/description rows and copy-label JSON
  with checkboxes. Copy settings appear only when enabled, and selected-label
  mode cannot silently fall back to copying all documents.
- Added optional similarity filtering, visible path examples, contextual help,
  and a live run summary. Existing CLI/REST/MCP profile formats are unchanged.
- Controller regression tests cover old and custom profiles, preset switching,
  duplicate copy selections, label limits, filter toggles, and run validation.
  All 31 JavaScript controller tests and `make check` pass.
  Browser visual QA is unavailable because computer-use permissions are not
  enabled. No plugin rebuild is required for this editor-only change.

### 2026-10-29 — Align embedding editor and CLI with the updated plugin

- Fixed plugin tokenizer truncation inherited from serialized configuration,
  directory-depth counting, and failed-scan deduplication/document accounting.
  Regression tests cover full token retention, the eight/nine-level boundary,
  and explicit paths following a failed directory scan.
- The WebUI now preserves and edits all three copy settings, checks them for
  unsaved changes, and documents chunking, directory/UNC inputs, and local copy
  destinations. Result counts include error rows explicitly.
- CLI profile JSON and full result exports already preserved these fields;
  updated CLI/MCP help and added HTTP-backed CLI round-trip coverage for copy
  settings, directory/UNC dispatch, and result metadata.
- Validation: plugin Go tests and `go vet`, all 26 JavaScript controller tests
  (including six embedding tests), all 157 Python tests (including twelve
  embedding tests), and `make check` pass. Windows native inference and live agent
  execution have not been rerun; an updated DLL must be built and installed
  before using the changed plugin behavior.

### 2026-10-29 — Accept directory paths in embedding requests

- The autonomous document-embeddings DLL now enumerates listed directories
  itself: directory entries expand, in lexical order and without following
  links, to contained supported files up to eight levels deep, bounded by the
  32-document limit; expansion beyond it fails explicitly instead of
  truncating. Files found both explicitly and through a directory are
  embedded once, and unusable paths return per-path error rows.
- Long documents are chunked instead of truncated by the same DLL, and UNC
  SMB paths are covered by validation tests. See the
  [DLL guide](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/blob/feature/autonomous-document-embeddings/docs/document-embeddings.md).
- `result_payload` in `rmm_embeddings.py` now accepts up to 64 result rows
  with at most 32 embedded documents, so enumeration error rows save
  alongside embeddings; covered by
  `tests/test_rmm_embeddings.py`.
- New opt-in profile fields `copy_documents`, `copy_labels`, and
  `copy_threshold` make the autonomous DLL copy embedded documents matching
  the selected labels — and score threshold — into a fresh temporary
  directory on the target, organized by label, reported as `copy_directory`
  and per-row `copied_to` result fields.

### 2026-10-28 — Skip identical installed plugin releases in the UI

- Compare release filenames and SHA-256 digests with the actual runtime
  inventory; dim and disable matching entries and exclude them from Select all.
- Disable successful installations immediately while keeping failed downloads
  and failed workspace synchronization retryable. Failed inventory requests
  display a warning and leave installation available.
- Regression tests cover matching hashes, changed hashes, successful installs,
  unavailable inventory, and failed synchronization.

### 2026-10-28 — Display the connected deployment mode

- Show Standalone, Central in workspace mode, or Workspace with its ID in the
  header and explain storage ownership in Config using live health responses.
- Refresh on connection and health probes; reset to unknown on disconnect.
- Controller coverage verifies each mode and the unknown fallback.

### 2026-10-28 — Make source list loading explicit

- Refresh sources whenever Config opens, independently of session/history
  initialization. Display loading, empty-store, and request errors beside the
  source selector, and reject malformed lists instead of showing a blank list.
- Preserve saved rows on refresh failures and never silently reseed deleted
  sources. Added regression coverage for Config opening and empty/error states.
- The specific failure on the deployed instance remains unconfirmed pending
  its source-load error message.

### 2026-10-28 — Repair source save feedback and separate source editing

- Move New/Edit source into a dedicated dialog with Enter-to-save, visible
  errors, cancel/Escape, immutable existing IDs, and duplicate-ID protection.
- Display the confirmed saved source immediately and retain existing rows
  after refresh failures; show workspace-routing error details.
- Prefill the standard GitHub repository for new sources. The existing backend
  default repository and one-time seeding behavior are unchanged.

### 2026-10-28 — Explain UI fields and collapse Config sections

- Add hover and assistive-technology descriptions to Config and common login,
  agent, pivot, AI, and embedding controls. Source ID and Label also have
  visible definitions with examples.
- Collapse every Config disclosure, each time
  the dialog opens. Form values and saved settings are preserved.

### 2026-10-28 — Simplify Config and plugin selection

- Follow-up: expand Config to a maximum of 76rem with viewport gutters;
  allow source and repository controls to grow into the available space.
- Split Config into plugins, installed assets/manual uploads, beacon transport,
  and import/export sections; widen the dialog and constrain scrolling.
- Replace the plugin dropdown with labeled checkboxes and sequential batch
  installation, preserving release/checksum pins and failed items for retry.
- Show credential presence outside the source editor and clarify that the
  credential field takes a service environment variable name, not an OAuth token.
- Ten plugin controller tests pass, including batch partial failure and retry.
  `make check` also passes. Live OAuth access has not been verified, and browser
  visual QA was unavailable because no browser connector is present.

### 2026-10-28 — Consolidate deployment guidance and fix login prerequisites

- Added one [deployment guide](deployment.md) for Ubuntu central + Exegol
  workspaces, with clearly separated Compose and standalone alternatives.
  Native central/workspace environment templates preserve JSON quoting and
  distinguish administrator, beacon, workspace, and local operator credentials.
- Removed stale shared-plugin-copy and wrapper guidance; documented role-local
  stores, source/version setup, manual claims, namespace access, optional Go
  generation/providers, persistence, and update procedures.
- Fixed workspace login bootstrapping: static GET UI assets load before local
  token authentication, while APIs and other methods remain protected. Added
  real HTTP regression coverage for auth and traversal boundaries.
- Added `iproute2` and CA certificates to the runtime image for Linux TUN and
  HTTPS downloads; image startup still performs no compilation. Corrected
  the beacon-bind help text to match the actual loopback default.
- Validation: `make check`, all 153 Python unit tests, Compose configuration
  parsing, native environment shell/JSON checks, and documentation link checks
  pass. No Docker image build, container deployment, or CI workflow was run.

### 2026-10-28 — Retire the startup build wrapper

- Removed `run_server.py`; `server_rmm.py` is the direct runtime entry point.
  Updated quick-start, profile, plugin, and workspace instructions accordingly.
- Native TUN helper preparation remains an explicit `make tun-build` step or
  part of the Docker image build. Existing TUN readiness checks report a
  missing executable when the operator requests TUN, without a startup build.
- Added migration instructions for service commands, working directories,
  and the removed rebuild flag/environment switch in [server startup](server-startup.md).
- Validation: `make check`, direct server `--help`, and server/workspace
  missing-helper checks pass. No compiler, helper process, or CI build was
  started. Remaining wrapper references are historical notes and migration guidance.

### 2026-10-28 — Saved plugin sources and branch versions

- Added UI, CLI, REST, and MCP source management for multiple repositories and
  branches. Settings persist in the active workspace/server asset store;
  dedicated credential references never include secret values.
- Release browsing selects the configured branch's highest numeric version.
  Installation pins the source revision, exact tag, and digest in the UI;
  editing settings invalidates stale selections. Legacy main SHA releases
  remain supported.
- Plugin publication now uses independent major/minor versions per branch:
  additions/removals/renames increment major, modifications increment minor,
  and unchanged inputs/metadata skip publication. Non-main publication remains
  opt-in through manual dispatch, with verified binary reuse across branches.
- See [source configuration](plugin-releases-operator.md) and
  [version/storage rules](plugin-releases.md).
- Validation: 151 Python tests, eleven Node controller tests, 49-tool parity,
  and repository lint pass. The plugin publisher has eighteen passing tests
  covering version increments, branch isolation, reuse, and publication guards.
- [Main version 1.0](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/releases/tag/plugins-0d6e4079e367-v1.0)
  published successfully in run 40666514314 with both compiler jobs skipped.
  Live saved-source selection, private download, cached retry, and stale-source
  rejection passed. Read-only replanning reports `publish: false` for unchanged
  plugin inputs and metadata.

### 2026-10-28 — Install compiled plugins from private releases

- Added shared GitHub release browsing and checksum-verified installation to
  REST, CLI, WebUI, and MCP. Installs use exact commit tags and optionally pin
  the selected digest. GitHub credentials remain in the storage owner's process.
- Workspace installations persist locally and republish to the workspace's
  central runtime cache. Server mode uses its existing plugin store. Neither
  publication nor installation automatically executes a plugin on an agent.
- Replaced the plugin repository's temporary manual-only publication policy
  with main-branch versioned releases and reuse of unchanged compiled inputs.
  Minimal-RMM's own `main` stays v1; implementation remains on `v2`.
- Added configuration, rollback, integrity, size-limit, and implementation
  documentation in [plugin releases](plugin-releases.md).
- Validation: 137 Python tests and seven Node controller tests pass; final
  focused release tests, lint, and 46-tool MCP parity pass. Tests cover exact
  release selection, credential stripping, checksum failure, cache reuse,
  owner isolation, runtime publication, and partial synchronization failure.
- Hosted [release publication](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/actions/runs/40663841398)
  passed and skipped both compiler jobs. The first durable private release
  contains eight plugin files. A live authenticated download through the new
  installer verified the autonomous DLL digest and cached retry. Local Python
  required `SSL_CERT_FILE=/etc/ssl/cert.pem`; TLS verification remained enabled.

### 2026-10-28 — Equal agent and AI panel widths

- Split the desktop workspace equally between the agent and AI panels when
  AI is open, replacing the fixed 300–360 pixel AI column.
- Preserved the session sidebar, panel divider, and stacked mobile layout.
- Validation: reviewed CSS grid sizing and responsive overrides;
  `git diff --check` passes. Browser visual verification remains pending.

### 2026-10-28 — Stop redundant plugin CI builds

- Removed automatic branch/PR triggers from the MSVC loader fixture. It now
  requires explicit dispatch or a `ci-msvc-*` validation tag; `main` stays v1.
- Plugin-repository tests and builds are manual-only, with explicit switches
  for compilation, bounded concurrency, and retained compiled artifacts.
- Canceled two redundant merge-triggered plugin runs and disabled the three
  obsolete native probe workflows on GitHub.
- Existing compiled bundle and validated ONNX DLL are cataloged with source
  revisions, checksums, expiry, and a verified local download/cache helper.
  Deployment can reuse these binaries without compiler or Actions usage.

### 2026-10-28 — Saved document embedding operator workflow

- Added dedicated profile and result APIs, CLI commands, a WebUI editor, and
  three MCP tools. Profiles persist independently of browser configuration;
  import/export uses JSON. Runtime model/auth/prefix settings remain bounded
  by the validated autonomous DLL's input contract.
- Workspace mode stores records locally in `workspace.db`; server mode uses
  `embeddings.db`. Central workspace requests are refused, including scoped
  workspace credentials against server-owned records.
- Results are saved by explicit session/event selection and can be exported
  or deleted. No automatic job correlation is claimed. Saved collections
  remain independent of transcript retention.
- Documented the required 64 MiB upload/sync limits for the 36.2 MB DLL and
  the distinction between saved `env` authentication preference and an
  already-provisioned agent `HF_TOKEN`.
- Validation: eight targeted HTTP/CLI/storage/server tests and three Node
  UI-controller tests pass. The full Python suite and `make check` pass,
  including 44-tool MCP parity and WebUI contracts. Visual browser
  verification is unavailable because computer-use permission is not granted
  in this environment; controller tests use a minimal DOM adapter.

### 2026-10-27 — Standard Windows loader for autonomous ONNX DLLs

- Added opt-in `RMMRequiresWindowsLoader` export inspection before execution.
  Marked DLLs use `LoadLibraryExW` in a separate agent child, with Windows
  handling CRT, exceptions, static TLS, and new native threads.
- The parent stages the DLL in a random directory with a protected Windows
  DACL, holds a read-sharing-only file handle until child exit, and cleans up
  after success, failure, or cancellation. Input remains on stdin. A forced
  parent termination can leave the temporary file behind.
- Removed the manual TLS and exception-table experiments from the production
  mapper. Unmarked DLLs retain the existing loader path. CLI/MCP descriptions
  now disclose temporary-file loading instead of promising all loads are
  diskless; the command schema remains unchanged.
- Added metadata bounds tests and Windows tests for native thread TLS,
  exceptions, cancellation, error cleanup, and a legacy unmarked DLL. The
  real-model test checks normalized MiniLM embeddings and classification
  through the authenticated parent/child path. Windows lifecycle and legacy
  fixture tests passed in run 40552719824. Local Go tests, Windows amd64/arm64
  and Linux cross-builds, 700k marker fuzz inputs, Python source-manifest tests,
  and `make check` passed. The full ONNX DLL build and real-agent acceptance
  passed in [plugin run 40553323301](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/actions/runs/40553323301)
  with plugin revision `fb963ab` and agent revision `6f18910`.
- The real-model acceptance test now selects both MiniLM (mean pooling) and
  a pinned quantized BGE-small graph (CLS pooling) with the same DLL. It checks
  two document vectors, a strategic label, and a missing-document error for
  each model, then validates invalid-input handling and staging cleanup.
- Both real-model cases passed on Windows: MiniLM in 1.97 seconds and BGE in
  2.38 seconds. Import inspection confirmed only Windows system DLLs. The
  [validated DLL artifact](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/actions/runs/40553323301/artifacts/12763491713)
  is 36,232,192 bytes, with SHA-256
  `a08000ab452a475f708293743f91231ca5f31317f4690b98cb41464841aae2e6`.
  Validation used the actual Go agent process in Windows CI; deployment to a
  managed host is separate. The DLL needs the updated agent and temporary
  on-disk staging, but no separate ONNX Runtime, VC runtime, or Python install.

### 2026-10-27 — Prototype an MSVC plugin loader test

- Added a Windows CI fixture built with MSVC `/MT` and a child-process test of
  the agent's in-memory PE mapper, including a C++ constructor and exception.
- Registered x64 unwind metadata with `RtlAddFunctionTable` before the mapped
  DLL's entry point runs. Corrected the documented mapper limitations.
- Local cross-compilation passed; the statically linked ONNX Runtime probe
  remains to be validated by its separate Windows CI job.
- Windows CI confirmed the `/MT` fixture has no VC runtime DLL import and its
  global constructor runs. `RtlLookupFunctionEntry` finds mapped unwind data,
  but an ordinary caught C++ throw terminated the child with `0xe06d7363`.
  The manual image is not registered for MSVC CRT module-base lookup. A
  fixture-local C bridge that supplies the image base made the same exception
  test pass in Windows CI. ONNX Runtime linking and execution remain open.
- A separate plugin-repository Windows probe linked a Go C archive into an
  MSVC `/MT` DLL. Explicitly starting the Go runtime from its exported entry
  point passed both normal `LoadLibrary` and this agent's in-memory mapper.
  This preserves a possible route to reuse the existing Go document pipeline
  in a single native DLL. The plugin repository now has an ONNX C++ bridge,
  Go autonomous build tag, and Windows combined-DLL workflow. The first
  static ONNX Runtime link succeeded in CI; mapped inference and the combined
  document workflow still require Windows CI validation. A subsequent
  Windows job successfully linked the Go archive and static ONNX Runtime
  into `document-embeddings-autonomous.dll`. Its artifact upload failed while
  traversing an unrelated protected `_deps` directory; the plugin workflow
  now uses the exact DLL path and is rebuilding for import and mapper tests.
- Extended the mapped-plugin fixture test to accept a JSON result substring
  and a three-minute timeout for the public-model end-to-end check.
- The combined DLL passed Windows import inspection with only system DLLs and
  executed under the normal Windows loader. Its first run through the manual
  mapper crashed in an MSVC static TLS access. Added an experimental
  current-thread PE TLS template and callback setup to the mapper; the
  diagnostic workflow is validating the already-built DLL without rebuilding
  ONNX Runtime.
- The validation-only mapped request now reaches the embedded Go pipeline.
  A real model request exposed a second null static-TLS slot after Go entered
  its download path. The plugin branch now restores the mapped TLS block at
  each native ONNX boundary and locks Go execution to one OS thread. Its
  MSVC helper compiled in a short Windows job; the full DLL rebuild and
  model-inference retest are pending. A separate short Windows fixture
  deliberately replaced the thread TLS vector; the helper restored a
  thread-local value through the actual agent mapper.
- The rebuilt single DLL passed Windows dependency inspection: it imports only
  Windows system DLLs and needs no separate ONNX Runtime or VC runtime DLL.
  A pinned public MiniLM model produced an embedding and strategic label under
  the normal Windows loader (plugin CI run 40548809398). The same request
  crashes under the in-memory mapper at a native static-TLS access on an ONNX
  Runtime worker thread (RVA `0xF478D7`, TLS index 4096, null block). The
  current-thread TLS repair cannot cover threads created later. The plugin is
  therefore a prototype, not a working Go-agent feature. The next step is a
  loader design that initializes static TLS for every native thread, or a
  standard Windows loader path for this DLL, followed by the real-model test.

### 2026-10-15 — Add Exegol MCP and RMM pivot routing skill

- Added the default `exegol-rmm-pivot` AI skill to distinguish RMM agent
  control from Exegol command execution and to select SOCKS5 or TUN when an
  Exegol tool needs access to an agent-reachable network.
- Documented live Exegol tool discovery, namespace ownership, agent plugin
  prerequisites, current TCP/UDP-only TUN behavior, and temporary pivot
  cleanup.
- Validation: AI skill loading test and `git diff --check` pass.

### 2026-10-15 — Align Go agent command dispatch

- Matched the Go Windows agent with the PowerShell agent for CMD dispatch:
  exact `ls` maps to `dir`, `cmd:` is explicit, and `PS:` / `powershell:` /
  `pwsh:` accept optional whitespace.
- Added the Windows `pwsh` fallback to `powershell.exe`, WebUI completion
  spacing, command-dispatch regression tests, and updated operator/AI guidance.
- Validation: Go agent tests, WebUI contract checks, Python compilation, and
  `git diff --check` pass.

### 2026-10-14 — Align TUN TCP timeout with Ligolo-ng

- Set the external `tun.dll` target-side TCP CONNECT timeout to five seconds,
  matching Ligolo-ng's agent behavior. A timeout produces no success response,
  so the local socket scanner continues to see the port as filtered.
- Updated the plugin and TUN pivot documentation and added a regression check
  for the timeout value.
- Validation: plugin host tests, TUN race tests, TUN vet, and `git diff --check`
  pass.

### 2026-10-13 — Complete agent and pivot shutdown

- Routed the embedded CLI `kill` command through `RMMServer.kill_session`, so
  it now stops active SOCKS/TUN relays, clears persisted session state, and
  finalizes session history just like the REST path.
- Made Go plugin child shutdown wait for the child runner after cancellation,
  and made persistent plugin downloads context-cancellable.
- Added explicit cleanup for the server's public SOCKS and transport managers
  during process shutdown.
- Added regression coverage for the CLI termination path.

### 2026-10-13 — Unified workspace tabs

- Moved Shell, Files & Screenshots, and Pivots into one main agent workspace
  tab bar. Files and pivot operations remain scoped to the selected live agent.
- Kept Downloads and Screenshots as global application-header controls backed by
  the shared artifact dialog, so each view continues to cover every agent.
- Kept the agent workspace focused on session-scoped operations while the global
  artifact dialog remains available without changing the selected session.
- Updated the WebUI contract checks and operator documentation.

### 2026-10-13 — TUN TCP scan false-positive fix

- Replaced the stock tun2socks TCP forwarder in `server-tun/rmm-tun` with a
  gVisor forwarder that performs the agent-side SOCKS `CONNECT` before sending
  the local SYN-ACK. Failed target connections now produce a local TCP reset
  instead of a successful handshake for every port.
- Kept the Go agent privilege-free and preserved the separate TCP and UDP TUN
  transport. The helper still does not forward target raw packets or ICMP.
- Added focused tests for TUN device parsing and endpoint metadata conversion,
  and documented relay latency and the remaining raw-packet limitation.

### 2026-10-13 — TUN relay close race and helper diagnostics

- Kept a successful private SOCKS `CONNECT` pending until the helper receives
  its success reply when the agent reports `ok` and `closed` in one poll. This
  preserves open-port results for targets that close immediately after
  accepting a TCP connection.
- Forwarded server-helper stderr to the server operator log so failed agent
  `CONNECT` attempts are visible instead of being discarded.
- Added regression coverage for the fast-close relay state and the gVisor TCP
  SYN-ACK/reset decision.

### 2026-10-13 — TUN flow diagnostics and return-path coverage

- Changed the server helper's default log level to `info`, with the
  `RMM_TUN_LOG_LEVEL` environment variable available for `debug` or `warn`.
- Added explicit server-side TCP connect-attempt and target-connect messages so
  a filtered scan can be separated from a missing agent response.
- Added an in-memory end-to-end test proving target TCP response bytes return
  through the gVisor stack to the TUN link after the local handshake.
- Documented that TCP connect scans (`nmap -sT`) are the reliable validation for
  the current socket-based transport; raw SYN scans are timing-sensitive.

### 2026-10-13 — Independent pivots and global artifact views

- Removed the SOCKS/TUN lifecycle coupling in the central server and workspace
  pivot manager. Each relay keeps its own transport manager and the Go agent
  keeps one controller per plugin, so both channels can run for one session.
- Replaced the separate Files and Pivots dialogs with a main workspace tab bar
  containing Files & Screenshots and Pivots views. Downloads and Screenshots
  remain global application-header controls.
- Added `GET /api/v1/artifacts?kind=downloads|screenshots`, including agent
  identity, session status, artifact metadata, and authenticated artifact URLs
  for server and workspace storage modes.
- Updated the WebUI contract checks, focused regression tests, README, wiki
  pages, and downloads-browser roadmap.

### 2026-10-13 — Persistent WebUI configuration and local asset uploads

- Fixed the Config dialog surface color so it remains opaque in both themes.
- Added authenticated local file uploads for plugins, AI skills, and rclone
  `.json` profile files through the existing asset API. The WebUI switches to
  a direct `profiles.json` upload control for rclone.
- Added `rmm_ui_state.py` and the `/api/v1/ui/config` GET/PATCH contract. Server
  mode stores the allowlisted UI state in `RMM_UI_STATE_DB`; workspace mode
  stores it in the existing `workspace.db` SQLite database.
- Migrated operator preferences, skill selection, agent generator values, AI
  provider state, shell history, event cursors, and theme synchronization to
  the authenticated SQLite state while keeping the API token as a browser
  login credential.
- Made the AI Skills section open by default and marked default/always skills
  in the visible catalog. Documentation and WebUI contract checks were updated.

### 2026-10-13 — Dynamic RMM-Profiles and operator asset management

- Split the administration listener from the agent beacon listener. The
  default ports are `8080` for WebUI/API and `8081` for agent and pivot paths;
  each listener rejects the other role's routes.
- Added the generic `minimal-rmm-profile-v1` process contract in
  `rmm_profile.py`. The server starts a trusted profile bundle, waits for a
  newline-delimited `ready` event, publishes its `public_url`, and stops the
  process cleanly. Cloudflared moved to the private
  `CyrilleFranchet/Minimal-RMM-profiles` repository.
- Added automatic Cloudflared startup and `trycloudflare.com` URL discovery
  through `/api/v1/profile` and `/api/v1/transport`, with CLI, MCP, and WebUI
  controls. The agent generators now use the beacon URL.
- Added `rmm_asset_store.py` and the `/api/v1/assets/*` REST surface for URL
  installation, enablement, and removal of `.dll`/`.ps1` plugins, `.md` AI
  skills, and `.json` rclone profiles. The same feature is available through
  the WebUI, CLI, and MCP.
- Workspace asset bytes persist under the operator's Exegol data directory.
  Enabled plugins synchronize to the central runtime cache and are republished
  after a workspace restart. The workspace plugin list still includes seeded
  central artifacts.
- Added `docs/rmm-profiles.md` and `docs/operator-assets.md`, and linked both
  from the documentation wiki, architecture map, startup guide, workspace
  guide, and root README.
- Validation: 102 Python tests, `make check`, `docker compose config`, and
  `git diff --check` pass.

### 2026-10-13 — Documentation wiki and architecture map

- Added `docs/README.md` as the repository wiki index with links grouped by
  deployment, architecture, pivots, agent capabilities, WebUI, AI, and
  planning.
- Added `docs/architecture.md` as the canonical current architecture page with
  Mermaid diagrams for the control plane, Exegol workspaces, plugin loading,
  pivot traffic, storage ownership, and privilege boundaries.
- Marked `progress.md` as historical status and `tech-plan.md` as roadmap
  content so they are not mistaken for current runtime documentation.

### 2026-10-13 — External plugin repository boundary

- Confirmed the intended split: Minimal-RMM remains the control-plane and
  generic agent-host repository; agent plugin sources and release builds move
  to a separate plugin repository.
- Created the private
  `CyrilleFranchet/Minimal-RMM-plugins` repository for plugin source,
  manifests, bundle builds, tests, and CI.
- Removed agent plugin sources, server-side plugin compilation, and the Go
  plugin build panel from Minimal-RMM. The server now inventories and serves
  only artifacts installed in `RMM_PLUGINS_DIR`.
- Kept `server-tun/rmm-tun` in Minimal-RMM because it creates the operator-side
  TUN device and is not delivered to managed hosts.

### 2026-10-13 — Persistent dynamic plugin store

- Added a persistent Docker plugin volume at `/app/plugins` for dynamically
  deployed Go and PowerShell plugin artifacts.
- Added an image seed directory and entrypoint that installs built-in artifacts
  only when the volume does not already contain them, preserving operator
  versions across image updates.
- Documented that plugin commands remain in the plugin artifact and are
  invoked through the agent's generic isolated PE export loader; new command
  logic does not require changes to the server or the main Go agent.
- Added the optional `command` field to the PE operator request. It queues a
  generic `__PLUGIN_COMMAND__` envelope whose `Run` dispatch remains entirely
  inside the dynamically delivered plugin.

### 2026-10-13 — Multi-operator workspace deployment

- Added `RMM_STORAGE_MODE=workspace` with central workspace authentication,
  exclusive session claims, scoped REST access, and bounded transient event
  delivery. The legacy server-owned storage mode remains the default.
- Added `workspace_service.py` and `workspace_pivot.py`. Each operator's
  workspace service persists sessions, transcripts, AI chat, downloads,
  screenshots, and keylogs in a local SQLite/filesystem store. It owns the
  local SOCKS listener and TUN helper, so `/dev/net/tun` and `CAP_NET_ADMIN`
  stay out of the central control-plane container.
- Added central transport-only SOCKS/TUN bridges with task requeue on failed
  workspace handoff, local listener restoration, and stale relay cleanup.
- Forwarded workspace headers through direct and stdio MCP AI tool clients so
  workspace-scoped AI actions cannot escape the claimed session set.
- Added `Dockerfile`, `.dockerignore`, `.env.example`, and
  `docker-compose.yml` for a control-plane service plus an optional privileged
  workspace sidecar. The image builds only the server-side `rmm-tun` helper;
  agent plugin artifacts are mounted from the runtime store.
- Added workspace unit coverage for registry authentication, event cursor
  overflow, local chunk reassembly, artifact/chat persistence, session claims,
  central payload routing, and transport-only bridges.
- Moved workspace-mode rclone profiles, default profile selection, and size
  limits into each operator workspace. The central broker now accepts only the
  workspace-built transient job and exposes plugin/bootstrap capabilities
  without returning workspace profiles.
- Mounted `tools/rclone/` into the Compose workspace service only, added local
  rclone status and exfil routing, and carried `max_bytes` into the Go plugin
  path with source-size enforcement.
- Documented the architecture and operational procedures in
  `docs/workspace-deployment.md`, `docs/server-startup.md`, `docs/tun-pivot.md`,
  `docs/pivot-plugins.md`, `docs/session-history.md`,
  `docs/web-ai-chat-memory.md`, `../README.md`, and this plan.

### 2026-10-12 — Multiple TUN routes and host diagnostics

- Added canonical `routes[]` support across the REST API, CLI, MCP tools, and
  WebUI. The existing single `route` field remains accepted for compatibility.
- The server installs and removes all configured routes with the relay
  lifecycle; the WebUI accepts one IPv4 CIDR per line and the CLI repeats
  `--route`.
- Added actionable Linux TUN errors for missing `/dev/net/tun` and missing
  `CAP_NET_ADMIN`, plus validation for explicit `utun<N>` names on macOS.

### 2026-10-12 — WebUI TUN routing controls

- Added TUN routing controls inside the SOCKS **Pivots** dialog in the live
  session header. Operators can configure the server device,
  target route, TUN address, egress interface, MTU, and UDP timeout.
- Added active TUN inventory, helper and beacon status, start/stop actions, and
  periodic refresh through the existing `/tun` operator endpoints.
- Added a dedicated privilege-free `tun.dll` plugin for TUN routing. It runs
  in its own child process, polls `/tun`, and stays separate from the public
  `socks.dll` plugin and `/socks` channel.
- Moved the server-side `rmm-tun` build output to `tools/rmm-tun`, keeping
  `RMM_logs/` for runtime data and logs.
- Added `run_server.py` to build the server-side TUN helper and report runtime
  plugin artifacts before starting `server_rmm.py`.
- Added WebUI contract coverage and a regression test for private bridge
  filtering.

### 2026-10-12 — Server-owned TUN pivot

- Replaced the external proxy-based TUN prototype with an RMM-owned TUN
  frontend. The server creates the device and starts the checked-in
  `server-tun/rmm-tun` helper; no external proxy console is required.
- Added a dedicated TUN transport with SOCKS5 UDP association support. The
  Windows amd64 Go agent's `tun.dll` plugin opens target-side UDP sockets
  alongside its TCP connections while remaining privilege-free.
- Added `GET /tun` and `POST /sessions/{id}/tun`, matching CLI and MCP tools,
  Linux/macOS device and route lifecycle, helper readiness checks, and cleanup
  on stop, kill, and server shutdown.
- Added `docs/tun-pivot.md` and focused TUN/UDP validation. The first slice
  carries TCP and UDP socket flows; raw
  packet and ICMP semantics remain a separate capability.

### 2026-10-12 — PowerShell screenshot plugin

- Moved the PowerShell screenshot capture implementation into the private
  external plugin repository.
- The agent fetches the deployed artifact through the authenticated plugin endpoint,
  evaluates it once, and preserves the existing screenshot result and operator
  API behavior.
- Added server delivery coverage and documented the runtime contract in
  `docs/go-agent-plugins.md`.

### 2026-10-12 — Make SOCKS protocol explicit in the WebUI

- Added a visible SOCKS5 protocol badge and client configuration guidance to
  the Pivots dialog.
- Documented that the shipped relay accepts unauthenticated TCP `CONNECT`
  through SOCKS5 and does not support SOCKS4 or HTTP proxy mode. This avoids
  confusing a proxy client configuration failure with a relay failure.

### 2026-10-12 — SOCKS relay reliability audit

- Fixed fragmented SOCKS5 handshake and request reads, rejected clients that do
  not offer the supported no-auth method, and buffered fast remote data until
  the local client socket is attached.
- Fixed server task queue behavior: close operations retry until the agent
  acknowledges them, the 32-send poll cap is enforced without replaying
  deferred data, and failed listener binds no longer leave a registered
  running relay.
- Added bounded PowerShell target connects, remote EOF detection, bounded read
  batches, transient `GET /socks` retry behavior, and response batch retries.
  The Go plugin now reuses sockets across repeated connect tasks, retries
  response batches, and completes short TCP writes.
- Added regression tests for fragmentation, task ordering/caps, early remote
  data, listener rollback, and Go short writes. `rmm_rclone.py` remains a
  separate cloud upload module.

### 2026-10-12 — Pivot plugin boundary

- Renamed the WebUI session control to **Pivots** while keeping SOCKS5 as the
  first technical pivot type and preserving the `/socks` API for compatibility.
- Moved the Go agent SOCKS relay into the private external plugin repository.
  Windows amd64 Go agents start it from the `socks_active` lifecycle flag and
  can cancel it without blocking the beacon loop.
- Moved the PowerShell relay implementation into the private external plugin
  repository, fetched through the authenticated plugin endpoint and run in its
  dedicated worker runspace.
- Added plugin readiness checks so a Go pivot cannot start until the required
  `socks.dll` artifact is installed. See `docs/web-socks.md` and
  `docs/go-agent-plugins.md`.

### 2026-10-12 — WebUI pivot controls

- Added a **SOCKS5 relays** dialog to the live session header. Operators can
  start or restart the selected session relay, stop it, choose the listener
  port and bind host, and copy the proxy URL.
- Added a global relay inventory backed by `GET /api/v1/socks`, with agent
  identity, proxy URL, channel, tunnel count, beacon state, and per-relay stop
  actions. Active listener ports also appear in the session sidebar.
- Documented the flow in `docs/web-socks.md` and added a WebUI contract check.

### 2026-10-12 — Plugin exfil password normalization

- Fixed the in-process rclone plugin path to obscure plain backend password
  options before rclone constructs the remote, while preserving values that are
  already rclone-obscured. Plugin jobs now share the profile key mapping used by
  binary jobs. Added regression coverage for both password forms.

### 2026-10-12 — WebUI shell help

- Added local `help` and `?` commands to the WebUI shell. They display the
  browser-handled command reference and no longer queue `help` for `cmd.exe`.
- Added the commands to WebUI Tab completion and documented their behavior in
  `README.md` and `docs/web-shell-completion.md`.

### 2026-10-12 — WebUI file and screenshot overlays

- Replaced the inline **Files & screenshot** and **Downloads from agent**
  sections with dialog overlays opened from the selected session header.
- Sized both overlays against the viewport so the file form and download table
  remain usable even when their contents are short.
- Screenshot previews now expose a translucent **Download** control on hover or
  keyboard focus. It uses the authenticated artifact download path and saves
  the screenshot locally.

### 2026-10-09 — WebUI command completion audit

- Confirmed shell history navigation and Tab/Shift+Tab completion are wired to the selected session, persisted in `sessionStorage`, and rebuilt from session events after reload.
- Documented the existing `pe` operator meta command in `docs/web-shell-completion.md`; the WebUI already includes it in completion candidates and routes it to `POST /api/v1/sessions/{id}/pe`.
- Fixed Tab cycling so repeated Tab and Shift+Tab preserve the original candidate set instead of filtering to the first completed value; history candidates now retain most-recent-first ordering.
- Added `scripts/check_web_contracts.py`, included in `make check`, to enforce completion/documentation alignment and WebUI configuration export/import wiring.
- Moved the PowerShell agent source to `agent-powershell/client_rmm.ps1` to match the Go agent layout; the server API and generated download filename remain `client_rmm.ps1` for compatibility.

### 2026-10-09 — Server-side plugin builds and organized deploy UI

- Moved plugin compilation and release packaging to the private external
  plugin repository. Minimal-RMM only inventories deployed artifacts and
  serves them through the authenticated plugin endpoint.
- The operator surfaces retain plugin inventory and loading; plugin-specific
  build tooling and source browsing are no longer part of this repository.
- Exfil mode selection now supports `auto`, `plugin`, and `binary`. Windows amd64 Go agents advertise `pe` and `rclone-plugin` capabilities; `auto` prefers an available `rclone-exfil*.dll`, while other Go targets, PowerShell agents, and explicit `binary` mode retain the executable fallback.
- Tests: plugin inventory and artifact delivery coverage. Docs updated for the
  external repository and runtime artifact contract.

### 2026-10-09 — In-process rclone exfil plugin

- Moved the rclone exfil plugin source and build metadata into the private
  external plugin repository. Minimal-RMM retains the operator API and the
  generic artifact delivery contract.

### 2026-10-09 — Isolated Go plugin execution

- Replaced in-process manual PE mapping with a Windows child-process runner.
- The authenticated parent fetches a plugin DLL, starts
  `agent.exe --plugin-child`, passes the DLL bytes and export input over stdin,
  and captures stdout. No plugin file is created.
- Plugin crashes and large plugin dependencies are isolated from the beacon
  process; the child has a ten-minute timeout.
- Added `make go-test` for Windows amd64 and Linux amd64 cross-build checks.

### 2026-10-09 — Screenshot plugin

- Moved Go Windows screenshot capture into the private external plugin
  repository.
- `__SCREENSHOT__` now loads `screenshot.dll` through the authenticated,
  isolated child-process plugin path and preserves the existing PNG result
  handling and operator surfaces.
- The plugin uses native GDI capture and returns the existing base64 PNG
  payload; no screenshot DLL is written to the target disk.

### 2026-10-12 — Screenshot plugin output limit

- Increased the child plugin output buffer to 16 MiB so high-resolution PNG
  screenshots do not become truncated base64 payloads.
- The screenshot plugin now returns an explicit overflow error instead of
  writing truncated output that the server cannot decode.

### 2026-10-12 — Server-side plugin lifecycle logging

- Removed plugin lifecycle logging from the Go agent's target-side log.
- The RMM server now logs plugin queueing, authenticated delivery, dispatch, and
  completion or failure with safe metadata, including output size and duration.
- Added an explicit repository rule that plugin lifecycle and operator audit
  logs belong on the server.

### 2026-10-09 — Go agent diskless PE plugin system

- Added the `__PE_LOAD__` command to the Go agent: plugins are fetched from the beacon-authenticated `/tools/plugins/<name>` endpoint on demand and manually mapped into the child process with private allocations. Nothing is written to the target disk; code pages are never RWX (see `docs/go-agent-plugins.md`).
- `agent-go/pe_loader_windows.go` implements parsing (`debug/pe`), writable staging and protected image allocations, base relocations, IAT resolution, per-section protections, `DllMain` attach, export resolution, and child-process cleanup. Windows amd64 only; other builds return explicit unsupported errors.
- Server: `GET /api/v1/plugins` (name/size/SHA-256 listing), `POST /api/v1/sessions/{id}/pe` (queues `__PE_LOAD__` with plugin/export/input validation), and plugin serving from `RMM_logs/plugins/` (`RMM_PLUGINS_DIR` override) with beacon auth plus a registered-session check.
- Full operator parity: `rmm_cli.py pe list` / `pe load`, interactive `pe` verb, MCP tools `list_plugins` and `queue_pe_load`; parity script and README/`docs/mcp-parity.md` tables updated.
- Tests: `tests/test_server_rmm_plugins.py` covers the API routes, plugin listing, and serving; `tests/test_go_agent.py` manifest now includes the new source file.

### 2026-10-08 — Go agent build workflow

- Added the dependency-free `agent-go/` protocol-compatible beacon with command execution, runtime config acknowledgement, chunked file transfer, HTTP proxy support, plugin-managed SOCKS pivot startup, rclone bootstrap/exfiltration, and Windows-specific screenshot/keylog/persistence adapters.
- Added authenticated WebUI source inspection and server-side cross-compilation for Windows/Linux amd64/arm64.
- Added `docs/go-agent.md`; Windows amd64 Go agents use the persistent `socks` plugin, while the PowerShell agent fetches its script plugin on demand.
- Go Windows child commands now use hidden process creation, including PowerShell screenshot capture and explicit PowerShell commands.
- Go Windows screenshot capture now uses native GDI APIs and Go PNG encoding without launching PowerShell.
- Replaced fragile `GetDIBits` screenshot readback with a native GDI DIB section whose pixel buffer is read directly.
- Go agents now exit when registration returns the server's `TERMINATED` response after a session kill.
- Go rclone exfil now obscures plain `*_PASS` profile values like the PowerShell agent before upload.

### 2026-10-08 — AI tool-round diagnostics

- Increased the default web AI tool-round checkpoint from 12 to 32.
- Added automatic continuation across four checkpoints, with an emergency ceiling of 160 rounds.
- Added `RMM_AI_MAX_TOOL_ROUNDS` for server-side checkpoint configuration.
- The web UI now displays the failed request's tool-call trace when the emergency ceiling is reached.

### 2026-10-08 — SOCKS connect race

- Fixed a race where a fast agent response could arrive before the server registered the tunnel wait event, causing a false 45-second connect timeout.
- Added regression coverage for early success and error responses.

| Field | Value |
|-------|--------|
| **Phase** | NDR detection investigation — resolved |
| **Branch** | `main` |
| **Last updated** | 2026-07-03 |
| **HEAD** | NDR detection restored; idle /cmd padding rolled back |
| **Commits** | ~60 since initial import |
| **Tests** | None in-repo (manual lab validation only) |

---

## Repository map (from code)

| Path | Lines (approx.) | Role |
|------|-----------------|------|
| `server_rmm.py` | ~2k | Threaded HTTP server: beacon + `/api/v1` operator API + embedded `--cli` |
| `agent-powershell/client_rmm.ps1` | ~2k | Windows PowerShell beacon, command execution, pivot plugin host |
| `RMM_PLUGINS_DIR` | runtime store | Operator-installed plugin artifacts from the private external repository |
| `rmm_cli.py` | ~1.3k | Operator CLI (`RmmApiClient`), interactive REPL + subcommands |
| `rmm_socks.py` | ~570 | SOCKS5 listener on server; task queue; TCP/UDP relay via pivot plugin |
| `rmm_ws.py` | ~210 | Stdlib WebSocket (operator event hub + agent SOCKS channel) |
| `rmm_tools.py` | ~430 | Shared operator tools (MCP + web AI fallback) |
| `mcp_rmm_server.py` | ~170 | FastMCP server (16 tools) |
| `rmm_mcp_client.py` | ~290 | Spawns MCP over stdio; optional Exegol MCP merge |
| `rmm_ai.py` | ~350 | OpenAI, Anthropic, and Mistral chat loop with RMM (+ Exegol) tools |
| `web/` | static | Operator UI (`/ui/`), WebSocket events, AI panel (`ai.js`) |
| `rmm_run_on_host.py` | ~215 | Batch recon by hostname (API automation example) |
| `rmm_kill_host_sessions.py` | ~115 | Kill all sessions matching hostname |
| `docs/progress.md` | — | This file |
| `docs/README.md` | — | Wiki index and documentation map |
| `docs/architecture.md` | — | Canonical current runtime architecture |
| `CLAUDE.md` | — | Agent onboarding / architecture summary |

Runtime artifacts: `RMM_logs/{downloads,screenshots,keylogs}`, `~/.rmm_cli_state.json`, `~/.rmm_cli_history`.

---

## Architecture (as implemented)

```text
                    ┌─────────────────────────────────────────┐
                    │           server_rmm.py                 │
                    │  RMMServer + RMMHandler (threaded)      │
                    ├─────────────────────────────────────────┤
 Beacon (secret)    │  /register /cmd /result /ping /socks /tun │
                    │  SocksManager × 2 ← rmm_socks.py          │
 Operator (token)   │  /api/v1/*  +  /api/v1/ws (events)      │
                    │  /ui/* static  +  POST /api/v1/ai/chat  │
                    └──────────────┬──────────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         ▼                         ▼                         ▼
  agent-powershell/         rmm_cli.py / web/          mcp_rmm_server.py
  (main loop +              (REST)                     (FastMCP tools)
   pivot plugin host)
```

**Beacon model:** poll-based, no interactive TCP shell. Latency ≥ one sleep interval (+ jitter). Commands are JSON on `GET /cmd`; results on `POST /result`.

> The [architecture overview](architecture.md) is the canonical current
> topology. The summary below describes the original single-server model kept
> here for historical progress context.

**SOCKS model:** SOCKS5 binds on **operator host** (`127.0.0.1:N`). Bytes traverse: local SOCKS client → server listener → task queue → `socks.dll` (HTTP `/socks`) → remote TCP or UDP socket on agent LAN. The TUN frontend uses a separate private relay manager and `tun.dll` (HTTP `/tun`) with the same task framing. In workspace mode, the local workspace owns the operator listener and TUN device while the central server keeps the transport bridge. The server still exposes the WebSocket transport for compatible future plugins.

---

## Development timeline (git)

1. **Foundation** (`29d7cbb` … `6f519dd`) — minimal server + PS client; REST API; headless mode; `rmm_cli.py`; web UI; beacon secret; WebSocket events; file API.
2. **Operator UX** (`e72b27e` … `89e2a9b`) — interactive CLI default; prompt_toolkit; auth/connection fixes.
3. **Security hardening** (mid history) — dual tokens, session ID validation, artifact path safety, `secure_compare`, default `127.0.0.1` bind.
4. **AI / MCP** — `rmm_tools.py`, `mcp_rmm_server.py`, web AI panel, `rmm_ai.py`, optional Exegol MCP (`rmm_mcp_client.py`).
5. **SOCKS relay** (`594c0cd` … `ec4392e`) — dedicated `/socks` channel; runspace isolation (PS 5.1/7); WebSocket pull protocol; Cloudflare/tunnel fixes; reliability + global `socks list`.

---

## Completed (by subsystem)

### Server (`server_rmm.py`, `rmm_ws.py`)

- [x] `ThreadingHTTPServer` — concurrent beacons, operator API, WebSocket handlers
- [x] Session registry: register, touch, kill, prefix resolution, `beacon_status` (online/stale/offline)
- [x] Command queue: oneshot FIFO, persistent until `__STOP__`, idle `__CONFIG__` push (deferred until `config_synced`; `config_pending` after PATCH)
- [x] Load `sessions.json` on startup — restore sleep/jitter per session id after server restart
- [x] Result handling: `output`, `file_upload`, `cloud_upload`, `exfil_progress`, `download_progress`, `screenshot`, `keylog`, `config_ack`
- [x] Result events: `file_upload` / `screenshot` include `command` for web UI pairing
- [x] Event transcript per session (`deque`, max 500); operator action logging; WebSocket broadcast (`OperatorEventHub`)
- [x] Operator REST `/api/v1`: health, sessions CRUD, config PATCH, commands, exec (blocking), upload, download, exfil, screenshot, socks, events, artifacts, AI chat
- [x] `GET /api/v1/socks` — list active relays with hostname, WS channel, tunnel count
- [x] Embedded `--cli` (readline / prompt_toolkit): full command set including keylog, persist, socks list
- [x] Security: `RMM_API_TOKEN`, `RMM_BEACON_SECRET`, `--insecure` lab flag; `MAX_BODY_BYTES` via `RMM_MAX_BODY_BYTES` (default 32 MB); path traversal guards on artifacts
- [x] Beacon/API responses include explicit `Connection: close` and `close_connection = True` so HTTP/1.1 exchanges are terminated after each request/response (no `Content-Length` — see below)
- [x] Agent `HttpWebResponse` always closed via `try/finally` in `Invoke-RmmRestMethod` and `Save-RmmBeaconFile`; `Get-RmmHttpErrorBody` always closes response in its own `finally` — prevents TCP RST caused by unread data in the receive buffer when GC finalizes an unclosed response
- [x] **TCP RST fix** (`c9702d9` → `54b8d0d` → `f28a0b4` → `843d6fe`): `c9702d9` added `Content-Length` to fix HTTP framing; this caused `ReadToEnd()` to stop early, leaving the 85-byte Cloudflare `TLS close_notify` in the TCP receive buffer — `Socket.Close()` fires RST on unread data. `54b8d0d` removed `Content-Length` (server fix). `f28a0b4` forced `KeepAlive=true` on every `HttpWebRequest`: with `KeepAlive=false` .NET skips the pool-drain step and `Socket.Close()` races against the buffered alert; with `KeepAlive=true` the pool-drain reads those bytes first → FIN. To prevent `KeepAlive=true` from letting Cloudflare multiplex all beacons over one persistent TLS session: `be68622` assigns a random `ConnectionGroupName` per request (no pool sharing between requests) and `843d6fe` calls `ServicePointManager.FindServicePoint(...).CloseConnectionGroup(guid)` in the `finally` block to tear down the idle socket immediately. Net result: one TCP flow per beacon exchange, always closed with FIN.
- [x] Chunked download reassembly (`_save_file_upload`: `.part` staging, `upload_id` / `offset` / `eof`)
- [x] `GET /api/v1/sessions/{id}/downloads` — per-session download artifact index (`download_artifacts`, disk backfill)
- [x] `queue_agent_download` / `register_download_artifact` — track remote path from queue + agent `remote_path` field
- [x] `queue_agent_exfil` — `POST …/exfil` queues `__EXFIL__` (agent rclone upload; `cloud_upload` in events)
- [x] `GET /api/v1/rclone/config` — rclone binary + masked profile status; `GET /tools/rclone.exe` beacon bootstrap
- [x] Session transcript persistence — `RMM_logs/history/{id}/events.jsonl` + `meta.json`; archive on kill; **orphan archive on server restart** (`server_restart`)
- [x] `GET /api/v1/history` — list ended sessions; `GET …/history/{id}/events` read-only transcript

### SOCKS (`rmm_socks.py`)

- [x] `SessionSocksBridge` — SOCKS5 handshake, CONNECT, UDP ASSOCIATE, and per-tunnel TCP/UDP relay
- [x] Task ops: `connect`, `send` (base64), `close`; pending sends until remote connect ack
- [x] `SocksManager` — per-session bridge; `list_relays()` for operator inventory
- [x] Agent WebSocket attach on `GET /socks` upgrade; pull-based task delivery (`pull` → `tasks`)
- [x] HTTP poll fallback when WS disconnected (`fetch_tasks` returns `[]` if WS up)
- [x] Per-tunnel `client_lock` on operator-side SOCKS socket (send vs recv thread safety)
- [x] `MAX_SENDS_PER_PULL` (32) to keep WS frames under proxy limits

### Windows client (`client_rmm.ps1`)

- [x] Config block + env overrides (`RMM_BASE_URL`, `RMM_BEACON_SECRET`, proxy, verbose, persistent HTTP)
- [x] Register with infinite retry; `sync=1` on first register and on `-Reconnect` to adopt script sleep/jitter
- [x] Fast poll after reconnect or config change (`RmmFastPoll`) — skip one sleep cycle for timely `/cmd`
- [x] HTTP transport: IPv4-only tunnel resolution, `Host` header, optional corporate proxy + default credentials
- [x] User commands: bare `cmd.exe`, `cmd:`, `PS:` / `powershell:`, `pwsh:`; cwd tracking via `RMM_CWD_SIG`; **hidden child processes** (no console flash) — see `docs/client-command-execution.md`
- [x] CMD execution — temp `.cmd` batch file + `WorkingDirectory` (no `/S /c ""` or embedded `cd /d`)
- [x] Internal commands: `__DOWNLOAD__`, `__EXFIL__`, `__UPLOAD__`, `__SCREENSHOT__`, `__KEYLOG__`, `__INSTALL_PERSIST__`, `__REMOVE_PERSIST__`, `__STOP__`, `__CONFIG__`
- [x] `__EXFIL__` — bootstrap rclone from server, ephemeral `RCLONE_CONFIG_*` env, `rclone copyto` / `rclone copy` (folders) + optional `link`; live `exfil_progress` POSTs during upload
- [x] Chunked exfil (`Send-RmmFileDownload`, varied **2 / 5 / 10 / 20 MB** chunks → `POST /result` `file_upload`; live `download_progress` POSTs; **paced by default** — `$downloadBurst` / `RMM_DOWNLOAD_BURST` for burst mode)
- [x] Keylogger job (`__KEYLOG__ start|stop|dump`) → temp file → `keylog` result type
- [x] Persistence installer copies script to `%APPDATA%` + Run key (with current URL/sleep/jitter)
- [x] SOCKS lifecycle hook: `Sync-RmmSocksChannelFromServer` starts and stops the fetched `socks.ps1` plugin from the `/cmd` flag
- [x] SOCKS PowerShell plugin: authenticated fetch, HTTP poll, TCP task handling, and dedicated runspace execution
- [x] SOCKS Go plugin: persistent `socks.dll` child for Windows amd64, with cancellation owned by the beacon
- [x] TUN Go plugin: separate persistent `tun.dll` child and `/tun` channel for TUN routing in the configured storage namespace
- [x] Runspace/process isolation: PowerShell `BeginInvoke` and Go plugin child keep relay code outside the main beacon loop

### Operator CLI (`rmm_cli.py`)

- [x] `RmmApiClient` mirrors REST API
- [x] Interactive REPL (default): list, use, info, exec, run, persist, stop, files, screenshot, socks/socks list/socks stop, events, health, background streaming output
- [x] Subcommands: `health`, `sessions list`, `session use|info|kill`, `run`, `exec`, `config set-sleep|set-jitter`, `download`, `upload`, `events`, `socks list`
- [x] Session state `~/.rmm_cli_state.json`; tab completion; `--json` mode
- [x] `print_socks_relays()` for global SOCKS inventory

### Web UI (`web/`)

- [x] Login via API token (`sessionStorage`)
- [x] **Light / dark theme** — default dark; toggle on login + header; `localStorage` `rmm_theme` — see `docs/web-ui-theme.md`
- [x] **Deploy agent** sidebar — generate full `client_rmm.ps1` or config snippet; panel pinned to sidebar bottom (see `docs/web-agent-generator.md`)
- [x] **Resizable sidebar** — drag handle between sidebar and console; width stored in `sessionStorage`
- [x] Session sidebar with beacon status, sleep/jitter display, **first connection time** (`first_seen`); hover **Beacon** (sleep/jitter dialog) and **Kill** on live rows
- [x] Shell: queue command by default (**Enter**), exec wait via **Ctrl+Enter**, kill session; **↑/↓ history + Tab completion** (§4)
- [x] Shell **operator meta commands** — `exfil`, `download`, `screenshot` routed to REST (not agent `cmd.exe`); see `docs/web-shell-completion.md`
- [x] **Queued result placement** (§6) — command blocks; results render under echoed line via `ev.command` / tool kind
- [x] Files: download queue, upload (base64), screenshot, **rclone exfil** (profile dropdown + live upload progress bar, same UI as downloads); panel renamed **Files & screenshot** (beacon config moved to sidebar)
- [x] **Downloads from agent** panel — list `GET …/downloads`, download/preview, WS refresh on `file_upload`; live download progress bar in shell
- [x] Live session list — WebSocket + 12 s poll; client-side beacon status refresh; kill closes console
- [x] **Session history** sidebar — browse archived transcripts (`GET /api/v1/history`); hover **Delete** removes one archive; **Clear all** removes every ended archive (`DELETE /api/v1/history`)
- [x] Beacon config apply (PATCH sleep/jitter) — per-session **Beacon** button opens modal dialog
- [x] **SOCKS5 relays** — start/stop/restart selected session relays and manage the global relay inventory; see `docs/web-socks.md`
- [x] **TUN routing** — configure the device, routes, helper, and private agent
  transport from the selected session; ownership follows storage mode; see
  `docs/tun-pivot.md`
- [x] WebSocket `/api/v1/ws` + polling fallback; shared event transcript with CLI
- [x] AI assistant panel (`ai.js` + `POST /api/v1/ai/chat`); tab-scoped OpenAI, Anthropic, and Mistral keys with live validation and provider-specific model lists; Mistral standard and multi-message completion responses normalize to displayable text, and Mistral tool results include the function name required to end a tool loop; optional Exegol MCP settings; **server skills** (`ai-skills/*.md`, `GET /api/v1/ai/skills`) — see `docs/ai-providers.md` and `docs/web-ai-skills.md`
- [x] **AI task completion reports** — default `task-completion-report` skill requires a concise work summary; successful responses include server-generated UTC task start and completion timestamps
- [x] **Full live results after WS truncation** — large WebSocket event bodies remain bounded, and the web UI fetches the full event body from REST when `body_truncated` is set; see `docs/web-live-full-results.md`
- [x] **AI chat memory** — per-session history on server (`RMM_logs/history/{id}/ai_chat.json`); **Reset chat**; purge on kill/delete — see `docs/web-ai-chat-memory.md`
- [x] **UI configuration export/import** — versioned JSON restore for allowlisted browser settings; secrets excluded by default — see `docs/web-ui-configuration.md`
- [x] Go deployment parameters persist in tab-scoped `sessionStorage` and participate in UI configuration export/import.
- [x] **Server unreachable modal** — gray backdrop + Retry / Disconnect when REST health or API fetch fails (network); periodic health probe while connected

### MCP & AI (`mcp_rmm_server.py`, `rmm_tools.py`, `rmm_ai.py`)

- [x] MCP tools mirror REST operator API — see `docs/mcp-parity.md`; enforced by `make check-parity` (`scripts/check_operator_parity.py`)
- [x] `session_ref` = hostname, id prefix, or full UUID (`_resolve_session_id`)
- [x] Web AI can use MCP stdio or direct `execute_tool` (`RMM_AI_USE_MCP=0`)

### Auxiliary scripts

- [x] `rmm_run_on_host.py` — find session by hostname, set sleep, run default recon command list
- [x] `rmm_kill_host_sessions.py` — kill all sessions for hostname

### Documentation

- [x] `../README.md` — setup, API tables, SOCKS troubleshooting, MCP mapping
- [x] `docs/README.md` — wiki index and documentation map
- [x] `docs/architecture.md` — current deployment and data-flow diagrams
- [x] `docs/plugin-architecture.md` — dynamic plugin volume and command boundary
- [x] `docs/workspace-deployment.md` — multi-operator data-plane ownership
- [x] `docs/server-startup.md` — local wrapper, Docker entrypoint, and plugin store
- [x] `CLAUDE.md` — project overview for agents
- [x] `docs/downloads-browser.md` — web downloads panel + `GET …/downloads` API
- [x] `docs/web-socks.md` — WebUI pivot controls and global SOCKS5 relay inventory
- [x] `docs/tun-pivot.md` — TUN setup, routing, WebUI controls, and limits
- [x] `docs/pivot-plugins.md` — agent-side pivot plugin delivery and lifecycle
- [x] `docs/web-shell-completion.md` — shell ↑/↓ history and Tab completion
- [x] `docs/client-command-execution.md` — hidden CMD/PS process launch on the agent
- [x] `docs/prd.md` — product vision; EDR/AV compatibility as commercial gate
- [x] `mcp.example.json` — Cursor MCP config template

---

## Operator feature parity

| Capability | REST API | CLI interactive | CLI subcommand | MCP | Web UI | Server `--cli` |
|------------|:--------:|:---------------:|:--------------:|:---:|:------:|:--------------:|
| Health check | ✅ | ✅ | ✅ | ✅ | ✅ (on connect) | ❌ |
| List sessions | ✅ | ✅ `list` | ✅ | ✅ | ✅ | ✅ |
| Session detail | ✅ | ✅ `info` | ✅ | ✅ | ✅ (sidebar) | ✅ |
| Kill session | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Queue command | ✅ | ✅ / bare line | ✅ `run` | ✅ | ✅ | ✅ |
| Exec + wait | ✅ | ✅ `exec` | ✅ | ✅ | ✅ | ✅ |
| Persistent cmd | ✅ | ✅ `persist` | via API | ✅ | ❌ | ✅ |
| Stop persistent | ✅ | ✅ `stop` | via API | ✅ | ❌ | ✅ |
| Patch sleep/jitter | ✅ | ✅ `set_*` | ✅ `config` | ✅ | ✅ | ✅ |
| Download file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cloud exfil (rclone) | ✅ | ✅ `exfil` | ✅ | ✅ `queue_exfil` | ✅ profile select | ✅ `exfil` |
| List session downloads | ✅ | ❌ | ❌ | ✅ `list_session_downloads` | ✅ | ❌ |
| Global downloads and screenshots | ✅ | ✅ client method | ❌ | ✅ `list_artifacts` | ✅ | ❌ |
| Session history (archived) | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ |
| Delete archived history | ✅ | ❌ | ❌ | ✅ `delete_history` | ✅ per row | ❌ |
| Clear all archived history | ✅ | ❌ | ❌ | ✅ `clear_history` | ✅ header button | ❌ |
| Agent script (`GET /agent/script`) | ✅ | ❌ | ❌ | ✅ `get_agent_script` | ✅ | ❌ |
| Upload file | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Screenshot | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| SOCKS start/stop | ✅ | ✅ | ❌ | ✅ | ✅ dialog | ✅ |
| Pivot list (SOCKS5) | ✅ | ✅ `socks list` | ✅ `socks list` | ✅ | ✅ dialog | ✅ `socks list` |
| TUN start/stop/list | ✅ | ✅ `tun` | ✅ `tun list`, `tun start`, `tun stop` | ✅ | ✅ dialog | ✅ `tun` |
| Events / transcript | ✅ | ✅ | ✅ | ✅ | ✅ WS | ❌ |
| Keylog | queue only | ❌ | ❌ | ✅ `queue_keylog` | ❌ | ✅ |
| Install/remove persist | queue only | ❌ | ❌ | ✅ `install_persistence` / `remove_persistence` | ❌ | ✅ |

**Gaps to note:** CLI subcommands still lack dedicated SOCKS start/stop commands. MCP parity requirement: `docs/mcp-parity.md`.

---

## In Progress

- [ ] None tracked in-repo.

---

## Up Next

- [ ] **Web UI — archived sessions missing entered commands** — see [tech plan §11](tech-plan.md#11-web-ui--archived-sessions-missing-entered-commands-bug): history replay should show operator-typed command lines, not only output or internal `__DOWNLOAD__` tokens
- [ ] **Web UI — traffic & beacon charts** — see [tech plan §1](tech-plan.md#1-beacon--traffic-visualization-web-ui): `GET …/metrics`, poll/byte counters, Chart.js panel
- [ ] **Web UI — interactive shell (cmd / PowerShell)** — see [tech plan §5](tech-plan.md#5-web-ui--interactive-shell-mode-cmd--powershell): shell mode selector, cwd prompt, `PS:` / `pwsh:` dispatch
- [ ] **Chunked upload** (symmetry with download; large `content_b64` still single POST today)
- [ ] **CLI subcommands:** `screenshot`, `socks start|stop` (only interactive today)
- [ ] **Tests:** SOCKS task ordering, chunked download reassembly, API auth, WS handshake
- [ ] **EDR/AV compatibility (commercial gate)** — see [PRD](prd.md#non-negotiable-edr-and-antivirus-compatibility): signing, lab matrix, review persistence/SOCKS/keylog detections
- [ ] **Generic persistent plugin lifecycle** — one-shot plugin commands are
  hot-loaded from the runtime store; arbitrary persistent plugins still need
  explicit lifecycle, cleanup, and operator API support.

---

## Key decisions

| Topic | Decision |
|-------|----------|
| Commercial viability | **EDR/AV must not routinely block the agent** — see `docs/prd.md`; otherwise the product is not sellable |
| Control plane | Beacon HTTP only (`/cmd`); SOCKS control via `socks_active` flag, not shell command |
| SOCKS data plane | Shipped pivot plugins use HTTP `GET/POST /socks`; WebSocket upgrade remains for compatible future plugins |
| Task order | Server returns connect/close before send; agent sorts connect → send → close |
| SOCKS bind | Listener on **RMM server** host; tools use `socks5://127.0.0.1:port` there |
| Downloads | Varied chunk sizes (2 / 5 / 10 / 20 MB raw) on `POST /result` `file_upload`; `upload_id` + `offset` + `eof`; paced by default (`$downloadBurst`) |
| Register sync | First `/register` sends `s`, `j`, `sync=1` so server adopts client script timing |
| Operator surfaces | New features should land REST → CLI → MCP together (`rmm_tools.py`) |
| AI | OpenAI key from browser; server spawns MCP locally; Exegol optional |
| Docker deploy | **Delivered** — central control plane plus optional per-operator workspace sidecar; see `docs/workspace-deployment.md` and `docs/architecture.md` |
| Security default | Dual secrets; localhost bind; no `--insecure` in production |

---

## Deviations from planned docs

- `docs/tech-plan.md` covers traffic charts; `docs/rclone-exfil.md` documents rclone exfil; `docs/downloads-browser.md` documents the web downloads panel (shipped).
- Agent `file_upload` JSON may include `remote_path` (full agent path) alongside `filename`.
- SOCKS uses a custom JSON task protocol over HTTP poll for the shipped pivot plugins, not a generic byte-stream tunnel. The server WebSocket adapter remains available for compatible future plugins.
- Embedded `server_rmm.py --cli` remains alongside `rmm_cli.py` (duplicate UX).
- Keylog + persistence exist on the client and embedded CLI; keylog and persistence wrappers are also exposed through MCP, while the WebUI has no dedicated controls.

---

## NDR detection analysis (2026-07-02 → resolved 2026-07-03)

### Detection pipeline

```text
beacon_detector  →  (beacon topic)  →  beaconator / deep_tunnel_t1
```

- **beacon_detector**: aggregates raw isessions by `(client_ip, server_ip)`. Requires:
  - `flags.isClose = true` (session must be closed), `duration ≤ 300 s`, in-to-out, HTTP/TLS only (no QUIC)
  - ≥ 9 sessions AND stream spans ≥ 10 minutes before the first evaluation fires
  - Evaluates every 1 minute; deletes `client_ja3` buckets with only 1 destination IP
- **beaconator** (decision tree): consumes beacon topic messages. Key filters:
  1. Learning period: **10 days** in production — no alerts during this phase
  2. `filtered_non_novel_ja3_pair`: JA3 pair age > 4 days → filtered. Stored in
     `/data/colossus/state/beaconator/ja3_pair_tracker_age.json` (CouchDB state); age is
     independent of SNI/tunnel URL, only resets when the pair is genuinely new.
  3. `filtered_idle_beacons`: requires variance in `bytes_sent` or `bytes_received`
- **deep_tunnel_t1** (LSTM): consumes beacon topic. Requires SCV of `bytes_sent` or
  `bytes_received` > 0.01. Pure idle polls (constant ~47 B responses) → SCV ≈ 0 → blocked.

### Root cause of detection loss after 2026-06-26

1. The Windows .NET `HttpWebRequest` + Cloudflare JA3 pair (`3b5074b1b5d032e5620f69f9f700ff0e`,
   various JA3S) exceeded 4 days in beaconator's tracker → `filtered_non_novel_ja3_pair` blocked.
   Changing the Cloudflare tunnel URL does **not** help — the JA3 pair is independent of hostname.
2. deep_tunnel_t1's activity filter also blocked idle sessions (SCV ≈ 0 on constant `/cmd` responses).

### Resolution (2026-07-03)

1. **Manually reset the JA3 pair tracker**: edited
   `/data/colossus/state/beaconator/ja3_pair_tracker_age.json`, removed the stale entries.
   → The pair is immediately "novel" again on the next beacon received.
2. **Executed commands via C2**: POSTs to `/result` with varied output sizes (from `whoami`,
   `ipconfig`, `dir`, etc.) create a bimodal `bytes_sent` distribution → SCV >> 0.01.
   This unblocks both beaconator's idle beacon filter and deep_tunnel_t1's activity filter.
3. Detection was confirmed restored after ~2-5 minutes (next beacon_detector evaluation window).

**Key lesson:** an agent that only polls `/cmd` with no command activity becomes invisible to
beaconator and deep_tunnel_t1. Command execution (varied result sizes) is what generates the
detectable traffic signature. Pure idle beaconing produces constant packet sizes → filtered.

**Idle /cmd padding (attempted and rolled back):** a `_p` field with random hex (16–512 bytes)
was added to idle `/cmd` responses to vary `bytes_received`. This was rolled back (`70adc44`)
because beacon_detector briefly stopped publishing after the state reset (10-min window not
yet reached), making it hard to validate. The padding approach remains valid in principle but
was not needed once commands were executed.

**Double request per beacon cycle:** commit `caf9b1a` (2026-05-21) added `Register-RmmSession`
to every beacon cycle. This creates two TCP connections per cycle (~70ms apart), making
beacon_detector's inter-arrival distribution bimodal `[~70ms, ~5000ms]` instead of clean
`[~5000ms]`. This predates the detection loss and is not the root cause.

---

## Known issues

| Issue | Detail |
|-------|--------|
| SMB / “NETBIOS timeout” via SOCKS | The regular SOCKS path carries TCP (`:445`), not 137–139. Windows may show generic timeout on `STATUS_LOGON_FAILURE`. Use target IP. The TUN path adds UDP association support. |
| Upload size | Single base64 POST per file; no chunking (downloads are chunked). |
| SOCKS throughput | Pull-loop latency; adequate for interactive use, not bulk transfer optimized. |
| Pivot throughput | HTTP pull-loop latency; adequate for interactive use, not bulk transfer optimized. |
| ~~Register + Web UI WS deadlock~~ | **Fixed:** single `_io_lock` on operator WS blocked `broadcast_sessions` during idle `recv_json` → Cloudflare 524 (~100s). Split send/recv locks in `rmm_ws.py`; async/debounced session broadcast; lighter register path. |
| ~~Beacon hang after large results~~ | **Fixed:** `/result` waited for history write + full-body WS push before HTTP 200; slow clients could block origin. Now respond 200 immediately, process async; truncate WS event bodies; 15s WS send timeout; client shows `Beacon poll…` and reports failed result POSTs. Web UI hydrates truncated live bodies from REST so operators still see the complete output. |
| No automated tests | Regressions caught manually only. |
| ~~Web UI queued results~~ | **Fixed:** command blocks + `pendingCommandBlocks` map in `web/app.js`; results match via `ev.command` / tool kind (download, exfil, screenshot, upload); history replay uses same pairing. |
| ~~Download > 2 GB Int32~~ | **Fixed:** `Send-RmmFileDownload` uses `[long]` for file size, offset, and `[Math]::Max([long]0, …)` (PowerShell `[Math]::Max(0, …)` picked the Int32 overload). |
| ~~Server restart vs beacon config~~ | **Fixed:** load `sessions.json` on startup; defer idle `__CONFIG__` until `config_synced`; `config_pending` priority after PATCH; agent `-Reconnect` sends `sync=1`; fast poll after reconnect/config change. |
| ~~CMD nested quotes / cwd~~ | **Fixed:** temp `.cmd` under `%TEMP%` runs the operator line literally; `/d /c` passes only the script path; `WorkingDirectory` for cwd (no `/S` + `%CD%` on `H:\`). |
| Web UI archived commands | History sidebar replays events without the operator-entered command line (`rmm> …`) — shows output only, internal tokens (`__DOWNLOAD__`), or `result » cmd` meta; see [tech plan §11](tech-plan.md#11-web-ui--archived-sessions-missing-entered-commands-bug). |
| ~~README stale~~ | **Fixed 2026-10-13:** security section now documents the 32 MB default body cap and `RMM_MAX_BODY_BYTES`. |
| Web ↔ CLI parity | No keylog in web UI; no interactive cmd/PS mode or traffic/beacon charts yet. |

---

## Lab validation notes (2026-06-02)

- SOCKS end-to-end confirmed: SMB negotiate + NTLM through `socks5://127.0.0.1:1080` on server host.
- Wireshark on target path showed `STATUS_LOGON_FAILURE` (credentials), not relay failure.
- WS stability fixes: task order (`824a086`), Aborted recv (`a965e4d`), socket lock (`9fd848c`).

### 2026-10-30 — Deployment review fixes

- Added portable `artifact_ref` responses for configured scripts and Go builds.
  CLI and AI uploads fetch these through the authenticated artifact API, allowing
  MCP and RMM to run on separate filesystems. Local uploads remain supported.
- Reject non-object Go build configuration with HTTP 400 before invoking builds.
- Added regression coverage for remote artifact uploads, reference validation,
  AI tool forwarding, and invalid configuration types.

### 2026-10-30 — Workspace deployment and isolated build artifacts

- Forward generated deployment artifact downloads from workspace services to
  central storage; allow authenticated workspace access to configured scripts
  consistently with Go builds while retaining session-artifact restrictions.
- Give Go compiler outputs and ZIP archives unique paths for every build while
  preserving executable names inside archives. Extend the workspace build proxy
  timeout to cover compilation.
- `DeploymentArtifactTests` covers workspace routing, central artifact access,
  and concurrent builds with distinct binaries, archives, and matching digests.

### 2026-10-30 — Align upload surfaces

- Interactive CLI uploads now accept artifact references like the command CLI.
- MCP and web AI upload descriptions explain queued versus delivered and require
  a fresh matching agent result before dependent actions. Shared responses
  include delivery state and completion guidance.
- Removed the WebUI fallback that associated unrelated upload results with a
  pending upload. Added executable matching regressions for multiple uploads,
  unrelated commands, similar paths, and already completed blocks.

### 2026-10-30 — Server-owned background AI jobs

- Added persisted asynchronous AI jobs with owner-scoped status, idempotent
  submission, progress checkpoints, and restart interruption without replay.
- WebUI submits once and reconnects to job results; workspace proxies short job
  requests. CLI/MCP expose job inspection. Existing synchronous chat remains.
- Documented execution ownership, sensitive result storage, reset behavior,
  and recovery limits in `docs/ai-background-jobs.md`.

### 2026-10-30 — Workspace AI session selection

- Removed the bearer-only precheck from AI session discovery; the API validates
  operator or workspace credentials and filters sessions.
- Resolve references from visible sessions and reject duplicate hostnames or
  ambiguous prefixes. Preserve resolution errors through session tools rather
  than silently choosing the first candidate.
- Added regression coverage for workspace authentication, ambiguity, exact
  matching, and refusal to execute against invisible sessions.

### 2026-10-30 — Direct CLI workspace authentication

- `ensure_client` now sends workspace headers for direct central-server access.
  Workspace credentials take priority over inherited operator credentials;
  explicit `--token` remains available for local or administrator access.
- Incomplete workspace configuration and rejected workspace credentials fail
  without an operator-token fallback. Added authentication precedence tests.

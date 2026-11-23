# Go agent

See the [documentation wiki](README.md) and the [architecture overview](architecture.md)
for the agent's place in the control plane and plugin boundaries.

The repository includes a dependency-free Go agent in `agent-go/`. It uses the
same HTTP beacon protocol as `client_rmm.ps1`, so the server does not need a
second registration or command path.

## Supported platform

Managed agents are Windows-only. The build allowlist is Windows `amd64` and
`arm64`; PE pivot plugins currently require `amd64`. CLI, MCP/AI, and WebUI
expose Windows targets only. Non-Windows Go adapters support development tests
on the operator's host and do not imply Linux/macOS deployment support. The
control plane and Exegol workspaces may still run on non-Windows hosts.

## Web UI workflow

1. Sign in to `/ui/` with the operator API token.
2. Open the separate **Deploy agent (Go)** section.
3. Set the server URL and beacon secret, then choose the target and timing.
4. Click **Compile on server** and download the returned ZIP archive.

The archive contains the target binary, `README.txt`, and a launcher script:
`run-agent.ps1` for Windows. UI builds embed the
selected URL and secret; the launcher also applies them to the environment,
replacing stale inherited values. Generic builds with no connection settings
require `RMM_BASE_URL` and `RMM_BEACON_SECRET` to be supplied first. Windows does
not need `screen`; use the PowerShell launcher in a console, Task Scheduler,
or a service wrapper when the agent must remain running after logout.

The source and build endpoints require the operator API token. Builds use the
repository source only, disable CGO, apply `-trimpath`, and write ZIP artifacts
to `RMM_logs/agent-builds/`. The server accepts only the two Windows architectures
listed above. Each successful build returns its byte count and SHA-256 digest.

## Server-configured builds (AI/CLI autonomy)

`POST /api/v1/agent/go/build` accepts `"use_server_config": true`: when the
request's `config` omits the base URL or beacon secret, the server fills them
from the active beacon transport and the server's own `RMM_BEACON_SECRET`. The
AI tools `build_go_agent` (MCP) and the CLI command `agent go-build` use this
mode, so an operator or the AI assistant can produce a fully configured archive
without handling the secret manually. The build response includes the
portable `artifact_ref` for staging through `queue_upload.local_path`, plus the
usual filename, size, SHA-256, and download URL. The secret is never part of
any response. When no beacon transport is active, the endpoint answers
`no_active_beacon_url` instead of building against a stale URL.

The server must have the Go toolchain installed and available as `go`. The
build request has a 120-second timeout and does not execute shell text supplied
by the browser. The server passes `-buildvcs=false` because source trees
mounted into Exegol or Docker can contain Git metadata that is unavailable or
owned by a different container user; the agent does not need embedded Git
revision metadata.

## Runtime configuration

Nonempty target-host environment variables override embedded build values.
For the workspace ID, an explicitly empty variable clears the embedded value:

| Variable | Required | Default |
| --- | --- | --- |
| `RMM_BASE_URL` | unless embedded | Build value |
| `RMM_BEACON_SECRET` | unless embedded | Build value |
| `RMM_SESSION_ID` | no | Build value or `go-` plus timestamp |
| `RMM_AGENT_WORKSPACE_ID` | configured deployments | Owning workspace embedded by the authenticated build |
| `RMM_SLEEP_SECONDS` | no | `60` |
| `RMM_JITTER_PERCENT` | no | `30` |
| `RMM_HTTP_PROXY` | no | direct connection |

The agent registers before each command cycle, polls `/cmd`, executes shell
commands, posts `/result`, and accepts `__CONFIG__`. Downloads are sent in
2 MiB resumable chunks using the server's existing `upload_id` and `offset`
protocol. On Windows it supports `cmd.exe`, `cmd:`, `PS:`, `powershell:`, and
`pwsh:` dispatch. The non-Windows command router exists only for development tests.

## WebUI download

Click **Download agent** to compile with the selected settings and automatically
start the authenticated ZIP download. The button stays disabled through both
steps. If downloading fails, use the generated download link to retry without
recompiling. Browsers control the save location and may ask for download approval.

The shared `download` helper in `web/go-agent.js` handles automatic downloads
and link retries, preventing overlapping downloads and retaining completion
guidance after retries. Blob URLs are released after a 60-second grace period
to allow the browser to consume them. It retains connection checks so an old build cannot download
after the operator reconnects. The status announces progress and errors.

## Quick Tunnel connection checks

The generator refreshes transport status before compiling. If its selected
`trycloudflare.com` hostname differs from the active Quick Tunnel, it asks the
operator to select the current beacon URL. That button now refreshes the URL
instead of copying cached state. Custom/named routes are preserved.

The build API's `validate_agent_connection` checks a supplied beacon secret
against the server's active secret before invoking Go. It also rejects a known
stale Quick Tunnel hostname. It never returns either secret. Generic builds
without supplied connection settings remain supported.

`currentBeaconUrl`, `quickTunnelOrigin`, and `useCurrentBeaconUrl` in
`web/go-agent.js` fetch current transport metadata, distinguish Quick Tunnel
origins, and update the form only when requested. Transport errors preserve edits.

For a previously generated agent that cannot connect, inspect its connection
error in `%TEMP%\minimal-rmm-agent.log` (or `RMM_LOG_FILE`):

- `403 FORBIDDEN` from the RMM origin indicates a beacon-secret mismatch. Use
  the active `RMM_BEACON_SECRET`, not the operator or Cloudflare token. Server
  environment/startup flags override saved values; saved changes need a restart.
- `404 Not Found` can indicate that the tunnel points to the administration
  listener instead of the beacon listener.
- DNS, Cloudflare routing errors, and `502` require checking the current public
  URL and connector's origin listener before changing credentials.

An old `RMM_BASE_URL` or `RMM_BEACON_SECRET` on the target overrides a new binary
when it is launched directly. Use the configured archive's launcher or correct
those overrides. Regression coverage exercises registration through a local
HTTPS reverse proxy; it does not claim a live Cloudflare deployment test.

## Command dispatch

### Foreground execution and multiple agents

The command loop calls `execute` synchronously, and `runCommand` waits for
`CombinedOutput`. There is no per-command execution deadline. Windows
`WaitDelay` bounds cleanup after exit or context cancellation, not how long a
running command may execute. The PowerShell agent also waits for its command
child to exit.

The generated Windows launcher uses `& $binary`; the shell launcher uses
`exec`. Both run in the foreground. Invoking either through an existing agent
can stop that agent from polling until the new process exits. An offline
session or operator timeout alone does not prove that its process was killed.
Use the `__SPAWN__` command below instead of launching a second agent through
a foreground shell command.

A second process must have a distinct session ID. Nonempty `RMM_SESSION_ID`
overrides the embedded value, and generated launchers skip empty configuration
values, preserving an inherited ID. Use a fresh identity and verify that both
sessions answer commands. Deployment automation must establish independent
process lifetime and standard streams before launch; queued submission alone
does not provide either property. These constraints have been reviewed in
source; live Windows coexistence remains unverified.

### Spawning a second agent in band (`__SPAWN__`)

The server reserves a random session ID before sending `__SPAWN__` with a JSON
body containing `session_id` and `workspace_id`. `parseSpawnCommand` validates
that identity; `spawnCommand` and `spawnAgentWithIdentity` start a detached copy
with the requested ID and owner. `spawnEnvironment` replaces inherited runtime
connection settings and workspace binding. Bare `__SPAWN__` remains supported
for legacy direct commands and generates a new identity locally.

On Windows, `startDetachedAgent` calls CreateProcessA exactly once. An active
make_token thread token does not change the child process identity or transfer
its network credentials. The API exposes `spawn_method=CreateProcessA` and
sets `identity_degraded=true` when an active thread token is not propagated.
The spawn path never enables process privileges or switches creation APIs.
`dispatchSpawn` records the single attempt, including a Windows failure code.
`spawnANSI` converts arguments to the system ANSI code page and rejects
unrepresentable characters. `buildEnvBlock` sorts entries and preserves UTF-16.

`spawnWithLogonDetached` handles spawnas with CreateProcessWithLogonW and
explicit username/password credentials. `runWithPipesLogonW` handles runas
with the same API and captures stdout/stderr. Neither command falls back to
CreateProcessAsUser, CreateProcessWithTokenW, or thread impersonation. A logon
failure is returned to the operator; the requested identity is never silently
replaced with the caller identity. These commands require applicable account
logon rights and Secondary Logon availability; CreateProcessWithLogonW does
not support a LocalSystem caller. `make_token` behavior is unchanged.
`logonCredentialPointers` rejects embedded NULs and supplies a NULL domain when
none was provided, including UPN usernames. `createInheritablePipe` uses the
native security-attributes size on both supported 64-bit targets.

Runas retains its 120-second execution deadline. `collectRunasOutput` polls
available pipe bytes and process completion on one goroutine, preserving stdout,
stderr, nonzero exit codes, and cancellation errors without a blocking reader.
It checks immediate-child completion on every iteration while consuming output.
Once the child has exited, it snapshots the queued bytes and drains at most that
snapshot; output subsequently written by descendants is excluded. The portable
`collectRunasOutputWithOps` implements this lifecycle through `runasOutputOps`,
with a deterministic host regression for a pipe that never becomes empty and
native Windows coverage for a continuously writing descendant.
Cancellation terminates the immediate command process and waits up to five
seconds for termination. Descendants are not terminated; a descendant retaining
the output pipe does not prevent return after the immediate command exits.

The acknowledgment also carries a `spawn diagnostics=` JSON line. The
`spawnDiagnostics` record contains ordered `spawnAttemptDiagnostic` entries,
containing the single CreateProcessA attempt. Numeric Windows errors
are extracted through wrapped errors; messages come from the Windows error
code rather than raw command/path errors. Passwords and token handles are never included or logged.

The server exposes validated fields under `spawn_diagnostics`. WebUI lists each
attempt and its error code; the full response includes process identity,
credential availability, creation flags, token kind (`spawnTokenType`), and
read-only process/active-token privilege snapshots (`spawnTokenPrivileges`).
Only SeAssignPrimaryTokenPrivilege, SeIncreaseQuotaPrivilege, and
SeImpersonatePrivilege are queried. States are enabled, disabled, absent,
removed, or unknown. The snapshots are read-only: spawn does not enable
privileges or change creation APIs. A rebuilt agent is required; errors discarded by an older run cannot be recovered
afterward.

This changes the Go agent binary and requires rebuilding and
deploying the agent; updating only the server does not update running agents.

The child's inherited process token is independent of the parent's subsequent
`rev2self`. It does not populate the child's in-memory `make_token` label, so
`/uid`'s active network-identity line is not proof of inherited credentials.
Confirm outbound authentication against an authorized network service. Native
Windows validation of token inheritance remains required.

The agent acknowledgment (`spawned session=<id> pid=<pid>`) proves process
creation only. The server's `_new_deployment_id` and `_verify_deployment` require
the expected new session to register under the source workspace before returning
`ok: true` and `registered: true`. A missing acknowledgment or operator wait
timeout is inconclusive; the server still checks the known session ID rather
than automatically retrying. The response retains that ID for later inspection.

Deployment confirmation waits only for the expected new session to register
under the source workspace. No echo, internal ping, shell, or additional command
is sent to either agent. The legacy `verified` field mirrors `registered` for
compatibility; parent/source/target responsiveness and probe-output fields are
no longer returned. Normal beacons provide ongoing activity information.
Registration does not establish shell execution privileges or network identity.

With `verify: false` (`--no-verify`), the response reports `accepted: true`,
`pending: true`, `ok: false`, and `registered: null`; request acceptance is not
successful deployment. Inspect registration before relying on that session.
This behavior also applies to lateral deployment.

Surfaces: REST `POST /api/v1/sessions/{id}/spawn`, CLI `spawn` and `/spawn`,
WebUI `/spawn`, MCP/AI `spawn_session`. The request budgets are `spawn_timeout`,
`register_timeout`. `verify_timeout` is accepted for compatibility but no longer
used by the server.

### Alternate network identity (`__MAKE_TOKEN__` / `__REV2SELF__`)

All Go-managed server requests enter `requestContext` and `runServerRequest`.
On Windows, `suspendServerIdentity` locks the calling goroutine to its OS thread,
opens that thread's actual impersonation token with OpenAsSelf, and checks
RevertToSelf before allowing any request work. It retains the clean thread
through request construction, transport, response-body reads, and body close.
The captured token is restored and closed on success, cancellation, or error.
Capture/revert failures block the request; restore failures are returned.

Capture, revert, or restore failure sets a terminal identity fault. Further
requests, operator commands, and spawn attempts are rejected, and the agent loop
exits rather than continuing with uncertain identity state. An uncertain thread
is kept pinned so it cannot return to the scheduler's reusable thread pool.

The guard does not reactivate revoked tokens when the thread was not
impersonating. Explicit rev2self checks RevertToSelf, closes and clears the
operator token and credentials, and cannot be undone by restoration helpers.
Token replacement reuses the existing thread lock. Worker goroutines apply the
enabled token on their own pinned thread and clear it before releasing that
thread; they do not change the global enabled-state flag.

The guard covers registration, polling, results,
file transfer, rclone bootstrap, and plugin artifact downloads through the common
request path. The former result-only guard left other paths uncovered.
Non-Windows host tests use a no-op suspension adapter. `requestAsProcess`
contains the guarded HTTP implementation.

This isolates thread credentials, not inherited process credentials. An agent
started with a netonly process token still carries those network credentials
after RevertToSelf. Native plugin callback implementations are separate from
the Go request guard and require their own validation. The standard Go HTTP
transport here does not implement automatic Windows integrated proxy login.
Native Windows proxy authentication must be tested before claiming end-to-end
credential isolation.

The Windows identity workflow (`.github/workflows/windows-agent-identity.yml`)
runs the real thread-token, permanent revocation, proxy-407 challenge, and
production spawn-dispatch tests on Windows Server 2022. The proxy test checks
that the standard Go transport does not send ambient credentials in response to
Negotiate; it does not test an SSPI-enabled proxy transport. `dispatchSpawn`
invokes the single CreateProcessA implementation supplied through `spawnMethods`,
allowing host tests to exercise success and failure with and without an active
token. Native child fixtures also cover runas output, exit-code propagation,
deadline termination, and pipes retained by descendants without alternate
credentials. `identityBoundaryFailure` records the terminal identity fault.

The Go agent can adopt another identity for outbound network authentication on
an impersonating thread, with the same local-versus-network identity distinction
as `runas /netonly`. Standard spawn uses the process primary token and does not
inherit a thread's alternate credentials. Operator command children explicitly
given a token through `applyCommandToken` use a separate creation path. The agent
is never injected into another process, and no credential material is written
to the agent log.

Agent-side behavior (`agent-go/make_token.go`,
`agent-go/make_token_windows.go`):

- `__MAKE_TOKEN__` carries a JSON body
  (`{"domain":"…","user":"…","password":"…"}`) on a second line, mirroring
  `__EXFIL__`, preserving password bytes without target-shell interpretation.
  Interactive CLI and WebUI commands require single or double quotes around
  passwords containing spaces; backslashes stay literal. Use the other quote
  style when the password contains a quote, and `""` for an empty password.
  A combined `DOMAIN\user` value is split automatically.
- `LogonUserW` runs with `LOGON32_LOGON_NEW_CREDENTIALS` /
  `LOGON32_PROVIDER_WINNT32` — the exact logon type behind
  `runas /netonly`. The returned token is a clone of the caller's token
  with the new network credentials attached. This logon type does not
  validate the credentials, so success does not prove the password is
  correct; verify with a network command.
- The token is stored in memory (mutex-protected) and impersonated on the
  calling OS thread. `applyCommandToken` also supplies it to operator command
  children through Go's `SysProcAttr.Token` (`CreateProcessAsUser`), but only
  when the agent process holds an enabled `SeAssignPrimaryTokenPrivilege`;
  without it, children launch through plain `CreateProcess` with the agent
  identity and the command output carries a degraded-identity notice instead
  of failing to run. `SeImpersonatePrivilege` does not enable this path: it
  belongs to `CreateProcessWithTokenW`, and a live elevated administrator
  holding only SeImpersonate confirmed `CreateProcessAsUser` fails with
  ERROR_PRIVILEGE_NOT_HELD. Standard
  spawn does not use this helper; spawnas and
  runas use explicit credentials. The cloned token retains the local identity
  and supplies different outbound
  credentials. Beacon HTTP requests temporarily revert thread impersonation;
  inherited process credentials require separate validation as described above.
- A second `__MAKE_TOKEN__` replaces the active token (the previous one is
  closed) and the result reports the replacement.
- `__REV2SELF__` closes the token and returns spawned commands to the
  agent identity. It is idempotent: reverting without an active token
  answers `no token active` as a success.

Result contract (used by the server to detect success):

- `token created: <identity>; network operations on this thread use this identity`
- `token replaced: <identity> (was: <previous>); …`
- `token reverted: <identity>; spawned commands use the agent identity`
- Failures answer `Make token failed: …` / `Rev2self failed: …`.

Privileges: `CreateProcessAsUser` (Go's `SysProcAttr.Token` path) requires
`SeAssignPrimaryToken`. Before every command launch with an active token, the
agent checks the process token for an enabled `SeAssignPrimaryTokenPrivilege`
(`processCommandTokenPrivilegeHeld`, a variable test seam reusing the
read-only `spawnTokenPrivileges` snapshot). When it is not enabled — the
default for interactive or elevated administrator accounts, which hold
`SeImpersonate` but never `SeAssignPrimaryToken` — the child is created with
plain `CreateProcess` under the agent identity and the output appends a
degraded-identity notice recommending `runas` with explicit credentials for
network commands, so a missing privilege never blocks command execution.
Agents running as a service (LocalSystem) hold the privilege and inherit the
network identity normally.
`applyCommandToken` reports whether it applied the token. Password handling: the beacon command
carries the credentials in memory; server operator audit events record only
the `DOMAIN\user` label. The interactive CLI keeps the entered command,
including its plaintext password, in `~/.rmm_cli_history`. The WebUI displays
and retains the full command in shell history and persisted UI configuration,
as requested by the operator. These histories are intentionally not redacted. Non-Windows builds answer that the network
identity commands require the Windows agent build.

Operator-side behavior (`make_token` / `rev2self` in `server_rmm.py`):
send the command, wait for the result (bounded by `timeout`), and report
the agent output. All credentials are passed as explicit parameters on
every surface — there is no interactive prompt. Surfaces:
`POST /api/v1/sessions/{id}/make_token` and `…/rev2self`, CLI
`make-token <identity> <password>` / `rev2self` subcommands and
`/make_token <domain\user | user> <password>` / `/rev2self` interactive
commands, WebUI `/make_token <domain\user | user> <password>` and
`/rev2self`, MCP `make_token` and `rev2self`. Live Windows behavior
remains to be validated on a real host.

### Identity query (`__UID__`)

The WebUI shows the full registered account and hostname separately.
`sessionAccountLabel` preserves machine, domain, and service prefixes, including
`HOST\Administrator` and `NT AUTHORITY\SYSTEM`. `renderSessionIdentity` renders
separate account and host rows; long accounts wrap rather than losing their
prefix or being ellipsized. `sessionFullLabel` supplies full identity tooltips.
Live and archived views use the same formatting. Reload the WebUI to pick up
this presentation change.

Go agents register the process
account resolved by `processUserIdentity`, including its domain when available,
so it matches the `agent user:` identity from `/uid`. Inherited USERNAME/USER
environment values cannot override a successful Windows process-token lookup.
Environment lookup remains a fallback if the token account cannot be resolved.

`spawnas` creates a new session under the requested account; the parent session
keeps its original identity. `make_token` changes thread network credentials
and does not change the process account shown in the label. Rebuild and redeploy
the agent to get corrected registration metadata: a server/UI update alone
cannot correct the identity reported by an older running agent.

`__UID__` reports the identity the agent runs under, so operators can
verify `make_token` and `rev2self` without guessing:

```text
agent user: LAB\alice
network identity: none (spawned commands use the agent identity)
```

The first line resolves the process token account
(`OpenCurrentProcessToken` → `LookupAccount`), falling back to the
environment. The second line shows the active `make_token` identity or
`none`. Non-Windows builds report the process user and no active network
identity; creating or resetting a network identity returns an unsupported
platform result. `rev2self` reports `ok: false` for agent failure output,
including unsupported platforms, consistently through REST, CLI, MCP, and WebUI. Surfaces: `POST /api/v1/sessions/{id}/uid`, CLI
`uid` and `/uid`, WebUI `/uid`, MCP `session_uid`.

### Lateral movement (`__LATERAL__`)

`__LATERAL__` starts a fresh agent on another Windows host through one of
three execution modules, so the target registers as a new session. The
command carries a JSON body (`{"method","host","session_id","workspace_id","remote_name","stage",
"loader","payload_sha256","timeout_seconds"}`) on a second line, like
`__EXFIL__`. All modules use native Windows APIs: SMB file access, SCM,
COM/DCOM, and the WSMan client library — never `wmic.exe`, `winrs.exe`, or
a remote PowerShell wrapper command beyond the documented receiver.

Execution model (`agent-go/lateral.go`, `lateral_windows.go`):

- `lateralThreadGoroutine` runs every method through `runLateralThread` on a
  pinned worker, applying the active `make_token` identity before SMB and
  native execution calls. It clears impersonation before releasing its thread;
  failed cleanup makes the agent's identity boundary terminal. The calling
  command thread keeps its own token. No local child process is created for
  these execution paths, avoiding token-assigned process creation privileges.
  Without a token, the worker uses the agent's identity.
- `captureLateralToken` duplicates the selected token under the identity lock
  before dispatch; replacing or revoking the command thread's handle does not
  change a queued worker's identity. `runLateralThreadWithCleanup` publishes
  its final result only after `clearLateralWorkerIdentity` and handle cleanup.
  Failed cleanup suppresses success and makes the identity boundary terminal.
- WMI initializes COM security once with dynamic cloaking and applies the
  same policy to its proxy. WinRM establishes this shared policy before
  WSMan can initialize it implicitly. Failed initialization stops the command
  rather than silently using the process identity for WMI authentication —
  except `RPC_E_TOO_LATE` (`0x80010119`) on a memory-hosted source, where the
  PowerShell host's CLR configured process COM security before the agent's
  code ran: the per-proxy `CoSetProxyBlanket` calls still carry the WMI
  calls, so deployments proceed, with the documented caveat that the initial
  remote connect uses the host process security instead of a make_token
  thread token. `wmiSecurityResult` (`agent-go/lateral.go`) owns the
  decision and its host-runnable test pins both branches.
- With an active make_token, WMI uses the ambient cloaked identity. Only a
  forced identity (`-i` / `-u -p`, the `identity` payload field) supplies
  explicit WMI credentials. The current explicit path selects NTLM.
- WMI process creation runs on one persistent, pinned COM worker with temporary
  per-operation impersonation and verified cleanup. This avoids the per-attempt
  COM teardown associated with the observed failure; it does not establish the
  exact Windows failure mechanism. All targets share the serialized worker.
  Its reply timer starts after an unbounded queue submission, so it does not
  bound the complete wait or cancel an executing Windows call.
  See [WMI COM lifetime](wmi-com-lifecycle.md) for architecture, idle network
  behavior, alternatives, test evidence, and acceptance gaps.
- `__WMI_PROBE__ <host>` (`agent-go/wmi_probe.go`, `wmi_probe_windows.go`)
  is the lab diagnostic for that failure, built into the Windows agent and
  served by the generic exec route (no REST/CLI/MCP surface of its own).
  It runs two read-only WMI round trips (`ConnectServer` plus a
  `Win32_ComputerSystem` class fetch — no method execution, nothing written
  on the target) under the active make_token identity, deliberately on the
  ambient cloaked path, then a third through the explicit credentials.
  Evidence discipline: every stage (impersonation, token capture,
  `CoInitializeEx`, the once-per-process `CoInitializeSecurity` report,
  locator creation, `ConnectServer`, blanket configuration, query,
  cleanup) records a UTC timestamp, PID, OS thread ID, raw HRESULT, and
  duration; stages that never executed report `not_run` instead of a
  misleading zero HRESULT, so a denied connect shows no proxy and no
  query. The thread token is snapshotted immediately before and after each
  `ConnectServer` with `TokenId`, `AuthenticationId` (the logon session),
  `ModifiedId` (the change detector an unchanged logon ID would miss),
  type, impersonation level, and the user SID, compared against the
  captured source token and across attempts. The original
  `CoInitializeSecurity` HRESULT and the requested flags are preserved
  separately from the classification: an accepted `RPC_E_TOO_LATE` is
  reported as a foreign initialization the requested settings may never
  have reached, never as applied. Successful connects are observed through
  `CoQueryProxyBlanket` before and after the blanket is applied
  (authentication service, principal, levels, capability flags — no
  credential material). The verdict never names a cause the evidence does
  not support. `proxyImpersonationLevelLabel` decodes COM proxy levels
  separately from token impersonation levels, which use different numeric
  values. COM initialization call counts use atomic access, and failed
  apartment initialization is never balanced with `CoUninitialize`.
  The Windows-only `wmi_probe_windows_test.go` regressions cover proxy/token
  label distinctions, unexecuted queries, and conservative verdicts. A
  reproduced denial reads "second ambient connection denied; cause
  undetermined", lists what the unchanged identity does and
  does not prove, and is followed by the target-side correlation checklist
  (Security 4624/4625: account/domain, authentication package, logon
  type, source address, and on 4625 the Status and SubStatus, with the
  probes' UTC timestamps as window anchors; missing events prove
  neither). The probe leaves the same per-process WMI client state behind
  as an ambient wmiexec — which is the point of running it in the live
  agent. Success of the explicit comparison establishes only that this
  connection succeeded; it does not guarantee later deployments.
- The optional lifecycle modes (`__WMI_PROBE__ <host> held`,
  `__WMI_PROBE__ <host> cycled`, and `__WMI_PROBE__ <host> switch`; the
  parser accepts exactly those words) run their connections inside one
  continuous COM initialization on a single locked OS thread: one
  `CoInitializeEx` and one `CoInitializeSecurity`, complete instrumented
  connect sequences (stages suffixed `@1`/`@2`/`@3`, same
  token/blanket/HRESULT evidence), and a single `CoUninitialize` teardown
  at the end. Held keeps one impersonation across both connects; cycled
  reverts and reapplies a fresh duplicate between them (the default mode's
  identity handling with the held COM lifetime), separating the COM-
  lifecycle suspect from the impersonation-cycling suspect. Switch is the
  A → B → A account-switch diagnostic: A is the active make_token identity,
  B is the process identity (revert with a token snapshot that verifies the
  absence of a thread token), and A2 reapplies the make_token identity from
  a fresh duplicate — three read-only class queries under one continuous
  COM lifetime, each step's proxies released before the identity changes.
  Its readings are bound before the run: B succeeding under the process
  identity is only decipherable through the target events (unintended
  identity reuse — the target still seeing the make_token account — versus
  the process account having more rights than the baseline assumed); B
  authenticating as the process account and being denied followed by A2
  reconnecting supports correct identity switching for this sequence, and
  the verdict never generalizes beyond it. Switch requires an active
  make_token and refuses without one. Switch, held, and cycled all run
  through the generic lifecycle runner (`wmiProbeLifecycleRun`): one
  locked OS thread and one COM initialization held across every step,
  explicit impersonation and reversion scoped to each operation, a token
  snapshot after every revert, and a final `token-after-cleanup`
  verification on every exit path — and the verification is enforced, not
  observed: a failed reversion, a thread token that survives a revert, or
  a leftover token seen before a step aborts the run with the remaining
  steps marked not_run, and a final cleanup failure (revert failure or
  residual thread token) sets the run's cleanup-failed flag — every
  verdict returns "verdict invalid" before reading any HRESULT — and
  raises the agent's identity-boundary fault the way the lateral worker
  does, so a pooled OS thread never returns carrying the impersonation
  silently. COM teardown is balanced: the deferred `CoUninitialize`
  only fires when the explicit success teardown did not. Entry and exit
  follow a consistent ownership contract: `wmiProbeExecute` first
  suspends the caller's incoming thread impersonation through the
  established `suspendServerIdentity` pattern — capture the caller's
  token, revert it, and restore it on every exit path — so the probe
  never destroys the command thread's make_token state, its own revert
  enforcement operates on a verified clean entry, and a failure to
  suspend or to restore raises the identity-boundary fault like the
  server-request path (the report is kept next to the failure notice). In
  particular, `wmiProbeWithCallerIdentity` keeps the boundary pinned,
  checks the restored logon ID, SID, and impersonation level, and calls
  `wmiProbeExecuteBody` only after verifying clean entry. Token-capture
  errors are distinct from token absence; failed captures cannot satisfy
  a lifecycle clean-entry or cleanup check. Windows regression
  `TestWMIProbePreservesCallerIdentity` exercises real local token handling
  with and without an incoming token, on successful and early-error exits;
  `TestWMIProbeTokenCaptureFailureIsNotAbsence` covers invalid-handle reporting.
  In
  the two-account variant, identical SIDs between A and B are normal for
  network-only logons of the same account — the AuthenticationId
  distinguishes the logon sessions, and the target events establish the
  actual outbound accounts. A report rendered twice by a client surface
  (identical PID, OS thread, timestamps, and token identifiers) is the
  same execution displayed twice, not two runs. An unexecuted stage is
  never reported with a result. Failure-cleanup validation is
  part of the protocol: an unreachable host (connect denied, downstream
  stages not_run, cleanup verified), a denied connection (the switch B
  step), and account changes after a failure (the A2 step following the
  denied B) all surface through the same stages. The two-account variant
  (`wmiProbeDual` — in the agent through the `__WMI_PROBE__` JSON body and
  in the standalone binary through its environment variables; carrying
  second-account
  credentials through the raw exec route would put them in the exec
  audit text) runs A → B → A over two distinct network-only identities:
  set `RMM_WMI_PROBE_B_DOMAIN`/`_B_USER`/`_B_PASSWORD` alongside the
  account A variables with `RMM_WMI_PROBE_MODE=switch`; without them the
  B step uses the process identity like the agent mode without its JSON
  body. Its verdict keeps
  both permission outcomes readable and names the presentation question:
  the critical failure is a step presenting the other account's name,
  settled only by the target's authentication events. The two-account
  variant runs in the agent too: `__WMI_PROBE__ <host> switch` accepts an
  optional JSON body (`{"domain","user","password"}`, account B) that
  rides the beacon command like `__MAKE_TOKEN__`'s and is retained in
  operator history. The body is supplied through the dedicated `POST /api/v1/sessions/{id}/wmi-probe`
  route (`b_cred_id` from the workspace credential store or the direct
  `b_*` fields, mutually exclusive, switch mode only), which audits the
  identity and password; every surface reaches it (CLI `wmi-probe`,
  interactive `/wmi_probe`, MCP `wmi_probe`, WebUI `/wmi_probe` with
  credential-ID completion). The standalone binary keeps the same
  contract through `RMM_WMI_PROBE_B_DOMAIN`/`_B_USER`/`_B_PASSWORD` with
  `RMM_WMI_PROBE_MODE=switch`. Verdicts keep the
  evidence discipline: suspects, never proven causes, with fresh-process
  reruns and target-event correlation named for corroboration. The
  standalone probe binary exposes the modes through
  `RMM_WMI_PROBE_MODE=held`, `=cycled`, and `=switch` (switch gains the
  dual contract with the `RMM_WMI_PROBE_B_*` variables). The
  production WMI path keeps its per-operation lifecycle until these
  validations pass; until then the explicit-credential path remains the
  supported route for repeated WMI deployments and the lifecycle change
  stays an unapplied direction. The target-evidence checklist includes
  `RecordId` and the round-trip timestamp form: Security events can carry
  subsecond precision that CSV formatting can discard, and `RecordId`
  establishes event order within the log without identifying which probe
  call generated an event — call-to-event assignment stays inferred from
  timestamps and source addresses.
- The default mode also surfaces the account-switch case: the agent
  remembers the previous default-mode run's ambient identity summary
  (identifier fields and HRESULTs only — never credentials,
  `wmiProbeSeen`), and when two consecutive runs in one process carry
  different identities it prints an identity-switch observation naming
  both identities and their first-connect results. Successful reuse of an
  authenticated connection never establishes that switching between
  accounts behaves correctly, so the observation names the target-event
  correlation as the arbiter. This is the read-only validator for the
  deployment-relevant switch flow: make_token A → probe → rev2self or
  make_token B → probe, read what the poisoned process presents for the
  second account. Pinned by
  `TestWMIProbeIdentitySwitchObservationStoresAndCompares`.
- Loader deployments (`wmiexec_loader`, `winrm_loader`, `winrm_https_loader`,
  `winrm_https_insecure_loader` wire methods) fetch the masked,
  digest-verified agent artifact from the listener's staging endpoint and
  stream it through the target's Windows PowerShell; the source session's
  own binary is never deployed. Because the artifact no longer comes from
  `os.Executable()`, a memory-hosted source can deploy through them; the
  legacy methods keep refusing from an in-memory session. Only the `csharp`
  loader is supported; `powershell` is a reserved value that every surface
  rejects explicitly. The loader wire names are unknown to older agents,
  which reject the whole command instead of ignoring the new fields.
- SMB staging is now legacy `wmiexec` only: the worker copies the source
  executable to `\\<host>\ADMIN$\System32\<stem>-<random>.exe`. `remote_name`
  supplies the filename stem (default `minimal-rmm-agent.exe`); each
  deployment adds a fresh 128-bit random value encoded as 32 lowercase
  hexadecimal characters. The name must be a simple `.exe` filename because
  it becomes part of the service command line. The loader chain for `wmiexec`
  replaces this with an in-memory receiver over an SMB named pipe: nothing
  is written to the target's disk, and the deployed agent becomes
  memory-hosted.
- WinRM streams the deployment artifact through the authenticated WSMan
  session to a PowerShell receiver, verifies the length and SHA-256 of both
  the in-band loader and the artifact, and loads the agent **in memory**
  inside the receiver: no SMB share, no separate HTTP download on the target,
  and no agent executable written to the target's disk. `Add-Type` can create
  compiler scratch files in `%TEMP%`. The receiver keeps the shell open for
  the agent's lifetime. See [WinRM transfer](winrm-transfer.md) for failure
  handling, lifetime, and validation.
- The target copy starts with `--lateral-session <id>`, a fresh beacon-safe ID reserved by the server before dispatch.
  The source workspace is passed as `--workspace-id <id>` (the internal `-`
  sentinel explicitly clears an embedded owner);
  `parseLateralStartup` validates these startup arguments. A service process inherits no
  operator environment, so the staged binary must carry **embedded
  connection settings** (build it with the server's `use_server_config`);
  the session ID and workspace binding are passed on the command line.
  The WinRM in-memory receiver instead sets `RMM_SESSION_ID` and
  `RMM_AGENT_WORKSPACE_ID` environment variables in the host process before
  starting the agent thread, plus the `RMM_IN_MEMORY` capability marker.
- The agent is service-aware (`service_windows.go`): when the SCM starts
  it, `StartServiceCtrlDispatcher` connects, the beacon loop runs as the
  service body, and `SERVICE_CONTROL_STOP` stops it cleanly. Normal
  interactive starts are unaffected.

Modules:

WinRM accepts `-s` / `--https` in the CLI and WebUI shell, and both
wmiexec and winrm accept `-l` / `--loader csharp|powershell` (`powershell`
is a reserved value that fails explicitly). WinRM loader deployments are
independent by default; `--tethered` restores the held-shell lifetime:

```text
/lateral winrm -s 10.254.100.32
/lateral winrm 10.254.100.32 -s -k
/lateral winrm 10.254.100.32 --tethered
/lateral wmiexec 10.254.100.32 -l csharp
```

The standalone CLI uses `lateral winrm <host> -s --session <source-id>`.
For `lateral` only, `-s` now selects HTTPS; use the existing long `--session`
option for source selection. Other commands retain their `-s` session alias.
REST `POST /sessions/{id}/lateral`, `RmmApiClient.lateral_move`, MCP, and the
AI tool expose `https: true`. It defaults to false and is valid only for WinRM;
the server rejects other methods and nonboolean values before dispatch.
HTTPS commands use the internal `winrm_https` wire method, normalized by the
new agent to WinRM with HTTPS enabled. Older agents reject that method instead
of ignoring a new field and silently connecting over HTTP. Rebuild the source
agent to use `-s`; the external API method remains `winrm`.

The private command's `https` boolean reaches
`winrmCreateProcessWithTransportAPI`, which selects HTTPS/5986 instead of
HTTP/5985 and brackets IPv6 literals. It does not change the selected account.
HTTPS process identity explicitly selects Negotiate with implicit credentials;
explicit credentials still select Kerberos for DNS or Negotiate for IP targets.
There is no HTTP fallback after an HTTPS failure.

HTTPS can provide server authentication for NTLM without TrustedHosts when
the target has a WinRM HTTPS listener and its server certificate is trusted,
unexpired, and valid for the supplied DNS name or IP address. For an IP URL,
the certificate must cover that IP. `-s` does not bypass CA, name, or revocation
checks and does not configure the listener or change machine trust settings.
Add `-k` / `--insecure` with `-s` to skip the CA, certificate-name, and
revocation checks for this session. The API/MCP equivalent is `https: true,
insecure: true`. The bypass requires HTTPS and defaults to false. It does
not change Windows trust stores or TrustedHosts. Older agents reject the
distinct internal `winrm_https_insecure` method instead of ignoring the flag.
`winrmCreateProcessWithTLSAPI` applies the three skip options before opening
the shell and stops if an option fails. `TestWSManCertificateBypass` checks
the option values, ordering, failure handling, and rejection without HTTPS.
Operator errors identify the configured certificate mode as `verify` or
`skip-ca-cn-revocation`. This is a partial bypass: native WSMan still enforces
certificate validity dates. An expired or not-yet-valid server certificate can
still fail with error 12175. The documented WSMan API exposes no skip-date
option; renew/rebind the target listener certificate to resolve expiry on this
transport. See Microsoft's [WSMan session options](https://learn.microsoft.com/en-us/windows/win32/api/wsman/ne-wsman-wsmansessionoption)
for the available checks. `TestWSManPartialCertificateBypassPreservesTLSFailure`
injects error 12175 to check error propagation and the certificate-mode label;
it does not exercise TLS or an actual expired certificate.
`TestLateralHTTPSValidation` checks protocol inputs; `TestWSManHTTPSTransport`
checks transport/authentication combinations, and
`TestWSManHTTPSFailureDoesNotDowngrade` checks failure propagation without
retrying through HTTP or changing certificate checks.
`TestWSManHTTPSFailureMatrix` covers failures at each native API stage for
implicit and explicit identities, checking callback cleanup, password-buffer
clearing, and absence of session retries or password disclosure in errors.
The registered MCP tool uses `StrictBool` for HTTPS; its SDK-boundary test in
`test_mcp_result_encoding.py` rejects strings, integers, and null before the
implementation runs. `make check-agent-deployment` includes that test.

| Method | Technique |
|--------|-----------|
| `psexec` | Two-stage RemCom-style deployment: push the dedicated lateral service host to `ADMIN$`, start it as a demand-start service; its linked stage1 worker fetches the masked stage2 agent payload from the listener's staging endpoint, decodes it in memory, installs and starts the agent, then stops. The calling agent waits for successful service completion and removes the temporary service and host binary itself; the server cleanup command is only a retry path when that immediate cleanup stays pending |
| `wmiexec` | Loader chain (default, `wmiexec_loader`): the worker starts a Windows PowerShell receiver through the native COM/DCOM chain (`wmi_windows.go`): `CoInitializeEx`, `CoCreateInstance(WbemLocator)`, `ConnectServer` to `\\host\root\cimv2`, `CoSetProxyBlanket` with packet privacy, then `GetObject("Win32_Process")` → `GetMethod("Create")` → `SpawnInstance` → `Put("CommandLine")` → `ExecMethod`, checking the provider `ReturnValue`. No `wmic.exe`. The source then streams the framed loader and artifact halves over a session-specific named pipe and waits for the `RMM_LOADER_READY` marker; the receiver maps the agent in memory and blocks on its thread. Nothing is written to the target's disk; the created process is independent of the source agent and ends at target shutdown or an explicit kill. The legacy ADMIN$ copy path remains for old servers only |
| `winrm` | Loader chain (default, `winrm_loader` and its HTTPS wire variants): native WSMan client through `WsmSvc.dll`: create an authenticated session and shell, run a Windows PowerShell receiver, stream a framed payload (loader source + agent binary) with `WSManSendShellInput`, verify each half's length and SHA-256, then load the agent **in memory**: one `VirtualAlloc(PAGE_EXECUTE_READWRITE)` image, a full PE32+ amd64 mapping (sections, relocations, imports, TLS), and a detached `CreateThread` on the entry point. The agent executable is not staged on disk; `Add-Type` can create compiler scratch files in `%TEMP%`. Native runtime validation remains outstanding. The tethered opt-in (`--tethered`) instead keeps the shell open for the deployment's lifetime; the in-memory session cannot spawn, spawnas, lateral, load PE plugins, or install persistence (guarded with a clear error, except the loader chain itself, which a memory-hosted source may run). WinRM loader deployments are independent by default: the receiver re-stages the deployment through a local WMI process create so the target escapes the shell job — the source closes the WinRM session right after readiness and the deployment survives it, ending like a WMI loader deployment; `--tethered` (`independent: false`) restores the source-tethered lifetime. Transfer and launch both use WinRM; no SMB, WMI network calls, `winrs.exe`, or `wmic.exe` (the default independent variant uses WMI on the target machine only) |

WMI and explicit-credential WSMan execution select authentication using the target's written
form (`lateralHostIsIP` in `lateral.go`). This selection does not govern SMB
staging or guarantee Kerberos for the whole operation.

- `psexec` and WMI's SMB staging retain Windows negotiation.
  DNS targets may fall back to NTLM when Kerberos is unavailable. Client
  policy, existing connections, and SPN configuration affect negotiation.
- `wmiexec`: DNS names pass the `kerberos:HOST/<host>` authority as an
  allocated BSTR to
  `ConnectServer` and pin `RPC_C_AUTHN_GSS_KERBEROS` in `CoSetProxyBlanket`
  with the target's `HOST/<host>` machine-account SPN as the server
  principal, so the connection and proxy use the same identity; IP targets keep
  the null authority and `RPC_C_AUTHN_WINNT` (NTLM).
- `winrm` with make_token: `WSManCreateSession` receives a `WSMAN_AUTHENTICATION_CREDENTIALS`
  structure with `WSMAN_FLAG_AUTH_KERBEROS` for DNS names and
  `WSMAN_FLAG_AUTH_NEGOTIATE` for IP targets. The native WSMan client
  exposes no direct NTLM flag; IP targets normally negotiate NTLM (Windows
  can support IP SPNs when explicitly configured). With `make_token` active,
  WSMan receives the selected username/password explicitly for both transfer
  and launch. WinRM does not open an SMB connection.

### Forced deployment identity (`identity` payload field)

`__LATERAL__` accepts an optional `identity` JSON object
(`{"domain","user","password"}`) for the wmiexec and winrm methods; psexec
keeps its ambient SCM/SMB staging and rejects it. The operator supplies it on
every surface through a credential-store ID or direct credentials (REST
`identity_cred_id`/`identity_*` body fields, CLI `-i` or `--user`/
`--password`, WebUI `/lateral … -i | -u -p`, MCP `identity_*` parameters),
the same convention as make_token, spawnas, and runas. A combined
`DOMAIN\user` value splits like make_token; audit records record the
`DOMAIN\user` label only.

Agent-side behavior:

- Precedence: the payload identity beats an active make_token for the
  deployment; without it the ambient make_token state remains the fallback
  (captureLateralWorkerIdentity in `lateral_windows.go`).
- The worker logs the identity on as `LOGON32_LOGON_NEW_CREDENTIALS`
  (`captureLateralExplicitIdentity`, no privilege required) and
  impersonates it on its pinned thread, so SMB staging and the loader pipe
  writes authenticate with the credentials.
- The WMI chain *also* receives them explicitly: `wmiConnectIdentity`
  passes `strUser`/`strPassword` BSTRs to `ConnectServer` with an
  `ntlmdomain:` authority (the domain never rides inside `strUser`, per
  the ConnectServer contract) and hands a `SEC_WINNT_AUTH_IDENTITY_W`
  structure to `CoSetProxyBlanket` instead of dynamic cloaking. The DCOM
  handshake therefore no longer depends on thread cloaking, which keeps
  WMI loader deployments working from a memory-hosted source whose
  process COM security the PowerShell host already owns. A forced identity
  always authenticates through NTLM, matching the network-only semantics
  of the logon type; DNS targets without a forced identity keep the
  Kerberos authority.
- The winrm chain passes the identity to `WSManCreateSession` exactly like
  the make_token snapshot it replaces.

Without make_token over HTTP, `WSManCreateSession` receives a NULL authentication
pointer for Default authentication. Over HTTPS it receives a Negotiate structure
with null account fields, together with the implicit-Negotiate option below.
The clean worker has no impersonation token and therefore uses the process
identity. Negotiate can choose NTLM or Kerberos; DNS
does not enforce a Kerberos-only policy. Existing WinRM host-trust policy and
the logon session's available credentials still determine authentication.
`TestWSManDefaultProcessIdentity` checks the NULL argument for HTTP DNS/IP URLs;
`TestLateralThreadDefaultProcessIdentity` checks the clean worker identity.
Shell failures include the configured transport and authentication mode, such
as `https; auth=implicit-negotiate`. This identifies the selected code path;
it does not establish that a TLS handshake or authentication succeeded.

For HTTPS Negotiate with implicit credentials, the agent sets the session-scoped
`WSMAN_OPTION_ALLOW_NEGOTIATE_IMPLICIT_CREDENTIALS` DWORD to 1 before
opening a shell. Failure closes the session and stops execution. Certificate
validation remains enabled unless `-k` is selected. Machine trust stores and
TrustedHosts are unchanged. The fixture
checks the option's ABI, ordering, and failure cleanup, and the local native
session test checks that the DLL accepts it.

WSMan endpoint URLs bracket IPv6 literals using `net.JoinHostPort`. The
native-call fixture checks exact URLs and authentication flags against
independent expected values for DNS names, IPv4, and IPv6.

The explicit Kerberos paths require a valid server principal and usable
Kerberos credentials. Workgroup/local-account access is not established by
switching to an IP address alone. WMI rejects an authority on local
connections, so the local Windows CI WMI test uses the loopback IP.

The native WMI binding uses the Windows SDK `IWbemClassObject` vtable slots:
`GetMethod=19`, `SpawnInstance=15`, `Put=5`, and `Get=4` (including
`IUnknown`). Older agents can fail at method lookup with `0x80041008`
because they dispatch to `GetObjectText` instead. Rebuild and replace the
source agent to apply the correction. Native Windows CI run `42381057961`
passed on Go 1.21 and 1.27 in normal and debug modes. The COM call chain stays on one OS thread so initialization
and cleanup share the same apartment. `TestWMICreateProcess` exercises local
process creation and a missing executable's provider error on Windows CI.

`wmiAuthentication` prepares the authentication service, an owned authority
BSTR, and a UTF-16 proxy principal. Its caller frees the BSTR after the COM
connection and proxies are released. `TestWMIAuthentication` checks the BSTR
with the native `SysStringLen` API, verifies matching `HOST/<host>` identities,
checks the NTLM path for IPv4/IPv6, and rejects embedded NULs. Windows CI
includes this test in both normal and debug builds.

The native `WSManCreateSession` binding supplies all six API arguments: an
authentication-credentials structure selecting the mechanism (see the
authentication paragraph above) and a null proxy pointer before the session
output pointer. Selected make_token credentials populate the account fields;
HTTP default identity uses a NULL authentication pointer; HTTPS process identity
selects Negotiate with null account fields and enables implicit credentials.
Agents built before this correction can fail locally with `0x803380ba`
(`ERROR_WSMAN_CLIENT_CREATESESSION_NULL_PARAM`); rebuild and replace the
source agent to pick up the corrected binding.

The remaining shell bindings use SDK argument ordering, counted
`WSMAN_COMMAND_ARG_SET` arguments, and pointers to `WSMAN_SHELL_ASYNC`.
Shell operations return `void`; their callbacks carry operation errors.
`WSManReceiveShellOutput` requests all streams and can deliver multiple output
callbacks before command completion. A binary output block can also carry the
Done state; success requires Done with exit code zero.

WinRM binding internals (`wsman_windows.go`):

- `loadWSMan` resolves every required export before initialization;
  `wsmanNative.call` implements `wsmanCaller` for native dispatch.
  `winrmCreateProcessWithAPI` runs the existing command flow through that
  boundary, allowing fixtures to validate calls without remote execution.
- `wsmanShellAsync`, `wsmanStringSet`, `wsmanData`, and `wsmanReceiveData`
  mirror the SDK's 64-bit structures, including union alignment.
- `wsmanScope.keep`, `pendingCall`, and `release` retain inputs and registered
  `wsmanPending` callback contexts through synchronous session/API teardown.
  Context IDs avoid exposing Go object addresses as native callback context.
- `wsmanCompletion` copies errors, command state, and exit codes while native
  buffers remain valid. `wsmanResult.wait` and `snapshot` synchronize callback
  completion and copied values; stream output alone does not complete a wait.
  `readWSManString` copies bounded command-state strings inside the callback.
- `wsmanScope.closeHandle` supplies and waits for mandatory close callbacks.
  Cleanup closes the receive operation, command, shell, session, and API in
  that order. Local waits are bounded; contexts and inputs remain alive until
  synchronous shutdown drains any delayed or canceled callbacks.

Windows CI runs `TestWSMan*` in normal/debug modes: native callback trampoline
tests, SDK layout checks, synthetic success/error/timeout/late-callback flows,
and real DLL/session creation without opening a shell. The independent
`testdata/wsman_layout.c` assertions compile against the Windows SDK using
MSVC's C++ parser, which supports the SDK's typed enums; the same fixture
also compiles as C with MinGW. These
checks do not establish remote authentication, staging, or detach behavior;
those still require an authorized Windows lab.

`wsmanError` mirrors the native `WSMAN_ERROR` layout. Failed callbacks copy
the bounded fault detail into the operator result before native memory expires;
null details retain the numeric code alone. `TestWSManCallbackCopiesFaultDetail`
checks this lifetime boundary. Error 1312 indicates an unavailable logon session
and does not by itself prove invalid target credentials or target rejection.
`readWSManStringBounded` caps fault details at 4096 UTF-16 code units while
preserving the shorter bound for command-state strings.

`captureLateralIdentity` snapshots token and credentials under one lock.
`winrmCredentials` and `lateralPayload.winrmIdentity` are local-only and are
excluded from JSON. `winrmCreateProcessWithIdentityAPI` validates and roots
the native account buffers until session teardown, then clears the password
buffer. Worker completion clears the snapshot's string references. Explicit
credentials retain Kerberos/Negotiate selection and existing host-trust checks.
`TestWSManExplicitIdentity` covers credential arguments, empty/Unicode passwords,
failure paths, and password-buffer cleanup without remote execution.

Before accepting remote authentication changes, validate DNS/Kerberos and
IP/NTLM connections separately with the agent identity and an alternate
identity. Confirm the negotiated protocol and account in Windows security
events, and confirm authentication fails for invalid credentials and an
unregistered Kerberos principal. Local session-handle creation is only an ABI
check and does not prove authentication or identity selection succeeded.

The WebUI renders the agent result through session events once, while the
HTTP response supplies registration status. Waiting messages and unconfirmed
registration warnings remain visible.

Result contract: an acknowledged launch answers
`lateral started: method=<method> host=<host> target session=<id>`; the
psexec handshake also carries `service=<name> binary=<name>` so cleanup
can remove exactly this deployment. An acknowledgment
does not prove deployment; failures answer `Lateral failed: <detail>`
with the explanation from the failing step. The server
(`lateral_move` in `server_rmm.py`) waits for the target session to
register under the source workspace and reports `registered` and the legacy
`verified` alias. No response probes are sent. Surfaces:
`POST /api/v1/sessions/{id}/lateral`, CLI `lateral` and `/lateral`,
WebUI `/lateral`, MCP `lateral_move`. Requirements and caveats: Go agent
Windows build with embedded connection settings, admin rights on the
target (or a `make_token` identity with them). WMI and PsExec require `ADMIN$`
for staging. WinRM requires Windows PowerShell on the target and WinRM on
port 5985 or HTTPS/5986 with `-s`; it does not require SMB. Live
behavior of every module remains to be validated on real hosts.

`exec_timeout` bounds the server response wait. For PsExec, the agent's worker
budget subtracts the source beacon sleep plus maximum jitter and 15 seconds,
capped at half the total timeout, to reserve delivery and response time. WMI and
WinRM receive the unchanged timeout, including fractional seconds. Delayed or
offline agents can still exceed the server wait. An HTTP timeout does not cancel
queued or running target work: the Go deadline ends the operator's wait, not
blocking SMB, SCM, or COM calls. Because a worker can still be progressing after
a timeout, the agent keeps a per-host in-flight claim; an immediate retry to the
same host fails with a busy error until the first worker fully completes, so one
timeout cannot stack two deployments. WinRM applies one transfer-wide deadline to
session setup, every input packet, and the loader readiness wait, instead of a
fresh per-operation budget for each step.

Workspace `operator_proxy_timeout` uses the requested command wait plus a
30-second margin for identity actions, and the execution wait plus registration
and two verification waits plus 30 seconds for spawn/lateral. Registration and
verification budgets are omitted when `verify` is false. Invalid budget values
use bounded defaults in the proxy; the central API still validates the request.

### PsExec method: dedicated lateral service and listener staging

The `psexec` module is a two-stage reimplementation of the RemCom
deployment model ([RemCom source](https://github.com/kavika13/RemCom) as
architectural reference, not a dependency): RemCom embeds a small
service binary inside its client and extracts it at runtime; here the
agent embeds the dedicated lateral service host and pushes it instead of
the full agent binary.

Dedicated lateral service (`agent-go/lateral-service/c/service.c`):

- The normal stage1 launcher has no SCM code. The dedicated host compiles its
  staging worker code with `STAGE1_EMBEDDED`, which excludes standalone WinMain.
  It owns its own SCM dispatcher and runs the linked stage1 worker on a native
  CreateThread thread. There is no second stage1 executable or custom PE loader.
- `stageServiceMain` registers the actual SCM service name, reports startup and
  running state, waits for `stageWorker`, and reports STOPPED with its result.
  `reportServiceState` and `stageServiceControl` publish SCM status. The worker
  invokes the shared `runLauncher` function. SCM controls cannot stop the worker
  midway through staging.
- The builder compiles this host separately and embeds it as
  `lateral-service.bin`, including in stageless agents. The host has no baked
  listener configuration; the calling agent passes its URL, the staging
  reference, and the target session ID in the service command line. No beacon
  secret is passed. The target-architecture MinGW compiler is required for this
  component. Without it the embedded placeholder fails closed; the normal Go
  stage1 fallback is never installed as a service and a stale host is not reused.
- Only the dedicated service host is copied to ADMIN$ for PsExec. Stage1 fetches
  stage2 over HTTP, decodes it, writes it to the service account's temp directory,
  launches the agent, and exits. The full agent is not copied over SMB on this
  path. WMI and WinRM retain their separate full-agent deployment paths.
- Stage1 creates a distinct payload file for each target session:
  `minimal-rmm-agent-<session_id>.exe` (or
  `minimal-rmm-agent-debug-<session_id>.exe` for the C debug variant). Existing
  files are never overwritten, so repeated lateral deployments do not collide
  with the executable of an agent already running on the same target.
- PsExec retains the original SCM connection and service handle through startup,
  completion polling, and deletion. `scmStartServiceRetained` transfers ownership
  to `scmServiceHandles`; its `close` method releases the service before the
  manager, and its `delete` method attempts to close both before binary removal.
  Close failures remain errors and retain failed handles for another close
  attempt; already closed handles are never closed twice. The Win32 adapters
  `closeSCMHandle` and `deleteSCMService` preserve native errors and debug
  checkpoints. Ownership tests run without Windows or a remote connection.
  `waitStage1ServiceStopped` polls that existing handle. After a confirmed stop
  (including worker failure), `lateralCleanupStopped` removes artifacts without
  another status check or SCM connection. Timeouts and query failures leave
  artifacts for the separate ownership-checked cleanup retry.
- Failure paths never leave untracked artifacts. A service created by
  `scmStartServiceRetained` that fails to start is marked for deletion on the
  retained handle before the error close, and `lateralPsexecMove` removes the
  staged launcher binary from ADMIN$ when SCM creation or startup fails — those
  failures occur before the acknowledged cleanup ownership record exists. WMI
  removes its copied executable when `Win32_Process.Create` fails.
- This avoids three redundant SCM connection attempts after startup. In an
  observed trace, each consumed about 21 seconds. The initial connection may
  still incur Windows RPC/TCP fallback latency; this change does not alter
  transport selection or machine-wide RPC settings.

Listener staging (`server_rmm.py`):

- `lateral_move(method=psexec)` stages the newest raw Windows agent
  build belonging to the source session's workspace
  (`latest_go_agent_binary`, produced by `build_go_agent`, which records
  each raw binary's workspace owner in a `{build_id}.json` sidecar) and
  passes the staging reference in the `__LATERAL__` command. Without a
  workspace-owned staged build the call fails with
  `agent_payload_unavailable` (or `debug_payload_unavailable` when the
  source session runs a debug build): artifacts owned by another
  workspace or builds without an ownership record are never selected, so
  the staged payload cannot make the target register under a foreign
  embedded owner.
- The staged launcher's service command line carries `--workspace-id`:
  stage1 (Go and C) forwards the requesting workspace on the target
  launch, so a PsExec deployment registers under the same explicit
  binding as WMI and WinRM instead of depending on the artifact's
  embedded workspace.
- Staging entries are in-memory and single-use. Lateral-move staging
  expires after 600 seconds; staged ZIP builds
  (`build_go_agent`, see `docs/staged-deployment.md`) use the 24-hour
  build TTL (`STAGING_BUILD_TTL_SECONDS`) because manual deployment
  takes longer than the psexec flow. `GET /tools/stage/<id>` serves the
  payload masked with a
  SHA-256 counter keystream keyed by the staging identifier
  (`staging_mask`), so the listener never serves an executable file.
  The psexec launcher returns its result through SCM/RPC without a separate
  HTTP status request. The legacy `POST /tools/stage/<id>` endpoint remains
  available for older launchers and the standalone Go fallback to report
  `stage_status`.
- The routes are capability-authenticated by the 128-bit staging id and
  allowed on the beacon listener without a beacon token; expired or
  already-fetched entries answer 404.

Cleanup is owned by the calling agent. After starting the service it uses
`waitStage1Stopped` and `launcherServiceOutcome` to observe SCM worker
completion, then calls `lateralCleanup` to delete the temporary service
and its exact binary. `removeStoppedLauncher` retries sharing/access errors
briefly while Windows releases image handles. It does not wait for a server
beacon round trip, run a shell, or delete the new agent executable.

The result reports immediate cleanup success or pending/failure details. The
server recognizes completed source-agent cleanup without queuing another
command. Pending cleanup retains exact-artifact ownership, allowing the existing
`__LATERAL_CLEANUP__` retry even when target registration failed. Arbitrary service
names and paths
remain rejected. Service deletion takes effect after outstanding SCM handles
close. A stopped worker permits cleanup even after a staging failure; running or
unreachable workers block deletion.

## Workspace ownership

`deployment_workspace_id` binds generated Go archives and configured PowerShell
scripts to the authenticated workspace. A workspace caller cannot select another
owner. Administrators can select a registered owner with `workspace_id`; when
multiple workspaces exist, they must select one. CLI `agent deploy-script` and
`agent go-build` accept `--workspace-id`; MCP/AI deployment tools accept the same
optional parameter. WebUI generators bind the active workspace automatically
and expose an owner field for administrators.

The Go linker embeds `defaultWorkspaceIDB64`, launchers set
`RMM_AGENT_WORKSPACE_ID`, and the PowerShell configuration sets `$workspaceId`.
Both agents include `workspace_id` in `/register`. Registration validates known
workspace IDs, assigns ownership before publishing events, and rejects attempts
to change a live or persisted owner. Spawn and lateral requests carry the source
workspace, including when the source was a legacy agent claimed by an operator.

The ID is routing metadata authenticated by the existing beacon transport; it
is not a workspace operator credential. No `RMM_WORKSPACE_TOKEN` is embedded.
Legacy agents without an ID retain persisted ownership, sole-workspace fallback,
or explicit claim behavior. New configured artifacts should be regenerated to
carry the binding. `GET /agent/script` remains a generic source template; use
configured deployment tools or the WebUI generator for a ready-to-run artifact.

The shared router is implemented by `buildCommandInvocation` in
`agent-go/main.go`.

On Windows, `newCommand` in `platform_windows.go` supplies the CMD command
line explicitly as `cmd.exe /d /s /c "<body>"`. The outer quotes delimit the
command tail; quotes inside the operator's command are preserved literally.
This bypasses Go's default Windows argument escaping, whose backslash-escaped
quotes are incompatible with CMD. `TestWindowsCMDQuotedCommands` exercises
quoted arguments, a script path containing spaces, and command operators on
Windows without changing system state.

Before CMD dispatch, `normalizeCMDQuotes` converts complete single-quoted
arguments such as `'two words'` to `"two words"`. This restores the Go agent's
missing convenience behavior already offered by the PowerShell agent. The Go
normalizer preserves apostrophes within words, existing double-quoted text,
unmatched quotes, partial arguments, and segments containing double quotes,
carets, or newlines. CMD expansion rules still apply to converted arguments.
`PS:`, `powershell:`, `pwsh:`, and non-Windows test adapters bypass normalization.

`TestNormalizeCMDQuotes` and `TestBuildCommandInvocationQuoteNormalization` are
host-runnable unit regressions. Run `make check-agent-commands` for focused
validation; both `make check` and `make go-test` include them. The Windows-only
`TestWindowsCMDQuotedCommands` also exercises single-quoted input through the
dispatch layer and actual CMD process in the dedicated Windows CI step.

| Input | Windows behavior |
|-------|------------------|
| Bare command | Runs with `cmd.exe /d /c`; an exact `ls` is normalized to `dir` |
| `cmd: <command>` | Runs with `cmd.exe /d /c`; an exact `ls` is normalized to `dir` |
| `PS:` / `powershell:` | Runs with `powershell.exe -NoProfile -NonInteractive -Command` |
| `pwsh:` | Uses PowerShell 7 when `pwsh.exe` is available, otherwise falls back to `powershell.exe` |

Prefix matching is case-insensitive and accepts optional whitespace before and
after the colon. A prefix must be followed by a command body, for example
`pwsh: Get-Date` or `PS:Get-Date`.

## Feature parity

The Go agent now implements chunked file transfer, HTTP proxy support, Windows
screenshot capture, Windows keylogging, Windows startup/Run-key persistence,
rclone exfiltration when `rclone` is already available on the target, and
isolated PE plugin loading (`__PE_LOAD__` and plugin-owned
`__PLUGIN_COMMAND__`, see
`docs/go-agent-plugins.md`). SOCKS5 is a persistent pivot plugin on Windows
amd64: the beacon observes `socks_active`, then starts the deployed `socks.dll`
plugin in a cancellable child process. Build that plugin in the external plugin
repository and install it in the runtime plugin store before starting a pivot.
The RMM-owned TUN frontend is a separate persistent plugin: the beacon
observes `tun_active`, then starts `tun.dll` in another cancellable child
process. Build the required plugin in the external plugin repository and
install `socks.dll` or `tun.dll` before starting the corresponding
pivot. The TUN device and user-space IP stack stay in the namespace selected by
the storage mode; the Go agent opens ordinary target-side TCP and UDP sockets
and does not need a TUN device or administrator privileges. See
`docs/tun-pivot.md`.
If rclone is absent, the agent bootstraps it through the authenticated
`/tools/rclone.exe` endpoint. Non-Windows adapters are retained only for
host-side development tests.

The Windows-specific adapters are isolated in `platform_windows.go` and are
selected by Go build tags. They should be tested on representative Windows
versions before production use.

Windows builds use the GUI subsystem, so launching the executable does not
open a console window. Child command windows are also hidden, including
PowerShell commands. Screenshot capture uses native Windows GDI APIs and does
not invoke PowerShell. Startup and connection errors are written to
`%TEMP%\minimal-rmm-agent.log`; set `RMM_LOG_FILE` to choose another path.
Plugin lifecycle events are logged by the RMM server and are never written to
this target-side log.

## Shutdown

The beacon HTTP client rejects redirects so `X-RMM-Beacon-Token` and request
bodies stay on the configured server endpoint. Configure the final beacon URL,
including HTTPS when required; an HTTP-to-HTTPS redirect is not followed.

The beacon handles SIGINT/SIGTERM through a cancellation context. Shutdown
cancels in-flight HTTP requests, beacon sleeps, directly managed command
processes, and plugin children before closing idle HTTP connections and the
agent log. `closeAgentLog` serializes closure against log writers;
`stopPlatformWorkers` stops and waits for the Windows background worker.
Child-command pipe waits are bounded after cancellation or process exit.

SIGKILL and Windows forced process termination cannot run these defers. There
is no Windows Job Object containment in this implementation: a forcibly killed
parent or grandchildren created by arbitrary commands may leave processes
behind. Windows GUI-subsystem shutdown behavior still needs native testing.
The host regression suite exercises cancellation with simulated beacons.
Windows cross-builds check compilation; native Windows service and process
lifetime behavior still require a Windows lab.

### Deployment identity and local workers

The server reserves random 32-character lowercase hex IDs before dispatch for
`spawn`, `spawnas`, and `lateral`, matching generated Go agents. IDs contain no
command name or parent identity. Registration still proves deployment
independently of a lost launch acknowledgment. The Go `derivedSessionID`
fallback uses 16 cryptographically random bytes in the same hex format for
legacy commands. Existing sessions keep their original IDs.

Lateral execution uses a pinned worker thread for PsExec, WMI, and WinRM.
Older builds used token-assigned local child processes for WMI and WinRM,
which could fail with Windows error 1314 before contacting the target.
Rebuild and replace the source agent to use the thread-based execution path.
Legacy child entry points and the separate cleanup-retry child remain for
compatibility; this change covers lateral execution, not arbitrary subprocesses.

The command deadline stops waiting but cannot terminate a blocking native
operation. A timed-out worker can continue staging or launching remotely;
check target registration before retrying. Server requests retain the existing
capture/revert/restore boundary for their actual calling thread.
`TestLateralThreadIdentity` covers worker logon identity and server-request
restoration; `TestLateralThreadDeadline` covers an operation outliving its wait.
`TestLateralThreadTokenSnapshot` verifies token-handle ownership across
revocation. `TestLateralThreadCleanupFailure` checks final-result ordering and
failure propagation when worker identity cleanup fails.

### Cleanup ownership and outcomes

`rememberLateralCleanup` records the exact host, service name, and launcher
basename returned by an acknowledged psexec request in the source agent's
memory. It requires the requested target session and basename and a matching
128-bit deployment suffix. `ownsLateralCleanup` requires an exact record before
`lateralCleanupCommand` starts a cleanup child. Syntactically valid arbitrary
names do not grant deletion authority. This guards operator requests; it is
not a security boundary against someone already able to execute arbitrary
code under the source agent's identity.

Records are removed only after confirmed cleanup success. Failures retain
the record for a retry. A source restart or an unacknowledged launch leaves
no cleanup authority: automatic cleanup refuses, and an operator must inspect
the remaining resources manually. Deployment records do not detect replacement
of remote resources by another administrator after deployment.

`serviceLookupOutcome` recognizes only Windows error 1060 as an absent service;
permission and transport failures remain errors. `lateralCleanupOutcome`
preserves service and file errors, including partial deletion, while allowing
an already missing file. The source and server require the positive
`lateral cleanup:` prefix followed by a space to report success; empty, failed, or unexpected
responses report failure. A successful deletion request does not prove that
Windows has finished removing a service marked for deletion.

### Build artifact isolation

Each build uses a unique server-side binary path and ZIP filename. The executable
name inside the archive remains stable for its launcher. Concurrent builds for
the same target cannot overwrite one another's artifacts or change previously
returned artifact references.

`build_go_agent` copies source inputs into a disposable directory beneath the
build-artifact directory. `_build_go_agent_from_source` compiles and packages
that snapshot, including its generated lateral-service.bin. Existing generated
binary files are excluded from the copy. The checkout stays unchanged, and
parallel builds need no shared-embed lock. Scratch sources are removed on success,
compiler failure, or timeout; downloadable artifacts remain available.
Only the disposable build root is made writable, so a read-only source directory
retains its permissions. The build directory must be outside the source tree;
the builder rejects a nested path before creating files. Forced process or host
termination can leave scratch directories behind, but does not modify tracked
source files.

Workspace services forward `agent-builds` and `agent-deploy` artifact downloads
to the authenticated central server, which owns these generated deployment
files. Downloaded session data remains workspace-local. Workspace Go build
requests use a 300-second proxy timeout to accommodate the compiler's
120-second limit and archive generation.

## Agent process identity

The Go agent's `register()` reports its own `os.Getpid()` as the optional `pid`
registration parameter, and the basename returned by `os.Executable()` as
`process_name`. This reports the executable actually running the agent, including
renamed binaries, rather than a presumed build filename or caller-supplied
argument. Rebuild and redeploy existing Go agents to report these values.
This identifies the running agent process, including agents launched by stage1;
the launcher process ID is separate.

The server's `Session` metadata exposes `agent_pid` as a positive 32-bit integer
or `null`. `register_session()` validates the value and refreshes it on each
registration; omitted or invalid values clear it without rejecting older agents.
PID and process-name changes are saved with the session and flow into workspace
archives. `normalize_agent_process_name()` accepts a nonempty basename of at
most 255 characters without path separators or ASCII control characters; invalid
or omitted names become `null`. `Session.agent_process_name` is included in
session metadata. `_session_from_record()` restores both process fields when
archiving persisted sessions after a restart.

The WebUI's `sessionPidLabel()` validates PID metadata. `sessionProcessLabel()`
combines the executable basename and PID, showing either independently when only
one is known. Live and archived cards and console headers display this label;
process names are escaped as text. `sessionLiveDetail()` formats the live header,
which refreshes when session updates arrive. Unknown process fields have no
label. Archived values describe the process at the time the session was saved.
Stage1 is a separate launcher and needs no change to report the Go agent's name.

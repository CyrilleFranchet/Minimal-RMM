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
  children through Go's `SysProcAttr.Token` (`CreateProcessAsUser`). Standard
  spawn does not use this helper; spawnas and runas use explicit credentials.
  The cloned token retains the local identity and supplies different outbound
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

Privileges: `CreateProcessAsUser` may require `SeAssignPrimaryToken` when
the token is not a clone of the caller's own token; a privilege failure is
reported verbatim to the operator. Password handling: the beacon command
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

`__LATERAL__` starts a copy of the agent on another Windows host through
one of three execution modules, so the target registers as a new session.
The command carries a JSON body (`{"method","host","session_id","workspace_id","remote_name","stage",
"timeout_seconds"}`) on a second line, like `__EXFIL__`. All modules use
native Windows APIs: SMB file access, SCM, COM/DCOM, and the WSMan client
library — never `wmic.exe`, `winrs.exe`, or a PowerShell wrapper.

Execution model (`agent-go/lateral.go`, `lateral_windows.go`):

- The parent runs the whole move in a bounded `--lateral-child` process
  (`newBoundedCommand`, default 300 s) so a stuck remote operation can
  never block the synchronous command loop. The child inherits the
  active `make_token` identity through `applyCommandToken`, so SMB, SCM,
  WMI, and WinRM authentication use the alternate credentials; without a
  token the move uses the agent's own identity and requires local admin
  rights on the target.
- Staging (`wmiexec`, `winrm`): the child copies its own executable to
  `\\<host>\ADMIN$\System32\<stem>-<random>.exe`. `remote_name` supplies
  the filename stem (default `minimal-rmm-agent.exe`); each deployment adds
  a fresh 128-bit random value encoded as 32 lowercase hexadecimal characters.
  The name must be a simple `.exe` filename because it becomes part of the
  service command line.
- The target copy starts with `--lateral-session <id>`, a fresh beacon-safe ID reserved by the server before dispatch.
  The source workspace is passed as `--workspace-id <id>` (the internal `-`
  sentinel explicitly clears an embedded owner);
  `parseLateralStartup` validates these startup arguments. A service process inherits no
  operator environment, so the staged binary must carry **embedded
  connection settings** (build it with the server's `use_server_config`);
  the session ID and workspace binding are passed on the command line.
- The agent is service-aware (`service_windows.go`): when the SCM starts
  it, `StartServiceCtrlDispatcher` connects, the beacon loop runs as the
  service body, and `SERVICE_CONTROL_STOP` stops it cleanly. Normal
  interactive starts are unaffected.

Modules:

| Method | Technique |
|--------|-----------|
| `psexec` | Two-stage RemCom-style deployment: push the dedicated lateral service host to `ADMIN$`, start it as a demand-start service; its linked stage1 worker fetches the masked stage2 agent payload from the listener's staging endpoint, decodes it in memory, installs and starts the agent, then stops. The calling agent waits for successful service completion and removes the temporary service and host binary itself; the server cleanup command is only a retry path when that immediate cleanup stays pending |
| `wmiexec` | Native COM/DCOM chain (`wmi_windows.go`): `CoInitializeEx`, `CoCreateInstance(WbemLocator)`, `ConnectServer` to `\\host\root\cimv2`, `CoSetProxyBlanket` with packet privacy, then `GetObject("Win32_Process")` → `GetMethod("Create")` → `SpawnInstance` → `Put("CommandLine")` → `ExecMethod`, checking the provider `ReturnValue`. No `wmic.exe`, no PowerShell |
| `winrm` | Native WSMan client (`wsman_windows.go`) through `WsmSvc.dll` — the library `winrs.exe` itself uses: `WSManInitialize`, `WSManCreateSession`, `WSManCreateShell`, `WSManRunShellCommand`, receive until the command's Done state, then close. The remote command runs the agent with `--lateral-detach`, which re-registers itself as a local SCM service and exits: WinRM terminates a shell's process tree when the shell closes, while the SCM-started instance runs outside that job object. No `winrs.exe`, no `wmic.exe` |

The native WMI binding uses the Windows SDK `IWbemClassObject` vtable slots:
`GetMethod=19`, `SpawnInstance=15`, `Put=5`, and `Get=4` (including
`IUnknown`). Older agents can fail at method lookup with `0x80041008`
because they dispatch to `GetObjectText` instead. Rebuild and replace the
source agent to apply the correction. Native Windows CI run `42381057961`
passed on Go 1.21 and 1.27 in normal and debug modes. The COM call chain stays on one OS thread so initialization
and cleanup share the same apartment. `TestWMICreateProcess` exercises local
process creation and a missing executable's provider error on Windows CI.

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
target (or a `make_token` identity with them), `ADMIN$` reachable for
staging, and WinRM over HTTP on port 5985 for the `winrm` module. Live
behavior of every module remains to be validated on real hosts.

`exec_timeout` bounds the server response wait. For PsExec, the agent's worker
budget subtracts the source beacon sleep plus maximum jitter and 15 seconds,
capped at half the total timeout, to reserve delivery and response time. WMI and
WinRM receive the unchanged timeout, including fractional seconds. Delayed or
offline agents can still exceed the server wait. An HTTP timeout does not cancel
queued or running target work.

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

Listener staging (`server_rmm.py`):

- `lateral_move(method=psexec)` stages the newest raw Windows agent
  build (`latest_go_agent_binary`, produced by `build_go_agent`) and
  passes the staging reference in the `__LATERAL__` command. Without a
  staged build the call fails with `agent_payload_unavailable`.
- Staging entries are in-memory and single-use. Lateral-move staging
  expires after 600 seconds; staged ZIP builds
  (`build_go_agent`, see `docs/staged-deployment.md`) use the 24-hour
  build TTL (`STAGING_BUILD_TTL_SECONDS`) because manual deployment
  takes longer than the psexec flow. `GET /tools/stage/<id>` serves the
  payload masked with a
  SHA-256 counter keystream keyed by the staging identifier
  (`staging_mask`), so the listener never serves an executable file.
  `POST /tools/stage/<id>` records one stage1 status line, surfaced as
  `stage_status` in the lateral result.
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

The lateral command starts a local bounded worker process, not a Unix fork.
The Go `fork/exec` error label can also appear on Windows. The worker isolates
blocking remote calls and receives the active command token. Attaching that
token uses Go's `CreateProcessAsUser` path; Windows error 1314 means the local
caller lacks a required process-creation privilege. This failure occurs before
the worker contacts the remote host and does not establish whether remote
authentication would succeed. The current worker/token implementation is
unchanged by the shell feedback fixes.

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

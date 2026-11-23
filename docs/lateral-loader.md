# Lateral loader architecture and review plan

**Status:** implemented except native Windows validation — the
server-sourced loader chain for `wmiexec` and `winrm`, the `loader` surface,
and the per-operation capability checks are implemented; the receiver script,
PE mapping, and live named-pipe/WSMan transfers remain unvalidated on real
Windows targets, so no combination may be claimed as supported until the
validation matrix runs. The `powershell` loader stays reserved and rejected.

**Release decision:** single-loader release with `csharp`; the `powershell`
value is reserved on every surface and rejected with an explicit error until a
later increment implements it.

**Goal:** make lateral `wmiexec` and lateral `winrm` independent of the source
agent's hosting mode (on-disk service/interactive start vs. in-memory
WinRM-loaded), with an operator-selectable loader (only `csharp` in this
release; `powershell` reserved) and no deployment artifacts written to the
target's disk.

**Scope:** `wmiexec` and `winrm` deployments, the artifact-selection path on the
server, and the operator surfaces that expose the new options.
**Out of scope:** PsExec behavior (kept unchanged; its shared assumptions are
reviewed in [§7](#7-capability-checks-instead-of-the-blanket-restriction)),
`spawn` / `spawnas` / persistence / PE-plugin execution.

---

## Current state (review findings)

Both lateral methods treat the source agent's own binary as the deployment
artifact, which is why a memory-hosted source cannot deploy at all:

- `lateralMoveWithProgress` refuses every lateral method when
  `RMM_IN_MEMORY` is set, with one blanket guard
  (`agent-go/lateral_windows.go`, `lateralMoveWithProgress`;
  `errMemoryHosted` in `agent-go/lateral.go`).
- `wmiexec` resolves `os.Executable()` and copies the source binary to
  `\\<host>\ADMIN$\System32\<name>` over SMB before starting it through
  WMI `Win32_Process.Create` (`agent-go/lateral_windows.go`, `case "wmiexec"`).
  The deployed agent therefore runs **from disk on the target**, and the
  stage must remain there while the process lives.
- `winrm` resolves `os.Executable()` and streams those same bytes as the
  final half of a framed stdin transfer
  (`agent-go/lateral_windows.go`, `case "winrm"`;
  `prepareWinRMTransfer` in `agent-go/winrm_transfer.go`). The receiver
  compiles an embedded C# loader with `Add-Type`, maps the PE in memory, and
  blocks on the agent thread, so this path already meets the no-disk goal —
  but only on targets the source agent itself can reach with its own binary.
- The loader (C# source, `winrmStage1CSharp`) and the receiver bootstrap
  (PowerShell, `winrmStage1Script`) are constants embedded in the agent
  binary; the receiver is started with `-EncodedCommand`
  (`winrmReceiverArguments`), and the loader travels as framed stdin data to
  stay under the WinRS command-line limit
  (`agent-go/winrm_transfer.go`).
- The server already maintains workspace-owned, build-configured artifacts:
  `latest_go_agent_binary(debug, workspace_id)` in `server_rmm.py` selects
  only artifacts bound to the source session's workspace, and PsExec already
  stages bytes through a masked, TTL'd staging endpoint. WMI and WinRM are
  the only consumers that never use it.
- No operator surface (REST, CLI, MCP, WebUI) exposes a loader or payload
  selection today; the method enum is closed at `psexec|wmiexec|winrm`
  (`LATERAL_METHODS`, `rmm_tools.py` schema, `web/app.js` parser).
- The `__LATERAL_CLEANUP__` retry path re-executes `os.Executable()` as a
  bounded child (`lateralCleanupCommand` in `agent-go/lateral.go`). A
  memory-hosted source running it would re-execute `powershell.exe` with a
  Go-agent flag; today this is prevented only indirectly because an
  in-memory source can never own lateral artifacts.

---

## 1. Loader options

Both `wmiexec` and `winrm` expose the same `loader` selection with two values:

| Value | Loader logic | Host relationship | Final agent start |
| --- | --- | --- | --- |
| `csharp` (default, the only value supported in this release) | Framed read, SHA-256 verification, PE mapping (sections, relocations, imports, TLS), environment setup, and thread start are C# source compiled on the target by `Add-Type` inside Windows PowerShell 5.1. PowerShell remains the host and only compiles and invokes it. | `powershell.exe` hosts the compiled loader | **In memory** — the Go agent image is mapped into the host process; no child process, no file. |
| `powershell` (defined, **reserved — not supported in this release**) | The same contract implemented as PowerShell script. `Add-Type` is permitted only for native method *declarations* (P/Invoke externs); all *logic* — framing, digest, section copy, relocations, imports, TLS — is PowerShell. Reserved for a later increment; the contract is documented now so the operator surface never needs to change shape. | `powershell.exe` runs the script directly | **In memory** — identical contract and start mode. |

**Release decision:** this release ships `csharp` only. `loader: powershell` is
rejected by the server, the agent, and every operator surface with an explicit
"reserved, not yet supported in this release" error — never silently
substituted. The parameter reserves the value so later increments ship without
an API, CLI, MCP, or WebUI shape change.

**Default:** `csharp`. It is the already-designed, smaller, compiled-mapping
path (today's WinRM loader is exactly this shape), it maps multi-megabyte Go
images at compiled speed, and it keeps one `Add-Type` unit for both loading and
mapping.

**Rejected combinations (fail closed with explicit errors, never silently
substituted):**

- **`loader: powershell` in this release:** rejected everywhere with the
  reserved-value error above.
- **Unsupported target architecture:** the C# mapper accepts AMD64 PE32+
  images only (machine `0x8664` check in `winrmStage1CSharp`). Until an arm64
  mapping exists and is natively validated, `wmiexec`/`winrm` with an arm64
  payload is rejected at command validation — not left to fail at runtime
  mid-transfer. The payload architecture must match the target
  architecture (see [§3](#3-the-server-is-the-artifact-source)).

- **Unsupported target architecture:** the C# mapper accepts AMD64 PE32+
  images only (machine `0x8664` check in `winrmStage1CSharp`). Until an arm64
  mapping exists and is natively validated, `wmiexec`/`winrm` with an arm64
  payload is rejected at command validation — not left to fail at runtime
  mid-transfer. The payload architecture must match the target
  architecture (see [§3](#3-the-server-is-the-artifact-source)).
- **PowerShell capability prerequisites absent:** Windows PowerShell 5.1
  present, `Add-Type` permitted (Constrained Language Mode rejects it).
  The bootstrap self-checks these before the payload stream starts and
  reports an explicit prerequisite error, so the failure is fast and
  diagnosable instead of a mid-stream digest or marker timeout.
- **Transport-specific flags:** HTTPS and certificate-bypass options remain
  WinRM-only, exactly as today.

**Compatibility rule:** loader selection (and every other new field) travels
under new wire-method names on the source agent, following the established
`winrm_https` / `winrm_https_insecure` pattern. Older agents must reject the
request instead of ignoring the new fields and falling back to copy-of-self.

## 2. Artifact separation

Three distinct artifacts must not be conflated. Knowing where the *bootstrap*
runs says nothing about where the *final agent* runs; each row states both.

| Artifact | What it is | Author / source | Delivered how | Written to target disk? |
| --- | --- | --- | --- | --- |
| **Bootstrap script** | Small PowerShell text that opens the payload channel, forwards framed bytes to the loader, reports readiness, and keeps the host process alive (blocks on the agent thread after the loader starts it). Contains sizes, digests, session ID, workspace ID, marker strings — no loader logic. | Rendered by the server, parameterized per deployment | WinRM: `-EncodedCommand` on the receiver command line (existing pattern). WMI: `Win32_Process.Create` command line of `powershell.exe -EncodedCommand ...`. | No. `-EncodedCommand` content never touches disk; PowerShell/WSMan internal scratch (e.g. `%TEMP%` compiler files from `Add-Type`) is a documented host-subsystem side effect, not a deployment artifact — keep the operator claim that precise, as in `docs/winrm-transfer.md`. |
| **Loader implementation** | The `csharp` (default) or `powershell` logic implementing the target loader contract: framed digest-checked receive, PE mapping, environment markers (`RMM_SESSION_ID`, `RMM_IN_MEMORY`, workspace), thread start. | Selected by the server, transported as data | Travels as the first framed half of the payload channel (never in the command line) — the existing WinRM framing model generalized to both transports. Loader text may stay embedded in the source agent binary in phase 1 (it is protocol-version-matched to the streaming code); server-side loader text is the versioned end state [open decision](#open-decisions). | No. Compiled in-process; only the `Add-Type` scratch caveat above applies. |
| **Final agent payload** | The configured Go agent executable — workspace-owned, build-mode-matched, architecture-matched, with embedded connection settings (`use_server_config`). | Server artifact store (`latest_go_agent_binary(debug, workspace_id)`) | Second framed half of the payload channel, streamed by the source agent after fetching the bytes from the server's masked staging endpoint over the source agent's existing HTTP channel. | **No** — this is the core change. Both loaders map the PE **in memory** inside the loader host process. There is no disk start for either loader, and the plan must keep it that way: an "in-memory bootstrap" that writes the payload to `%TEMP%` and starts it from disk is a disk start and is out of contract. |

**Explicit per-loader start mode:** both `csharp` and `powershell` start the
final agent **from memory**. The bootstrap and the agent share one process —
`powershell.exe` — for the deployment's whole lifetime. The contrast method is
PsExec (out of scope): it deploys a service host on ADMIN$ and an on-disk
stage2 agent by design, and today's `wmiexec` (ADMIN$ copy) is exactly the
artifact model this plan removes.

## 3. The server is the artifact source

The RMM server supplies the bootstrap (and the payload stream) through the
source agent's existing communication channel; the target never needs a new
egress path, and the source agent's hosting mode stops determining which
binary gets deployed.

**Chain of custody:**

```text
server ── lateral command (bootstrap text + loader name + masked stage ref)
      └─► source agent — any hosting mode
            ├── fetch masked payload bytes from the staging endpoint
            │   over its existing beacon-configured HTTP channel
            └── transport: WinRM stdin stream  |  WMI payload channel
                  └─► target powershell.exe: bootstrap → loader → mapped agent
```

**Artifact selection must preserve:**

- **Workspace ownership:** only artifacts whose build sidecar records the
  source session's workspace — the existing `latest_go_agent_binary`
  owner-filtering rule; a lateral deployment must never promote another
  workspace's embedded owner.
- **Build configuration:** debug vs. normal follows the source session's
  inherited build mode; debug sources require a server-side debug artifact and
  fail without fallback (existing behavior, now applied to all three methods).
- **Architecture:** amd64 payloads by default; arm64 rejected until the
  mapper supports it (see [§1](#1-loader-options)). Payload machine type is
  checked again on the target before mapping so a mismatch fails with a
  labeled error instead of a crash.
- **Version compatibility:** the bootstrap framing the server renders must
  match the streaming contract the source agent implements. Pin a protocol
  version in the lateral command; the source agent rejects versions it does
  implement, and old agents reject the whole new command shape because of the
  new wire-method names.

**Integrity across the chain:** the staging fetch is digest-verified against
the server's recorded digest when the source agent fetches, and the target
loader verifies the stream digest again before compiling or mapping — the
same half-digest framing the WinRM transfer uses today. Corruption between
server and source, or source and target, produces an explicit stage-labeled
failure.

## 4. Removing the copy-of-self assumption

- Delete the `os.Executable()` reads from the `wmiexec` branch (SMB copy of
  self to ADMIN$) and the `winrm` branch (streaming self). Both branches
  consume the server-provided, workspace-owned payload stream instead. The
  source agent becomes a relay and dispatcher, never the artifact.
- With the artifact decoupled, a memory-hosted source can deploy through any
  method whose chain does not execute or copy the source binary — which is
  then true for `wmiexec` and `winrm` by construction.
- `os.Executable()` remains only where it is genuinely "this process":
  registration's `process_name` reporting (cosmetic — a memory-hosted session
  reports `powershell.exe`, a documented display quirk), and the
  self-relaunching operations that keep their guards ([§7](#7-capability-checks-instead-of-the-blanket-restriction)).
- The cleanup retry child has been redesigned as designed in this plan and
  shipped: `__LATERAL_CLEANUP__` now runs a tightly scoped PowerShell
  worker (`agent-go/lateral_cleanup.go`) in a bounded child process instead
  of re-executing `os.Executable()`, and cleanup ownership is persisted by
  the control plane as durable deployment records, so the path is correct
  for every source, memory-hosted or not, including after source restarts.

## 5. Transport and loader responsibilities

The transports keep their execution mechanisms; the loader owns bootstrap
responsibilities uniformly across them.

| Responsibility | WinRM | WMI |
| --- | --- | --- |
| Remote execution | WSMan shell (`WSManCreateShell` path, existing Kerberos/Negotiate selection, HTTPS 5986 option) starts `powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand <bootstrap>`. | `Win32_Process.Create` (existing COM/DCOM chain in `agent-go/wmi_windows.go`) starts the same `powershell.exe -EncodedCommand <bootstrap>` command line. No `wmic.exe`, no service, no ADMIN$ file. |
| Payload channel | Shell stdin framed stream (existing `winrmTransfer` framing: sizes + SHA-256 per half, chunked, transfer-wide deadline). | New: SMB **named pipe**. The bootstrap creates a session-named pipe server; the source agent connects over `\\<host>\IPC$\PIPE\<session-suffixed-name>` and streams the identical framing. Pipes are kernel objects — no files on any disk. Session-specific names avoid collisions with concurrent deployments, mirroring the stage2 filename-collision fix. The bootstrap blocks on the pipe first, then on the agent thread, so the process survives pipe close. |
| Readiness / closing | Existing `RMM_LOADER_READY` stdout marker awaited by the sender (`waitLoaderReady`); receive failure, stream end, or deadline before the marker fails the deployment and tears the session down. | Same marker, written into the pipe before the receiver blocks on the agent thread; the source closes the connection only after the marker, preserving "a deployment without a proven loader must not survive." |

**Shared loader contract (both loaders, both transports):** framed receive →
digest check → compile/interpret → map PE → set environment markers
(`RMM_IN_MEMORY` included, so the deployed target's capability model applies
recursively) → start detached agent thread → emit readiness marker → block for
process lifetime.

**Explicit prerequisites per transport:**

- WinRM: 5985/5986 reachable, WinRS operational, Windows PowerShell 5.1,
  `Add-Type` permitted, target architecture amd64.
- WMI: DCOM/WMI reachable with local admin on the target, SMB named pipes
  reachable (port 445), Windows PowerShell 5.1, `Add-Type` permitted, target
  architecture amd64.

**Explicit errors at each boundary**, stage-labeled so operators can tell
apart: transport/session setup, bootstrap start, channel open, loader
compilation, digest mismatch, mapping failure, readiness timeout, and
registration wait. A channel that opens but never signals readiness must be an
operator-visible failure, not a silent host process left behind.

## 6. Process lifecycle review

The lifetime model differs by transport and must be documented per method:

- **Target host process:** in both transports the deployed agent lives inside
  a `powershell.exe` as a mapped PE with a detached Go thread. It has the
  process-lifetime constraints of an in-memory session (documented in
  `docs/winrm-transfer.md`): no spawn/spawnas/persistence/PE-plugin children,
  `RMM_IN_MEMORY` marker set, dies with host process termination.
- **WinRM dependency:** independent is the default lifetime. The receiver
  re-stages through a local WMI process (outside the shell's job object),
  the source closes the session after readiness, and the deployment has
  WMI-loader lifetime — it survives its source and ends at target shutdown
  or an explicit kill. The tethered lifetime (opt-in via `--tethered` /
  `independent: false`, winrm-scoped) keeps the deployment alive exactly as
  long as the source agent holds the WinRM shell (`winrmHeldShell` registry,
  `stopHeldWinRMShells` at source shutdown): it dies with the source agent
  or a target reboot, and suits targets whose WinRM session cannot create
  local WMI processes.
- **WMI dependency (new lifetime class):** `Win32_Process.Create` starts an
  independent process. A WMI-deployed in-memory session **outlives the source
  agent** and is killed only by target shutdown or explicit termination. The
  plan must state this operator-visible difference, and define the
  termination mechanism: the `kill` command against the registered session
  must terminate the host process (the in-memory agent's shutdown path
  already exits its host; review the kill/ack ordering for memory-hosted
  sessions during implementation).
- **Cancellation:** native COM/SMB/SCM calls cannot be canceled; the caller
  deadline still bounds waits, and the per-host in-flight claim
  (`errLateralHostBusy`) still prevents stacked deployments to the same host.
  Copy that claim discipline to the new WMI pipe streaming path. A source
  that closes the pipe mid-stream causes the receiver to throw and exit —
  the interrupted-delivery case leaves no artifacts.
- **Timeouts:** apply the WinRM precedent — one transfer-wide deadline bounds
  session setup, every channel packet, and the readiness wait — to the WMI
  path too, instead of per-operation budgets stacking.
- **Cleanup:** loader-based WMI/WinRM deployments leave **no remote
  artifacts**: no service, no ADMIN$ binary, no stage2 file, so the cleanup
  ownership registry stays PsExec-only. Post-failure hygiene is: tear down
  the WinRM session (existing) or close the pipe (receiver exits), and the
  `__LATERAL_CLEANUP__` retry path is redesigned per [§4](#4-removing-the-copy-of-self-assumption)
  so it no longer re-executes the source binary at all — for every source,
  memory-hosted or not.

## 7. Capability checks instead of the blanket restriction

The single `memoryHosted()` refusal for all lateral methods is replaced by
per-operation capability requirements. Each operation must state what it
actually needs:

| Operation | Real dependency | Disposition |
| --- | --- | --- |
| `lateral wmiexec` / `lateral winrm` (new chain) | A server-sourced payload and a channel to the target — not the source binary | **Allowed for memory-hosted sources** once [§4](#4-removing-the-copy-of-self-assumption) lands; legacy wire methods without artifact-source fields keep the old refusal so old payloads never copy the host binary silently |
| `lateral psexec` | Embedded service host (an in-image constant, so the bytes exist even in memory), ADMIN$ write, SCM — and a cleanup worker that requires Windows PowerShell (`powershell.exe` in System32), never the source binary | **Implemented follow-on:** the deployment stages the embedded lateral service and the cleanup retry runs the dedicated PowerShell worker with a beacon-secret ownership tag, so a memory-hosted source may run psexec; the blanket guard is gone. Any missing capability (embedded service bytes, PowerShell worker) fails with an explicit capability error. Native Windows validation of the from-memory combination is still pending |
| `spawn` / `spawnas` | Exec/copy the actual agent binary (`os.Args[0]` / `os.Executable()` name the PowerShell host when in memory) | `spawn` keeps the guard. `spawnas` is implemented as the staged follow-on (`__SPAWNAS_STAGED__`): it deploys the server-staged workspace build off disk under CreateProcessWithLogonW, so memory-hosted sources may run it; the legacy copy-of-self wire command keeps its guard for old control planes |
| Persistence install | Copies `os.Executable()` to disk | Guard stays |
| PE plugin execution | Child processes run `os.Args[0]` | Guard stays for the long-lived channel plugins (socks/tun): their lifetime needs the copy-of-self child. Implemented exception: one-shot plugin commands (`__SCREENSHOT__`, `__PE_LOAD__`, `__PLUGIN_COMMAND__`) run from memory-hosted sessions in a bounded Windows PowerShell child hosting the identical stdin-framed plugin child contract — plugin implementations stay in the plugin repository, the bytes map in memory through an Add-Type compiled mapper, nothing is written to disk, and a bad plugin still crashes only the child |

The general rule: **guard on the dependency, not the hosting mode.** The
refusal message must name the missing capability, and the accepted combination
list must be covered by tests on both sides (memory-hosted source deploying
successfully; on-disk source unchanged).

A target deployed in memory by either loader inherits `RMM_IN_MEMORY` and
therefore the same capability model as its source if the source was
memory-hosted — expected, recursive, and now explicit instead of blanket.

## 8. Outcome reporting

Reporting must distinguish these states, and only the last one is success:

1. **Submitted** — REST accepted the lateral command; it is queued to the
   source agent (existing `launch_status: acknowledged/failed/inconclusive`
   enumeration is preserved and extended with the fields below).
2. **Acknowledged** — source agent accepted and dispatched through the
   transport (existing `LATERAL_RESULT_RE` ack).
3. **Bootstrap ready** — the target receiver compiled the loader, verified
   digests, and mapped the agent (`RMM_LOADER_READY` received by the source
   and reported as a progress event plus a `bootstrap_ready` result field).
   Diagnostic only: it proves the deployment machinery, never that a usable
   agent exists.
4. **Registered (confirmation)** — the reserved target session ID registers
   with the matching workspace (existing `_verify_deployment`:
   `_await_session_registration` + workspace equality). **Registration
   remains the single confirmation that a usable target agent exists**;
   bootstrap readiness without registration is an inconclusive-or-failed
   outcome, exactly as an ack without registration is today.
5. **Failed** — an explicit, stage-labeled error from any boundary in [§5](#5-transport-and-loader-responsibilities).
6. **Inconclusive** — the caller's deadline expired during a blocking native
   operation that cannot be canceled; acknowledged artifacts are preserved
   per the existing await semantics.

The result payload gains the loader selection (`loader`) and the
bootstrap-ready stage so operators can tell a silent wait from dead machinery
without reading agent logs.

## 9. Validation matrix

Native Windows validation on real targets is required before claiming support
for any combination — host Go tests and cross-builds do not validate remote
loading, per the repository's platform policy.

**Primary combinations (2 transports × 1 supported loader × 2 source hosting
modes = 4; the `powershell` loader is reserved and rejected, so it adds no
combinations in this release):**

- For each: submit from REST, verify staging fetch, channel stream, loader
  compile, readiness event, registration of the reserved session ID, correct
  workspace binding, and **zero deployment-authored files on the target**
  (Process Monitor audit; the `Add-Type` `%TEMP%` scratch exception documented).
- WinRM deployments verify source-lifetime semantics (kill the source; the
  target session dies); WMI deployments verify the new independent-lifetime
  semantics (target survives source death; explicit kill terminates it).

**Failure scenarios:**

- Artifact corruption: corrupt loader half, corrupt payload half, mid-stream
  truncation — receiver must throw and exit; sender must report a labeled
  failure; no host process survives with an unproven loader.
- Incompatible architecture: arm64 payload vs. amd64 loader rejected at the
  command-validation stage; a mismatched image that reaches the target
  (server misconfiguration) fails the machine-type check before mapping.
- PowerShell restrictions: Constrained Language Mode / disabled `Add-Type`
  fail the bootstrap prerequisite check with an explicit error, not a hang;
  execution policy is not a factor for `-EncodedCommand` (document, don't
  assume).
- Interrupted delivery: source killed mid-stream (receiver exits); target
  rebooted after readiness but before registration (inconclusive timeout,
  retriable under the in-flight claim); pipe/session torn down at every stage.
- Timeout handling: transfer-wide deadline at each stage produces the correct
  stage label; a timed-out-but-successful worker still releases the host
  claim (existing pinned-worker release semantics preserved on the new path).
- Cleanup failures: PsExec unchanged (retained-handle deletion path);
  verify no new cleanup obligations exist for loader-based deployments
  (nothing on disk; held WinRM shells close; pipes close with their process).
- Repeat and concurrent deployments: second deployment to a host with a live
  in-memory agent uses distinct channel names and succeeds; same-host
  concurrent dispatch still returns `errLateralHostBusy`.
- Old-agent compatibility: payloads using new wire methods against agents that
  predate them are rejected, never silently degraded to copy-of-self.

## 10. Operator surfaces and documentation

The `loader` parameter is exposed consistently, per the repository parity rule
(REST → CLI → MCP, plus WebUI):

- **REST** `POST /api/v1/sessions/{id}/lateral`: `loader` enum
  (`csharp` default; `powershell` accepted syntactically but rejected with the
  reserved-value error), strict-string validation, plus the new result
  fields (`loader`, `bootstrap_ready`).
- **CLI** `lateral` subcommand and `/lateral` shell meta command:
  `--loader csharp|powershell` with identical defaults; help text updated.
- **MCP/AI** `lateral_move` tool: `loader` parameter, StrictBool-style
  strictness for existing booleans is unchanged; docstrings updated; update
  `scripts/check_operator_parity.py` and run **`make check-parity`**.
- **WebUI** `web/app.js` lateral parser, completions, and help line: same
  flag, same default, same rejection messages.
- **Documentation:** update the lateral sections of `README.md`,
  `docs/go-agent.md`, and `ai-skills/lateral-movement.md` /
  `ai-skills/agent-deployment.md`; generalize `docs/winrm-transfer.md` or
  link it from this page; record prerequisites (PowerShell 5.1, `Add-Type`,
  amd64), target hosting behavior (in-memory agent inside a
  `powershell.exe` host), lifetime dependencies (WinRM: source lifetime;
  WMI: independent until reboot/explicit kill), and verified limitations —
  including that native validation status is the gate for every supported
  claim, per `docs/progress.md`.
- **Logging boundary:** loader and deployment lifecycle events are logged on
  the server (operator audit); the target receiver keeps no RMM log and the
  bootstrap emits only contract markers; keep payload bytes, URLs, and
  credentials out of target-side logs per the repository logging rule.

---

## Implementation order

1. **Baseline:** native lab validation of the existing WinRM in-memory chain
   (already an open item in `docs/progress.md`) — do not generalize an
   unproven loader.
2. **Server-sourced payload for WinRM** (removes `os.Executable()` from the
   winrm branch, staging fetch + digest re-verify, new wire methods, runtime
   identical otherwise).
3. **WMI loader path:** named-pipe payload channel + shared bootstrap/loader
   contract behind `wmiexec`; remove the ADMIN$ copy.
4. **Capability refactor:** replace the blanket `memoryHosted()` lateral
   refusal with the per-operation table; redesign the cleanup child; PsExec
   behavior unchanged.
5. **`powershell` loader variant:** explicitly out of this release; every
   surface rejects `loader: powershell` with the reserved-value error, and
   the rejection itself gets coverage on each surface.
6. **Operator surfaces + parity + docs** ([§10](#10-operator-surfaces-and-documentation)).
7. **Validation matrix** ([§9](#9-validation-matrix)) executed on real
   Windows targets; only then claim support.

## Open decisions

- **WMI payload channel:** named pipe over SMB is the recommended primary
  (no files, no target egress, rides the same admin/SMB prerequisite). A
  target-side fetch from the staging endpoint (the PsExec stage1 model) is
  the fallback — simpler channel code, but it makes the target fetch from
  the server directly and diverges from "the server supplies the bootstrap
  through the source agent's channel."
- **`powershell` loader scope (decided):** single-loader release with `csharp`;
  the `powershell` value stays reserved on every surface and is rejected with
  an explicit error. Implementing the pure-script mapper is a later increment.
- **Where loader text lives:** embedded in the source agent (phase 1,
  protocol-matched) vs. server-supplied (end state). Server-supplied loader
  text also resolves loader/agent version skew cleanly by pinning both to
  the same protocol version.
- **arm64:** horizon and precedence — mapper support first, or an explicit
  long-term amd64 statement in the docs.
- **PsExec from memory-hosted sources:** implemented follow-on. The cleanup
  retry no longer re-executes the source binary (it runs the dedicated
  PowerShell worker with a beacon-secret ownership tag), the hosting-mode
  guard for psexec is lifted, and cleanup ownership is persisted in the
  control plane's deployment records. The from-memory combination still
  needs its own native Windows validation before being claimed as supported.

# Progress Log

For current runtime behavior, use the [documentation wiki](README.md) and its
[architecture overview](architecture.md). This file remains the chronological
implementation log.

> Living document — update at the end of every work session.  
> Claude reads this via `/catchup` (`/.claude/commands/catchup.md`).

## Current Status

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

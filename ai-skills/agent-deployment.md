---
id: agent-deployment
title: Deploy an agent on another machine
description: Deploy an RMM agent on another authorized machine end to end: render or build the artifact with server config, stage it through an existing agent, execute laterally, and verify the new session registers.
default: true
---

# Deploy an agent on another machine

## Supported platform and workspace ownership

Managed agents are **Windows-only**. Linux/macOS agent deployment is not
supported. The control plane and Exegol workspaces can run on other platforms.

Configured PowerShell scripts and Go builds include the authenticated workspace
ID. Agents send that ID on registration; the server assigns the session before
publishing events and rejects attempts to change an existing owner. Spawn and
lateral deployment inherit the source workspace. Do not deploy an artifact from
another workspace or use an operator token as agent configuration. For an
administrator build with multiple workspaces, supply the intended `workspace_id`.

## Execution constraints

This reference applies only when included in the explicitly submitted AI
playbook; `default: true` does not load it into production AI requests.

Agent command execution is synchronous. Running a long-lived agent binary or
the supplied `run-agent.ps1` launcher in the foreground can
block the controlling agent's command loop indefinitely. `queue_command`
only changes submission timing; it does not detach the target process.
An operator tool timeout does not cancel the target command or prove that the
controlling agent was killed.

Before execution, establish that the selected launch mechanism returns promptly
and gives the new process an independent lifetime and standard streams. This
also applies to a second agent on the same machine. If these properties cannot
be established, stop before launching and report the limitation. Do not kill
the controlling agent or repeat an inconclusive launch as a recovery step.

For a second Go agent on a host that already runs one, prefer the
`spawn_session` tool: it starts a detached copy of the running agent with a
fresh session ID, waits for registration, and verifies that both sessions
answer a harmless command. Report `verified: false` results to the operator
instead of resubmitting; never launch an agent binary through a foreground
shell command on an existing session.

Workspace requests allow the configured execution, registration, and verification
wait budgets. Expiration is still an inconclusive outcome: it does not cancel a
queued spawn or lateral move. Inspect existing session and event evidence before
any recovery action; do not resubmit merely because the operator request expired.

Use a fresh session identity for each process. Check for inherited
`RMM_SESSION_ID` and a fixed identity embedded in the build; the generated
launcher currently preserves inherited values when its configured ID is empty.
Record existing session IDs before launch. A second agent on the same host
must register under a new ID, and both sessions must still respond afterward.

## Purpose and scope

Apply this skill when the operator asks to get an RMM agent running on another
authorized machine in the lab. The deployment is a composed chain, and every
step is available as an AI tool — no manual WebUI action or hand-built script
is required:

1. Produce a configured artifact (`deploy_agent_script` or `build_go_agent`).
2. Stage it on an existing agent (`queue_upload`).
3. Copy and execute it on the target through a lateral technique.
4. Verify the new session registers (`list_sessions`).

Deployment on a new host may proceed autonomously within the lab, like any
lateral action. The server renders the artifact with the active
beacon URL and beacon secret, and the secret is not returned to the AI: report
artifact paths and metadata, never secret-bearing file content.

## Building blocks

- `deploy_agent_script` renders a ready-to-run `client_rmm.ps1` server-side,
  configured with the server's active beacon URL and beacon secret. It returns
  a portable `artifact_ref` and server-local `path`, size, SHA-256, and the base URL used. The
  beacon secret never enters the tool response or the chat.
- `build_go_agent` compiles the repository Go agent on the server with the
  active connection settings embedded, and returns the ZIP `artifact_ref` and server-local `path`
  (binary + launcher + README), the binary name, size, and SHA-256. Windows
  amd64 is the normal target; Windows arm64 is also allowlisted.
- Both tools fail with `no_active_beacon_url` when no beacon transport is
  running. Check `profile_status` first, and start one with `start_profile`
  autonomously when an artifact is needed; report the prerequisite if no
  transport can be started.
- `queue_upload` stages an artifact reference or local file on an existing agent. A
  lateral channel that supports file copy (SMB `ADMIN$`/`C$`, WinRM
  `Copy-Item -ToSession`) can also deliver the file directly to the target.
- Remote execution uses the techniques in `lateral-movement`. The Go agent
  executable itself needs no PowerShell. Agent-side `lateral winrm` uses
  Windows PowerShell as its in-memory receiver, transferring the agent
  through the authenticated WinRM session and loading it without SMB or a
  file on disk. Manual deployment can run the extracted binary or its
  `run-agent.ps1` launcher.

## Workflow

1. Select the target, the credential to reuse, and the lateral execution
   technique autonomously, following `lateral-movement`.
2. Check `profile_status` for an active beacon URL. If none is active, start
   the transport with `start_profile`, or report the prerequisite if it cannot
   be started, before rendering an artifact.
3. Produce the artifact:
   - PowerShell agent: `deploy_agent_script` (optionally `session_id`,
     `sleep_seconds`, `jitter_percent`, `http_proxy`). Each rendered script
     generates a new session ID at first run unless a fixed one is requested.
   - Go agent: `build_go_agent` with the target `goos`/`goarch`.
4. Before staging, record the pivot session's latest event cursor with
   `get_events`. Stage with `queue_upload` using the returned `artifact_ref`
   as `local_path` and an explicit destination filename. A successful tool
   response confirms only that the upload was queued, not that the file exists.
   Poll `get_events` from that cursor and wait for the agent's successful
   upload result matching this session, upload command, and destination.
   Advance the cursor as events are consumed; old results for the same path
   are not evidence for this upload. If overlapping uploads make attribution
   ambiguous, verify the staged file's SHA-256 against the artifact metadata
   before continuing. Do not print the file contents.
   Only after delivery is confirmed, copy to the target over the lateral
   channel. For a Go ZIP, extract remotely (`PS: Expand-Archive`) or upload
   and extract on the target. Verify each transfer completes before its file
   is consumed, including when switching between agent and Exegol execution.
   On an explicit upload failure, stop dependent actions and diagnose it.
   On a timeout or missing result, inspect existing evidence and report
   delivery as inconclusive if unresolved; do not copy, extract, execute, or
   blindly queue another upload.
5. Execute remotely through the same lateral technique:
   `powershell -ExecutionPolicy Bypass -File <path>` for the script agent, or
   the extracted binary/launcher for the Go agent, using a launch mechanism
   that satisfies the execution constraints above. These paths describe the
   payload, not a safe foreground command for an existing agent session.
6. Verify registration: `list_sessions` must show a new session whose host is
   the target. A remote process start message is not proof; the new session
   is. Use `get_session` on it and, if needed, a local identity command on the
   new session to confirm which host answered.
7. Report what was rendered, where it was staged and executed, which new
   session ID appeared, and what was verified. The beacon runs while its
   process runs; installing persistence on the new host
   (`install_persistence`) is a separate operator action requiring explicit
   intent.

## Common failure causes

- **`no_active_beacon_url`**: no beacon transport is running. Start a profile
  or report the prerequisite; the artifact cannot be rendered without it.
- **No new session appears**: the target cannot reach the server URL. An
  agent on the pivot host proves nothing about the target's route; check the
  target's connectivity and consider the `http_proxy` argument when the
  target reaches the server only through a proxy.
- **Lateral execution fails**: classify the error following
  `lateral-movement` (connectivity, authentication, authorization, policy,
  detection) and try the next technique.
- **AV/EDR on the target flags the artifact or execution style**: a
  deployment blocker — report it; trying another delivery technique is
  autonomous, with the change stated in the report.
- **Go build fails server-side**: the tool returns the server's error
  (missing toolchain, timeout, unsupported target); report it rather than
  retrying unchanged.

## Deployment proof

The spawn/lateral tools reserve a fresh expected session ID before launch. A
process-creation message, successful shell exit, or service-wrapper timeout is
not deployment proof. Only registration of that expected agent under the source
workspace permits `registered: true` / `ok: true`. The tools retain the expected
ID even when the launch acknowledgment is lost; inspect that ID after a timeout
instead of resubmitting. With verification disabled, the result is only request
acceptance (`pending: true`, `ok: false`, `registered: null`).

`verified: true` additionally requires both source and new agents to return the
exact output of different fresh command probes. Unrelated events or command
errors do not count. Inspect `registered_hostname` against the intended target
and report registration separately from successful command execution.

## Reporting

Distinguish three states explicitly: executed remotely (the launch command
succeeded), registered (a new session exists), and verified active (the new
session responds to commands). Include the target scope, the technique used,
the artifact metadata (filename, SHA-256), the new session ID, and remaining
unknowns. Do not claim a host is managed because a file was copied or a
process started — only registration proves the agent is operational. Never
print artifact contents; they contain the beacon secret.

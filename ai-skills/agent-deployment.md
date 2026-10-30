---
id: agent-deployment
title: Deploy an agent on another machine
description: Deploy an RMM agent on another authorized machine end to end: render or build the artifact with server config, stage it through an existing agent, execute laterally, and verify the new session registers.
default: false
---

# Deploy an agent on another machine

## Purpose and scope

Apply this skill when the operator asks to get an RMM agent running on another
authorized machine in the lab. The deployment is a composed chain, and every
step is available as an AI tool — no manual WebUI action or hand-built script
is required:

1. Produce a configured artifact (`deploy_agent_script` or `build_go_agent`).
2. Stage it on an existing agent (`queue_upload`).
3. Copy and execute it on the target through a lateral technique.
4. Verify the new session registers (`list_sessions`).

Deployment on a new host is an operator-directed action. The target must be
within the authorized scope. The server renders the artifact with the active
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
  amd64 is the normal target; other allowlisted pairs exist.
- Both tools fail with `no_active_beacon_url` when no beacon transport is
  running. Check `profile_status` first, and start one with `start_profile`
  when the operator wants an autonomous retry — otherwise report the
  prerequisite.
- `queue_upload` stages an artifact reference or local file on an existing agent. A
  lateral channel that supports file copy (SMB `ADMIN$`/`C$`, WinRM
  `Copy-Item -ToSession`) can also deliver the file directly to the target.
- Remote execution uses the techniques in `lateral-movement`. The Go agent
  needs no PowerShell: run the extracted binary or its `run-agent.ps1`
  launcher.

## Workflow

1. Confirm the authorized target, the credential to reuse, and the lateral
   execution technique. Deployment requires the operator's intent, like any
   lateral action.
2. Check `profile_status` for an active beacon URL. If none is active, start
   or report the transport prerequisite before rendering an artifact.
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
   the extracted binary/launcher for the Go agent.
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
  detection) and try the next authorized technique.
- **AV/EDR on the target flags the artifact or execution style**: a
  deployment blocker — report it; do not silently switch to a more aggressive
  delivery.
- **Go build fails server-side**: the tool returns the server's error
  (missing toolchain, timeout, unsupported target); report it rather than
  retrying unchanged.

## Reporting

Distinguish three states explicitly: executed remotely (the launch command
succeeded), registered (a new session exists), and verified active (the new
session responds to commands). Include the target scope, the technique used,
the artifact metadata (filename, SHA-256), the new session ID, and remaining
unknowns. Do not claim a host is managed because a file was copied or a
process started — only registration proves the agent is operational. Never
print artifact contents; they contain the beacon secret.

---
id: rclone-exfil
title: File exfiltration via rclone MCP
description: Transfer staged files from a Windows agent to a configured remote (Mega, S3, etc.) using the rclone MCP tool. Use for authorized data exfiltration tasks in lab environments.
default: true
---

# File exfiltration via rclone MCP

Use the rclone MCP tool to transfer files from a Windows agent to a pre-configured
remote storage. `operator-boundaries` applies: do not modify or delete pre-existing remote data.

## Prerequisites

- An RMM agent session active on the host holding the staged files.
- A rclone remote pre-configured on that agent (verify with the rclone MCP tool
  before transferring).
- The staged directory path and approximate size confirmed before starting.

## Workflow

1. Send the staged files to the target remote by instructing the rclone MCP:
   "Send the files in `<local path>` to `<remote name>` using rclone."
   The MCP resolves the remote configuration, constructs the command, and executes
   it on the agent.

2. Wait for transfer completion. Do not retry a transfer whose outcome is uncertain
   — inspect the rclone output for bytes transferred and error count first.

3. Verify the transferred volume from the rclone output (bytes sent, file count).
   Record the volume in the task report. Do not claim success from submission alone.

4. After confirmed transfer, clean up the local staging directory if it was created
   during the current task (no consent required for agent-created files).

## Interpreting rclone output

| Field | Meaning |
|-------|---------|
| `Transferred` | Bytes and files successfully sent — primary success evidence. |
| `Errors` | Count of failed files — investigate before claiming success if non-zero. |
| `Elapsed time` | Wall-clock duration — useful for sizing future transfers. |

A non-zero error count with partial transfer is partial completion, not success.
Report remaining files and the error detail.

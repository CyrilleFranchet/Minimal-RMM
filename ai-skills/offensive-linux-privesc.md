---
id: offensive-linux-privesc
title: Linux privilege-escalation investigation
description: Investigate a Linux target's local escalation paths using effective identity, sudo rules, SUID/capabilities, privileged consumers, and namespace boundaries. Not a request to inspect or escape the operator's Exegol container.
default: false
---

# Linux privilege-escalation investigation

Adapted from [Claude-Red offensive-linux-privesc](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/privesc/offensive-linux-privesc/SKILL.md).
Retain its sudo/SUID/capability/service decision categories and telemetry focus;
replace automatic scanner downloads and historical exploit recipes with
target-specific prerequisite checks. `operator-boundaries` remains authoritative.

## Establish the host and privilege boundary

Bind commands to an authenticated session or explicitly selected existing SSH
channel on the target. Confirm hostname, identity, OS/build, and namespace
before interpreting results. A generic Exegol MCP shell runs on the operator
workspace: do not treat its root account, Docker socket, capabilities, or SUID
programs as findings on the lab target. No operator-container escape is implied.

Useful initial observations on the actual target:

```sh
hostname
id
uname -r
cat /etc/os-release
sudo -n -l
```

An unsuccessful noninteractive sudo listing may mean authentication is required;
it does not prove the user has no sudo rights. Do not repeatedly provoke password
prompts. Record real/effective identity and container/user-namespace context:
UID 0 inside a container does not establish host-root control.

## Select a branch from existing evidence

| Candidate | Establish before claiming a path | Common misleading observation |
|-----------|----------------------------------|--------------------------------|
| Sudo rule | Exact executable/arguments, allowed run-as user, authentication requirement, environment restrictions, and actual binary version. | An allowed command name without its argument/run-as constraints. |
| SUID/SGID binary | Owner, effective mode, mount options, execution behavior, privilege-dropping behavior, and current namespace. | The setuid bit on a script or a file on a `nosuid` mount. |
| File/process capabilities | Exact capability sets, supported operation, bounding/ambient context, namespace, and resulting effective permissions. | A capability label assumed to grant unrestricted root. |
| Cron/systemd consumer | Privileged run identity, actual script/binary and working directory, file/parent permissions, schedule/trigger, and environment. | A writable script that no privileged process uses. |
| Search-path/library input | Actual privileged consumer, runtime resolution order, attacker-controlled candidate, and whether secure execution ignores the relevant environment. | A writable PATH directory or environment variable alone. |
| Container-management socket | Effective socket access, daemon identity, rootless versus rootful mode, restrictions, and host boundary. | Group membership when the socket is inaccessible or the daemon is rootless. |
| NFS export | Relevant server export policy, allowed client identity, write access, ID mapping, mount flags, and privileged consumer. | `no_root_squash` text without a usable export or executable path. |
| Vulnerable package/kernel | Exact vendor build, backported fixes, architecture/configuration, reachable prerequisite, and stability impact. | Upstream version matching an old CVE range; PwnKit and sudo flaws are userspace issues, not kernel exploits. |

Start at relevant application/service paths rather than recursively reading the
whole filesystem. `stat`, `namei -l`, `getfacl`, `findmnt`, and `getcap` are useful
when already installed; use available equivalents or report visibility limits.
Read exact service units, included overrides, and script references. Account for
ACLs, symlinks, parent-directory replacement rights, and filesystem boundaries.
For broader SUID/capability inventory, bound the filesystem scope and retain
unreadable locations instead of silently treating them as clean.

Inspect trusted installed binaries without running unknown code as a diagnostic.
Do not assume `ldd` or tracing a supplied executable is a passive file read;
use existing static inspection tools when the file is untrusted. A GTFOBins-style
recipe is a hypothesis to compare with the actual binary and invocation rules,
not proof that the target preserves privileges.

## Build and validate the candidate chain

Write the chain as current identity → controllable object or allowed operation
→ privileged consumer → trigger → resulting identity/namespace. Rank candidates
by demonstrated links, reliability, and changes required. Report a missing
trigger or missing effective write permission as a blocker, not as success.

For requested validation, select a bounded proof of resulting identity and
host/namespace. Apply `operator-boundaries` to service/unit edits, pre-existing
file replacement, credential changes, and mounts/configuration changes. Existing
authorization remains valid; read-only investigation needs no extra approval.
Do not install tools, pipe downloads into a shell, modify `/etc/passwd`, leave
SUID shells, create persistence, or clear history as automatic next steps.
Credential investigation is relevant only when it serves the current task;
do not dump arbitrary histories, keys, or application secrets for completeness.

Preserve command/job IDs for asynchronous triggers. On missing output or a
timeout, inspect process and artifact evidence before rerunning. Stop once the
question is answered, proof is verified, prerequisites are blocked, or outcome
is uncertain. Cleanup covers only agent-created artifacts within the authorized
task; never erase existing logs or history.

## Detection and handoff

Return target/session, before/after identity and namespace, exact permissions,
consumer/trigger evidence, confidence, time window, sanitized artifact references,
and authorized change/rollback status. Root-level file access is not equivalent
to root command execution; distinguish the capability actually demonstrated.
Expect host process/authentication/audit/service telemetry according to the
tested path and available logging. Local checks may add nothing to the PCAP;
network authentication or NFS/container API operations may be visible separately.
Mark detection observed only with independent sensor evidence, not because a
command produced the desired result.

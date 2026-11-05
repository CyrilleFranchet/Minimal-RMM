---
id: offensive-netexec
title: NetExec protocol investigation and access validation
description: Use installed NetExec in Exegol for targeted protocol enumeration, authentication diagnosis, and validation of an authorized remote-access path. Not a replacement for the Python BloodHound collection workflow.
default: true
---

# NetExec protocol investigation and access validation

Adapted from [Claude-Red offensive-netexec](https://github.com/SnailSploit/Claude-Red/blob/a8de813edc9f66f60d44cfe42d47750e89d9930b/Skills/active-directory/offensive-netexec/SKILL.md).
The upstream catalog is a technique reference, not a tested contract for the
installed NetExec release. `operator-boundaries` applies to every operation.

## Establish the question and execution surface

Run NetExec through the verified Exegol MCP workspace. Record target endpoint,
protocol, account scope, authentication material type, and intended outcome.
Load `exegol-rmm-pivot` if the workspace lacks a confirmed route or DNS path.
A Windows agent reaching the target does not prove Exegol can reach it.

Separate four questions: is the endpoint reachable, can this identity
authenticate, what access does it have, and did a requested action complete?
Select one question before selecting flags. An enumeration request does not
itself request password spraying, secret extraction, coercion, or execution.
For requested remote execution, load `lateral-movement`; existing lab
authorization remains applicable without an extra approval for every attempt.

Inspect the installed command first, without contacting a target:

```sh
nxc --help
nxc smb --help
nxc ldap --help
```

Use the relevant protocol help, not all protocol help on every run. If modules
are needed, inspect the installed module listing and option help advertised by
that release. Check implementation or authoritative documentation when a
module's name hides side effects. Never infer that a vulnerability-check module
is read-only, or guess option names from this or an upstream skill.

## Choose the smallest useful protocol check

| Evidence or question | Select | Next evidence to obtain |
|----------------------|--------|-------------------------|
| SMB endpoint identified | SMB negotiation, then requested share/access enumeration | Host/domain identity, signing observation, exact share rights under the chosen identity. |
| Directory object or permission question | Targeted LDAP query/module | Exact DN/SID, relevant attributes or ACEs, successful bind and query scope. |
| WinRM or WMI access requested | That protocol against the named host | Authentication result followed by authorized bounded execution evidence, if requested. |
| Database endpoint identified | MSSQL identity/role/query checks | SQL login versus domain login, effective role, requested query result. Do not enable command-execution features to make a check succeed. |
| SSH access requested | SSH with the known identity | Remote account and command context; successful login does not imply root. |
| Other exposed service | Its installed NetExec protocol, only if relevant | Protocol-specific access evidence; do not translate another protocol's success label. |

For SMB negotiation, `nxc smb <target>` is a starting shape, not permission to
replace the named host with a subnet. Add authentication and action options only
after checking local help. Use existing protected credential references/cache
mechanisms where supported; do not place secrets in reports or AI chat.
Do not use NetExec's BloodHound flags as an implicit collector fallback: the
project's Python BloodHound adapter owns DCOnly collection and its provenance.

## Interpret and branch

1. **Timeout/refusal:** retain source namespace, destination, port, and duration.
   Confirm route/listener evidence before changing authentication. A timeout is
   not proof of a closed port or an invalid password.
2. **Authentication failure:** verify domain versus local-account interpretation,
   hostname/SPN and time requirements for Kerberos, and account policy evidence.
   Stop on lockout indications; do not expand credential attempts automatically.
3. **Success label:** retain the protocol's original result. `Pwn3d!` and other
   banners are tool assertions whose meaning varies by protocol/version; they
   are not proof that a requested command ran or a domain path is usable.
4. **Access denied after login:** identify the denied operation and required
   rights. An SMB login can succeed while share access or remote execution fails.
5. **Execution or module uncertainty:** keep its job ID/output artifact and
   inspect side effects before retrying. Do not allow automatic executor
   fallback to change a protocol-specific demo without recording that change.

Limit target count, concurrency, and wait budget from the actual question.
For an explicitly requested authentication campaign, account for attempts across
hosts and protocols; inspect local option semantics before supplying user and
password lists. A pacing option does not make lockout impossible.

## SMB share enumeration

To enumerate shares on a target and confirm access rights under a given identity:

```sh
nxc smb <target> -u <user> -p <pass> --shares
```

Inspect the output for share name, read/write access, and remarks. A hidden
administrative share ends in `$` and does not appear in `net view` output on the
target — enumerating with `--shares` is the correct discovery method. Record each
share name, access level, and whether it is a hidden share before attempting to mount.

For multiple targets, run one command per host; do not replace the target with a
subnet unless the task explicitly requests a subnet sweep.

## Evidence and detection handoff

Return target/protocol/identity reference, installed version or help fingerprint,
sanitized command shape, time window, observed result, and next unresolved
prerequisite. Preserve full sensitive output only in protected local artifacts.
Stop when the selected question is answered rather than chaining every module.

Expect protocol traffic and potentially authentication, directory-access, or
remote-execution telemetry depending on the chosen operation. Record the
actual source address after pivoting. Do not claim a detector fired from a
NetExec banner; require independent sensor evidence. Prefer reproducible,
observable tests for the demo over stealth-oriented defaults.

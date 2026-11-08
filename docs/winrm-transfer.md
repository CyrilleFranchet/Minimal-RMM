# WinRM in-memory agent transfer

`lateral winrm <host>` transfers the source agent executable through the
authenticated WinRM session and starts it **in memory** on the target. No
agent binary is written to the target's disk: the received bytes are mapped
into a `PAGE_EXECUTE_READWRITE` allocation and started on a detached thread
inside the receiver process. Rebuild and redeploy the source Go agent to use
this behavior; existing agent binaries retain their previous implementation.

This implementation has not passed native Windows deployment validation.
Compilation and mocked transport tests do not establish that the mapped agent
starts, registers, or supports normal operations. The behavior below describes
the intended design, pending that validation.

## Requirements and transport

- A Windows target with Windows PowerShell and a reachable WinRM listener:
  HTTP/5985 by default, or HTTPS/5986 with `-s` / `--https`.
- An identity authorized to use WinRM; the receiver needs no file writes,
  service rights, SMB, or WMI.
- A source agent built with embedded connection settings, as for the other
  lateral methods. The target receives the new session and workspace binding.
- Windows PowerShell must accept `Add-Type` (full language mode; Constrained
  Language Mode rejects the loader compilation).

Transfer uses the existing WSMan authentication and TLS settings. The selected
`make_token` credentials apply to the transfer; without `make_token`, the
existing process-credential behavior applies. HTTPS certificate validation
remains enabled unless `-k` / `--insecure` is explicitly selected with HTTPS.
The command does not change TrustedHosts, configure a listener, or retry over
another transport.

## Transfer and in-memory launch

The source streams a framed payload through `WSManSendShellInput` to the
receiver's standard input:

1. The UTF-8 **loader source** (a C# stage1 compiled in the receiver with
   `Add-Type`), with its exact length and SHA-256.
2. The **agent executable**, with its exact length and SHA-256.

Chunks contain at most 32 KiB; the executable size limit is 128 MiB. The
receiver verifies both digests before use. Keeping the loader on the stdin
channel holds the receiver command below the WinRS 8191-character command
limit: the loader travels as data, not as an encoded command parameter.

The loader then follows the classic injection pattern:

1. `VirtualAlloc` one `PAGE_EXECUTE_READWRITE` image of `SizeOfImage` bytes.
2. Copy the received bytes into it as a full PE32+ amd64 mapping: headers,
   sections, base relocations, import table, and the TLS directory (a Go
   binary needs its TLS slot patched into the fresh thread's TEB before the
   entry point can run). The agent is a PE, not raw shellcode, so the copy
   step performs this mapping before any code executes.
3. `CreateThread` on the entry point as a detached thread, then block the
   receiver forever on that thread.

The receiver sets `RMM_SESSION_ID`, `RMM_AGENT_WORKSPACE_ID`, and
`RMM_IN_MEMORY` in its own process before starting the thread, so the agent
registers under the target session and workspace.

## Lifetime

The WinRM shell job object kills the receiver's process tree when the shell
closes, so the in-memory agent lives exactly as long as the shell stays
open. The source agent therefore **never closes** the shell of a successful
deployment: the native command, shell, session, and API handles are moved
into a held-shell registry instead of being torn down, and they are closed
only at source-agent shutdown (`stopHeldWinRMShells`). Consequences:

- The deployed session dies with its source agent process (restart, kill,
  or host reboot of the source).
- The deployed session dies if the target host reboots (no persistence; the
  wmiexec path has the same property).
- Each deployment holds one WinRM shell on the target for its lifetime; mind
  the target's per-user shell quotas.

## In-memory session limitations

The agent runs inside `powershell.exe`, so `os.Executable()` and
`os.Args[0]` name the host process. Operations that relaunch or copy the
agent binary are refused with a clear operator-facing error (guarded by the
`RMM_IN_MEMORY` marker): `spawn`, `spawnas`, `lateral` from the deployed
session, PE plugin execution (socks/tun channels), and persistence install.
Command execution, file operations, exfiltration, and beaconing are intended
to remain available, but have not been validated in this hosting mode.
Redeploy on disk (wmiexec/psexec, staged build) when a session
needs those capabilities.

`Add-Type` compilation transiently uses the .NET CodeDom compiler, which
briefly writes compiler scratch files under the target's `%TEMP%`; the agent
binary itself is never on disk.

## Implementation reference

| Symbol | Responsibility |
| --- | --- |
| `winrmTransfer` | Framed stdin payload: loader + executable, with per-half lengths and SHA-256 |
| `prepareWinRMTransfer` | Validate the local regular file, hash both halves, and build the framed reader |
| `winrmStage1CSharp` | The in-memory loader: stdin receive, digest check, PE mapping, detached thread |
| `winrmStage1Script` | The receiver PowerShell script: stream reads, digest checks, `Add-Type`, launch |
| `winrmReceiverArguments` | Encode the receiver as the `-EncodedCommand` argument of Windows PowerShell |
| `winrmEncodedCommand` | Encode the receiver as UTF-16LE Base64 for PowerShell |
| `winrmPowerShellLiteral` | Quote values as PowerShell data |
| `winrmDeployAgent` | Open the local executable and initialize native WSMan deployment |
| `winrmExecuteWithTLSAPI` | Share existing authentication/TLS setup between fixtures and deployment |
| `wsmanScope.runCommand` | Run the receiver, stream the framed payload, and hold or tear down |
| `wsmanScope.sendInput` | Send bounded binary stdin packets and explicit end of input |
| `winrmHeldShell` / `stopHeldWinRMShells` | Keep successful deployments' handles open until agent shutdown |

## Validation

Portable receiver tests execute PowerShell against fixture inputs when
`pwsh` is available: loader truncation/corruption, agent truncation, digest
mismatch, and oversized input must all fail closed, and the C# loader must
compile through `Add-Type`. Windows native-call fixtures check WSMan send,
hold, teardown, failure propagation, and handle/callback lifetime. Host
tests and Windows cross-builds do not establish a successful remote
in-memory deployment: the PE mapping (`VirtualAlloc`, TLS patching,
relocations) needs native lab validation on a real target. The Windows
identity workflow includes `TestWSMan` and `TestWinRM` tests in normal and
debug builds.

For the native lab acceptance test, block SMB/445 between the source and
target while leaving the chosen WinRM endpoint reachable. Deploy through
WinRM, confirm the new session registers under the expected workspace, and
verify with host monitoring that no agent executable appears on the target's
disk. Also test interrupted transfer, invalid credentials, and HTTPS
certificate rejection; none should trigger SMB fallback or leave artifacts.

Native API reference:
[Microsoft WSManSendShellInput documentation](https://learn.microsoft.com/en-us/windows/win32/api/wsman/nf-wsman-wsmansendshellinput).

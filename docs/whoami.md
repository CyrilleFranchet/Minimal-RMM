# Windows identity report

`/whoami` inspects a Windows Go agent's process and the actual OS thread handling
its command. `/uid` remains the existing short identity summary.

The report includes process PID/parent PID, executable, architecture, inspected
thread ID, configured `make_token` network identity, and separate process and
thread token reports. Without a thread token the process token supplies the
effective identity. A thread-token access failure is explicitly unavailable;
it does not imply that no thread token exists.

Reports use visible process/token section headings and separate lines for
process, token statistics, logon, and ticket fields. Group attributes and
privilege states/descriptions are indented below their names to reduce long
wrapped lines in both operator shells. All collected fields remain visible.
This formatting is produced by the Go agent; rebuild and redeploy existing
agents to use it. Previously stored transcripts retain their original text.

Each token report includes user account/SID; primary or impersonation type;
Windows session ID; elevation type/state, restrictions, linked-token availability,
UAC virtualization, UIAccess and AppContainer state; integrity SID; token,
authentication and modified LUIDs; impersonation level where applicable; groups
and restricted SIDs with enabled/default/deny-only/integrity/logon-ID attributes;
and Windows privilege names, localized descriptions and states. A linked-token
query handle is closed immediately; its identity is not substituted for the
current token.

Privileges distinguish **enabled**, **disabled**, **absent**, **removed**, and
**unsupported on this OS**. Query failures remain **unavailable**. The catalog
covers documented Windows privilege constants and the legacy unsolicited-input
privilege. A disabled privilege is held by the token; an absent privilege is not.
Default-enabled, used-for-access, and raw attribute bits are shown for privileges
present in a token. Descriptions use the host Windows display language.

Logon metadata is read using `LsaGetLogonSessionData` for each token's
authentication LUID: account/domain, SID, logon type, Windows session, authentication
package, logon time, server, DNS domain, and UPN. Fields can be empty, especially
for LocalSystem; permission failures retain their NTSTATUS. The configured
`make_token` identity is a separate label and does not validate credentials or
prove that every OS thread has that identity.

`/whoami --tickets` additionally queries the effective caller's Kerberos ticket
cache through `LsaConnectUntrusted` and `KerbQueryTicketCacheExMessage` with a zero
logon LUID. It reports client/server names and realms, UTC start/end/renewal times,
encryption-type numbers and ticket flags. It does not retrieve encoded tickets,
session keys, or credentials. Query denial, unsupported authentication packages,
and empty caches are explicit. The report does not enumerate unrelated users'
logon sessions or caches.

Collection pins the calling goroutine to its OS thread and requests token query
access only. It does not impersonate, revert, enable privileges, or modify tokens.
Each native handle and LSA buffer is released. Account lookup can depend on domain
availability. The report is a sequence of reads, rather than an atomic system
snapshot, and describes the agent rather than child commands.

## Collector functions and types

- `whoamiAction`: pins the caller, collects process/thread token reports and optional tickets.
- `whoamiPrivilegeCatalog`: documented privilege names plus the legacy constant.
- `whoamiPrivilegeState`: formats held/absent privilege states and attribute bits.
- `whoamiUnicode`, `whoamiLogonData`, `whoamiTicket`: native LSA Unicode,
  logon-session, and ticket-cache metadata layouts respectively.
- `whoamiText`: copies counted LSA UTF-16 strings.
- `whoamiTime`: converts Windows FILETIME timestamps to UTC.
- `whoamiTokenInfo`: allocates a bounded query buffer for one token information class.
- `whoamiSID`: resolves a SID and best-effort account label.
- `whoamiGroups`: reports group/restricted SID membership and security attributes.
- `whoamiPrivileges`: compares token privileges with the catalog and resolves display names.
- `whoamiLogon`: queries metadata for one authentication LUID.
- `whoamiTickets`: queries the effective caller's ticket cache metadata only.
- `whoamiDescribeToken`: composes independent token fields and explicit partial failures.

## Validation

Portable tests cover privilege-state distinctions, catalog uniqueness, command
dispatch and invalid options. Windows tests assert native 32/64-bit layouts,
required report fields, UTC conversion and unchanged process/thread token
statistics and process privilege attributes around a report including tickets.
Host tests and Windows cross-compilation do not establish native Windows runtime
behavior; run `go test -run '^TestWhoami' .` on a Windows test host.

Test entry points: `TestWhoamiPrivilegeStates` checks privilege distinctions and
catalog consistency; `TestWhoamiDispatch` checks valid and rejected command
forms; `TestWhoamiNativeLayouts` checks native ABI offsets and time conversion;
`TestWhoamiReadOnlySnapshot` checks required output and unchanged token state;
`TestWhoamiImpersonatedReadOnlySnapshot` creates a same-account `ImpersonateSelf`
test fixture, repeats the snapshot checks with an actual thread token, then
restores the prior ambient token. The production collector never impersonates.

## Native buffer handling

Token queries preserve the initial Windows API error, retry a growing
`ERROR_INSUFFICIENT_BUFFER` requirement up to four reads, cap token allocations at
1 MiB, and parse only the reported returned length. Query storage has a minimum
of four zeroed bytes. `TokenHasRestrictions` is read from its first byte, which
supports native BOOLEAN results and padded buffers; other scalar fields require
a DWORD. This accommodates native behavior without assuming all token boolean
information classes have identical widths.

Group and privilege headers/counts, handle fields, scalar fields and statistics
are checked before parsing. Token SID pointers must refer to a complete valid SID
inside the returned token allocation. Kerberos responses are capped at 16 MiB;
entry counts and counted Unicode strings must lie within their returned buffer.
Logon-session Unicode descriptors have checked lengths, null pointers, and UTF-16
alignment. `LsaGetLogonSessionData` supplies structure size but no total allocation
length, so its nested strings and SID rely on the operating system's returned
allocation contract after the structure-size check.

Additional helpers: `whoamiScalar` decodes the information-class-specific scalar;
`whoamiQueryTokenInfo` implements bounded query/retry and preserves native errors;
`whoamiBufferSID` validates token-buffer SID membership and size;
`whoamiTextWithin` validates Kerberos string membership in its response.
Additional tests: `TestWhoamiScalarLengths` exercises one-byte, padded and short
scalars; `TestWhoamiTokenQueryBounds` covers original API errors, one-byte BOOLEAN
results, growing sizes, actual returned lengths and malformed responses;
`TestWhoamiNativeBufferBounds` covers short SID headers and invalid/out-of-response
counted Unicode strings. Portable scalar tests run on the development host;
Windows-only native query-helper tests require execution on Windows.

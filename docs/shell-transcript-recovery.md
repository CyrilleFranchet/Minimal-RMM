# Shell transcript recovery

## Session identity

The shell ID is the full RMM session ID. An event ID is only unique within
that session: identify a result by `(session_id, event_id)`, never by its event
number alone. Each tool call supplies its own `session_ref`; discovery resolves
it to a visible full ID and rejects ambiguous hostnames or prefixes.

An AI conversation can originate in session A and explicitly execute a tool
against session B. Its conversation and job remain attached to A; the command
audit and agent output belong to B. Changing the selected browser tab does not
retarget an accepted command or move its results. Open B's shell to inspect that
command and output. The original chat is not a second shell transcript.

WebUI `captureShellTarget` captures the full ID and current view generation
before asynchronous work. Command, transfer, and tool responses update the
visible shell only while that captured view is still current. A delayed response
cannot clear the next shell's input or append output to it. Newly recorded
events and successful execution responses include `session_id` for attribution;
older stored events remain scoped by their containing session history.

Killing a session interrupts jobs attached to that session. It does not cancel
every conversation that previously used that host: a job originating in A
remains attached to A even if it has used B. Further commands to killed B fail.
Remote commands already executing remain subject to cooperative shutdown.

## Recovery

The live and archived shell views retrieve retained session events in ascending
ID order, in pages of 500. The `forward=1` query option on central session and
history event endpoints selects the earliest events after `since`, preserving
the existing latest-events behavior for callers that omit it. Workspace event
queries already return ascending pages.

`RMMServer.get_events` merges persisted history with the live event ring,
deduplicates IDs, and sorts before applying the requested limit. Consequently,
reopening a shell can retrieve older events beyond the 500-event memory ring.
The merge also includes events awaiting their disk write.

The browser's `readShellEvents` helper traverses pages and checks session
selection before rendering each response. `pollSessionEvents` uses it for
reconnection recovery. WebSocket notifications trigger the same ordered recovery
instead of advancing the cursor past notifications delivered out of order.
Session selection restores shell command recall from the recovered transcript.

This covers retained agent session commands and outputs. Exegol and other
local tool operations are separate from the remote agent shell. AI tool result
previews are not the complete shell transcript. Events already lost due to a
workspace broker overflow, deleted history, or failed persistence cannot be
reconstructed by this recovery path. Central workspace mode intentionally does
not persist a second copy of workspace session history.

Validation: `tests/test_shell_recovery.py` covers merging more than 500 events,
out-of-order persisted records, and a partially persisted tail;
`tests/test_shell_recovery.cjs` covers 1,205 events across pages, ordering, and
responses arriving after session selection changes.

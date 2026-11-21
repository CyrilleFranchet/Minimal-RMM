# Session kill lifecycle

Kill removes the session from operator control after central acceptance. The
remote process exits when it next contacts the server; a running command can
delay that contact. An archived session is not proof that its remote process
has already exited.

The WebUI disables Kill and displays “Killing…” while the request is pending.
On success it removes the live row immediately and remembers the killed ID for
the current connection, preventing delayed polling or WebSocket snapshots from
restoring it. Failed requests preserve the live row and display an error.
An accepted kill stays successful if the following list refresh fails; the UI
reports the refresh failure and retains the killed-ID record.
Session and history reads ignore responses superseded by newer requests or a
connection change. The archive list excludes IDs still displayed in the live
list, so independently fetched snapshots never render the same ID in both.

The workspace handles session deletion before returning success to the browser.
`WorkspaceStore.mark_session_killed(session_id)` atomically records the ID in the
SQLite `session_kills` table, archives its local session row, and clears its AI
chat. Session synchronization ignores these durable tombstones, including
snapshots fetched before the kill, service restarts, and deletion of archived
transcripts. Failed central deletions do not create tombstones. Session IDs
are not reused for killed agents. Local pivots are stopped after acceptance.
`WorkspaceStore.is_session_killed(session_id)` checks the durable record before
restoring listeners after restart. `WorkspacePivotManager.end_session(session_id)`
serializes shutdown against listener starts, prevents later starts for that ID,
and attempts both SOCKS and TUN cleanup even when one fails. Cleanup errors are
logged locally and do not turn an accepted kill into an apparent request failure.

Central session broadcasts serialize snapshot capture and publication, so an
in-flight pre-kill broadcast finishes before the kill's updated snapshot.
Both beacon handlers and Kill schedule broadcasts asynchronously, so a slow
operator WebSocket cannot delay the kill response.

Regression coverage: `tests/test_workspace_session_kill.py`,
`tests/test_session_kill_ui.cjs`, and `tests/test_session_kill_ai.py`.
These tests simulate lifecycle races locally; they do not validate native
Windows process shutdown or browser layout.

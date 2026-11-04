# Legacy server storage migration

Single and multiple workspace deployments now use the same storage owner:
the workspace service. Historical server storage must be copied before relying
on the new workspace view. Migration does not change the source directory,
delete evidence, start services, or reassign central sessions.

## Procedure

1. Stop the legacy RMM server and all services writing its data, including CE
   workflow workers. The acknowledgement below is a prerequisite, not automatic
   process detection; copying an actively changing directory is not consistent.
2. Keep the existing data directory as your rollback copy. Prepare deployment
   credentials with `rmm_deployment.py`, but do not start the new workspace yet.
   Use `prepare --preset single --output /path/to/deployment --legacy-settings
   /path/to/old/RMM_logs/settings.json` when saved standalone settings exist.
   This preserves the central beacon secret and public endpoint configuration;
   a fresh deployment otherwise generates a new beacon secret.
3. Run the importer against a new destination, or the prepared workspace
   directory containing only its `settings.json`:

   ```bash
   python rmm_workspace_migration.py \
     --source /path/to/old/RMM_logs \
     --destination /path/to/deployment/workspaces/default \
     --workspace-id default \
     --services-stopped
   ```

4. Review `migration.json`, workspace settings, and the preserved
   `legacy-server/` archive. Prepared connection credentials take precedence
   over compatible settings imported from the old server. Environment-only
   settings are not imported; configure them separately for their new owner.
5. Start the central server and workspace using the deployment instructions.
   Reconnecting agents are handled by central session ownership rules; imported
   historical sessions remain archived until an authoritative workspace sync
   reports them active.

The importer does not populate the new central `sessions.json`. To preserve
per-agent sleep/jitter configuration and persisted ownership, copy that file
from the stopped legacy server to the fresh `deployment/central/` directory
before startup, without overwriting an existing file. Inspect previous workspace
ownership first: a saved owner is retained, not reassigned to the sole workspace.
Otherwise agents reconnect with the new central service's default configuration.

Never point the new workspace directly at the old server data directory.
Migration rejects overlapping trees, symlinks, malformed imported JSON,
conflicting session ownership, and any nonempty destination other than the
prepared settings-only directory. A failed migration removes its newly created
files and restores prepared settings exactly; it never cleans up source data.

## What becomes available

### Legacy global plugin stores

The central global plugin store is retired. Existing `plugin-store` directories,
Docker plugin volumes, and files under the old central `plugins/` directory are
not deleted, but they are no longer a fallback for agent downloads. Preserve them
as rollback data. Install the required releases through each workspace's release
controls or URL asset API, then verify the workspace runtime inventory before
loading a plugin. A file present only in the old global store is not installed
for any workspace.

The new central cache defaults to `RMM_LOG_DIR/plugin-cache/workspaces/<id>/`
(`RMM_PLUGIN_CACHE_DIR` changes its root). Workspaces republish authoritative
plugin assets after restart; do not move the old global directory into this cache.
`RMM_PLUGIN_STORE` is no longer used by Compose. Remove obsolete `RMM_PLUGINS_DIR`
overrides when updating configuration. `RMM_PROFILE_STORE` and `RMM_PROFILES_DIR`
remain valid because beacon transport profiles are centrally owned.

### Imported operator data

| Legacy data | Result |
|-------------|--------|
| `sessions.json`, `history/*/meta.json` | Archived workspace sessions |
| `history/*/events.jsonl` | Workspace transcript events with local artifact links where files exist |
| `history/*/ai_chat.json` | Workspace chat records, preserving stored message metadata |
| `downloads`, `screenshots`, `keylogs` | Workspace artifact directories; session-prefixed downloads indexed |
| `assets` | Workspace assets, including plugins, skills, and rclone profiles; beacon transport bundles remain central |
| Workspace-compatible saved settings | Merged into workspace settings; prepared values win |
| All source files | Preserved under `legacy-server/`; SQLite databases are consistent backups |

Existing BloodHound runs, AI job databases, embedding databases, and UI-state
databases are **archive-only**. Their schemas and absolute artifact references
are not automatically rewritten or resumed. Start new workflow runs; retain
the archived evidence for review. Server-only secrets remain in the private
archive and are not granted to the workspace configuration. Custom paths outside
the source tree are not copied. Review path-valued settings before startup.

SQLite input is cloned temporarily with its WAL or rollback journal and backed
up through SQLite's backup API. Hot rollback journals from an interrupted process
are recovered only in the temporary clone, so uncommitted changes are not imported.
The source is never opened for writing; final backups do not depend on stale
WAL/shared-memory or rollback-journal files. Regular copied files and generated settings
are private to the operator. Ensure sufficient disk space for the archive and
the working artifact copies.

## Implementation and checks

`rmm_workspace_migration.read_object` validates legacy JSON objects;
`copy_storage` preserves regular files and materializes SQLite backups;
`import_history` loads sessions, events, chats, and download indexes;
`migrate` validates paths and manages copy-only rollback; `main` provides the
explicit stopped-service CLI. `tests/test_workspace_migration.py` covers source
preservation, readable imports, existing-target refusal, unsafe paths,
malformed-history rollback, prepared settings, committed WAL recovery, and
recovery of a deliberately interrupted rollback-journal transaction.

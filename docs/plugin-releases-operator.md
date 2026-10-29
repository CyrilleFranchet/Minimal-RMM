# Configure plugin repositories and install releases

Use the same persisted repository/branch configurations from CLI, WebUI, or
MCP. The active server or operator workspace owns the sources and downloads
compiled plugin files. Installed files and source settings survive browser
refreshes and service restarts. See [deployment and integrity](plugin-releases.md)
for storage ownership, credentials, limits, and release publication.

## WebUI

Open **Config → Plugins**. The **Source** selector lists each saved repository and branch.
Choose one and select **Edit source**, or select **New source** to create another.
Both actions open a separate dialog:

- **Source ID** identifies the saved configuration, such as `stable` or `dev`.
- **Label** is an optional display name.
- **Repository** is the GitHub `owner/repository`.
- **Branch** selects that repository's release channel, including names such
  as `feature/test`.
- **Credential environment variable** refers to `RMM_PLUGIN_GITHUB_TOKEN` or
  a service-side variable named `RMM_PLUGIN_TOKEN_NAME`. Enter the variable
  name, never the token. The panel reports whether it is configured.

Select **Save source** or press Enter to persist changes. The dialog closes
after the service confirms the save, selects the saved source, and shows its
label and ID. Failed saves keep the dialog and entered values visible. Cancel
or Escape discards edits and restores the previous selection. Existing IDs are
read-only; New source rejects duplicate IDs. **Remove source** deletes only the
configuration; installed plugin files remain. A different source can use the
same repository with another branch or a different repository entirely.

Enter `latest` or an exact release tag and select **Load plugins**. The
resolved branch and version are displayed. Check one or more plugins and select
**Install selected (N)**. No plugins are checked by default. **Select all** and
**Clear selection** apply to selectable items in the loaded release. Plugins whose
filename and SHA-256 match the runtime inventory are dimmed, labeled
**Installed — SHA-256 matches**, and cannot be checked. Different hashes remain
selectable for updates. Successful installs become disabled immediately; failed
installs and failed synchronization remain available for retry. Downloads run sequentially;
successful items are unchecked and failures remain checked for retry. The request pins the source, source configuration
revision, exact tag, and selected file digest. Changing the source, release
selection, or source form clears the previously browsed release. Unsaved source
edits must be saved before browsing or installing.

Sources and plugin files live on the active workspace/server, rather than in
browser local storage. The general browser configuration export does not
export plugin source records. Use the source API or CLI JSON output to inspect
them. GitHub tokens are never part of source responses or exports.

## CLI

```bash
python rmm_cli.py plugin sources list
python rmm_cli.py plugin sources save stable \
  --repository CyrilleFranchet/Minimal-RMM-plugins --branch main --label Stable
python rmm_cli.py plugin sources save dev \
  --repository team/custom-plugins --branch feature/test \
  --credential-env RMM_PLUGIN_TOKEN_DEV
python rmm_cli.py plugin releases --source dev --tag latest --json
# Copy the exact tag returned above into TAG.
python rmm_cli.py plugin install example.dll --source dev --tag "$TAG"
python rmm_cli.py plugin sources remove dev
```

`--source` defaults to `default`, initially seeded from the service's repository
setting and `main`. Installation requires an exact published tag, not `latest`.
Use `--sha256 <digest>` and `--source-revision <revision>` to pin values returned
when browsing. Legacy commit tags and branch-scoped major/minor release tags
are supported. To roll back, browse and install an older exact tag from the
same source. Source commands and release commands accept `--json`.

The source credential reference must be provisioned in service settings or the legacy environment
before accessing a private repository. Different workspaces can configure
independent sources and credentials. Changing a saved source does not change
an already installed plugin or execute anything on an agent.

## Version display

Versions advance independently per branch: adding a plugin increments the
major number and resets the minor number; modifying a plugin increments the
minor number. Removing or renaming a plugin is a major inventory change.
Documentation-only changes keep the version unchanged. The manifest retains
the source commit for reproducibility; use the exact tag returned by browsing
rather than constructing a tag from the displayed version.

## REST and MCP

| REST | MCP tool | Purpose |
| --- | --- | --- |
| `GET /api/v1/plugin-sources` | `list_plugin_sources` | Inspect saved repository/branch configurations |
| `POST /api/v1/plugin-sources/{id}` | `save_plugin_source` | Add or update a source |
| `DELETE /api/v1/plugin-sources/{id}` | `remove_plugin_source` | Remove configuration without deleting binaries |
| `GET /api/v1/plugin-releases?source=dev&tag=latest` | `list_plugin_release` | Resolve the selected source's release |
| `POST /api/v1/plugin-releases/install` | `install_plugin_release` | Install one pinned plugin |

A source save body contains `repository`, `branch`, `label`, and
`credential_env`. An install body contains `name`, `tag`, `source`, and optional
`sha256` and `source_revision`. Normal operator API authentication applies.
The WebUI includes both optional pins automatically. A failed workspace runtime
synchronization is distinct from successful local persistence; retry the same
installation to republish the saved file.

## Operator implementation reference

| Function or method | Responsibility |
| --- | --- |
| `RmmApiClient.list_plugin_sources` | Read persisted source configurations |
| `RmmApiClient.save_plugin_source` | Save repository/branch and a credential reference |
| `RmmApiClient.remove_plugin_source` | Delete source configuration |
| `RmmApiClient.list_plugin_release` | Encode a tag and source ID in a browse request |
| `RmmApiClient.install_plugin_release` | Submit an exact tag and optional digest/configuration pins |
| `cmd_plugin_sources` | Dispatch CLI source CRUD and JSON output |
| `cmd_plugin_release` | Dispatch CLI browsing/installation and version display |
| `tool_list_plugin_sources`, MCP `list_plugin_sources` | Share persisted source enumeration |
| `tool_save_plugin_source`, MCP `save_plugin_source` | Share source upsert with credential names only |
| `tool_remove_plugin_source`, MCP `remove_plugin_source` | Share source deletion |
| `tool_list_plugin_release`, MCP `list_plugin_release` | Share source-specific release browsing |
| `tool_install_plugin_release`, MCP `install_plugin_release` | Share pinned installation and preserve failure details |
| Web `status` | Render accessible status/error text |
| Web `source`, `selected` | Resolve the saved source and checked manifest plugins |
| Web `syncControls` | Apply busy/dirty state and show the selection count and checksum verification guidance |
| Web `clearRelease` | Invalidate stale release selections |
| Web `editSource`, `newSource` | Populate or clear the source form |
| Web `loadSources` | Refresh persisted sources, preserving the selected ID when available |
| Web `sourceListStatus` | Show source loading, empty, success, and error feedback beside the selector |
| Web `renderSources` | Render saved source labels, IDs, repositories, and branches, preserving the selected ID |
| Web `openSourceEditor`, `cancelSourceEditor` | Open the edit dialog or discard drafts and restore the previous selection |
| Web `saveSource`, `removeSource` | Persist or delete configuration through authenticated APIs |
| Web `browse` | Render the selected source's exact release and version |
| Web `markInstalled` | Uncheck and disable a matching installed plugin with a visible checksum-match label |
| Web `install` | Install checked items sequentially with source, revision, tag, and digest pins; retain failures for retry and refresh installed assets |
| Web `bind` | Connect form controls and stale-selection handling |

`tests/test_plugin_release_surfaces.py` checks source CRUD requests, branch and
configuration pins, mutable-tag rejection, and shared-tool error propagation.
The UI test `fixture` supplies a DOM adapter and mock persistent API; its
cases cover configuration add/edit/reload/delete, stale selections, secret-value
rejection, pinned installs, missing configuration revisions, failed browsing,
and partial synchronization errors.

## Configuration layout and authentication feedback

The dialog separates **Plugins**, **Installed assets and manual uploads**,
**Beacon transport**, and **Import and export settings**. All sections
start collapsed every time Config opens. Source editing uses its own modal dialog. The dialog scrolls within the
viewport, and plugin rows use labeled native checkboxes for keyboard access.

Credential presence appears below the source selector while the source editor
is closed. Presence means only that the service has a nonempty credential;
it does not prove GitHub access. Enter the referenced credential in
**Service settings**, or keep a legacy environment override. Restart the
storage-owning service after changing its credentials. The source editor
contains only the credential reference name. See [credential setup](plugin-releases.md#configure-the-storage-owner).

The checklist tests cover explicit selection, clearing selection, per-file
checksum pins, partial batch failures, and retrying only failed items, in
addition to source CRUD and stale selection coverage. No live OAuth access is
validated by these tests.

### Default repository and reload failures

The backend still seeds `default` with `CyrilleFranchet/Minimal-RMM-plugins`
and branch `main` when the source store is first created. `RMM_PLUGIN_REPOSITORY`
overrides that initial seed. Existing stores are never overwritten or silently
reseeded after deletion. New source prefills the standard repository as an
editable suggestion; it does not save anything until submitted.

Saving uses the source returned by the successful POST immediately, without a
second GET that could hide a successful save. Refresh failures retain previously
loaded rows and display an error. Source API errors show the service detail when
available, including directions to use the workspace URL. Backend tests cover
saved sources surviving store reopen; UI tests cover submit, explicit success,
failed refresh, failed save, duplicate IDs, and cancel behavior.

### Source list loading

Opening Config refreshes sources independently of session and history loading.
The status directly below the source selector distinguishes loading, a confirmed
empty store, and request failures. An empty store offers New source; a failed
request shows its error and a retry placeholder when no previous rows exist.
Malformed responses are errors, not empty stores. Existing rows survive failed
refreshes. The UI never recreates a deleted default source automatically.

### Installed checksum comparison

Loading a release also reads `GET /api/v1/plugins`. The existing server inventory
hashes the actual runtime files; workspace requests use the workspace-visible
central inventory. Comparing both filename and digest prevents a successful
local download with a missing or outdated runtime copy from being treated as
deployed. Matching content already available through the workspace inventory
counts as installed even if it comes from the shared central plugin directory.

This is runtime availability, not proof that a plugin has executed on an agent.
If inventory lookup fails, the UI reports that comparison is unavailable and
keeps installation possible. Load plugins again after external changes, asset
deletion, or enabling/disabling assets to refresh the comparison.

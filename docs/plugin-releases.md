# Install compiled plugins from private GitHub releases

The plugin repository builds and publishes a versioned GitHub Release after
updates to its `main` branch. RMM downloads compiled files from that private
release; it needs neither a source checkout nor a compiler. Existing binaries
are reused when their build inputs are unchanged. See the
[publication workflow](https://github.com/CyrilleFranchet/Minimal-RMM-plugins/blob/main/docs/compiled-artifacts.md).

## Saved repositories and branches

Configure sources in **Configuration → Compiled plugins from GitHub**. Each
named source selects a GitHub `owner/repository`, branch, display label, and
credential environment variable. Sources can select different repositories or
different branches of the same repository. See the
[operator guide](plugin-releases-operator.md) for add/edit/delete controls and
equivalent CLI/MCP commands.

Settings persist in `plugin-sources.json` under the active asset store, with
owner-only permissions and atomic writes. Workspace mode keeps them in each
user's workspace. Server mode has one shared operator-owned configuration.
The browser is not the source of truth. Removing a source does not delete
installed files. Existing installations are migrated by seeding `default`
from `RMM_PLUGIN_REPOSITORY` and branch `main` once; later environment changes
do not overwrite saved settings or recreate a deleted default source.

Selecting `latest` resolves the highest numeric published version in the
selected branch's tag namespace. A development source never falls back to
main. A branch must already have a release using the compatible manifest;
adding a source in RMM does not start CI or publish another repository.
The plugin pipeline publishes `main` automatically and permits explicit manual
dispatch on other branches, keeping feature-branch builds opt-in.

Runtime filenames are shared within a workspace/server. Installing the same
plugin name from another source replaces that installed version; sources do
not create parallel runtime namespaces. Installing a release remains explicit.

## Simple branch versions

Each branch starts at **1.0**, independently of other branches:

| Change | Example |
| --- | --- |
| Add a published plugin filename | `1.4` → `2.0` |
| Modify plugin code, build inputs, or release metadata | `1.4` → `1.5` |
| Remove or rename a published plugin filename | `1.4` → `2.0` |
| Documentation only, without a plugin change | Keep `1.4`; no new release |

An inventory change takes precedence if a merge both adds and modifies files.
This is the requested two-number release policy, not compatibility-based
Semantic Versioning. Versions describe the complete plugin collection on a
branch, not individual files. File digests and original build commits remain
in the manifest when unchanged binaries are reused.

Release tags use `plugins-<branch-key>-v<major>.<minor>`, where `branch-key` is
the first twelve hexadecimal characters of SHA-256 of the exact branch name.
For example, main version 1.0 is `plugins-0d6e4079e367-v1.0`. This lets branches
use the same readable version without colliding in GitHub's tag namespace.
The manifest retains `source_branch`, `source_commit`, and `version`.
Legacy `plugins-<commit SHA>` releases remain installable from main sources.

## Configure the storage owner

Set credentials on the process that owns the plugin store: the workspace
service in workspace mode, or the RMM server in server mode.

```bash
export RMM_PLUGIN_REPOSITORY=CyrilleFranchet/Minimal-RMM-plugins
# Supply RMM_PLUGIN_GITHUB_TOKEN through your process environment/secret manager.
export RMM_ASSET_MAX_BYTES=134217728
```

Use a fine-grained GitHub token scoped to the private plugin repository with
**Contents: read**. GitHub App installation tokens also work; the deployment
must refresh them when they expire. RMM does not mint or rotate tokens. See
[GitHub release asset authentication](https://docs.github.com/en/rest/releases/assets).
The service's Python installation needs a working CA trust store. On a Mac
where Homebrew Python points at a missing OpenSSL certificate bundle, set
`SSL_CERT_FILE=/etc/ssl/cert.pem` in the service environment or repair that
Python installation's CA configuration. Keep TLS verification enabled.
`RMM_PLUGIN_REPOSITORY` is only the initial default source seed. Configure all
subsequent repository and branch changes in the UI, CLI, or source API.
Credential references are restricted to `RMM_PLUGIN_GITHUB_TOKEN` and dedicated
names such as `RMM_PLUGIN_TOKEN_DEV`; arbitrary service secrets cannot be used
as GitHub credentials. Multiple sources can share a credential reference.
The browser and CLI send source settings and release selections, never tokens.
GitHub credentials are never sent to the agent, central broker in workspace
mode, browser configuration, or plugin manifest.

For Compose, `.env.example` provides `RMM_WORKSPACE_PLUGIN_GITHUB_TOKEN` and
`RMM_SERVER_PLUGIN_GITHUB_TOKEN`; populate only the storage owner's value.
Compose maps that value to `RMM_PLUGIN_GITHUB_TOKEN` in the selected service.
Protect the environment file and exclude it from source control. Each separately
deployed workspace can use its own repository credential.
For additional named credentials in Compose, explicitly pass each variable
into that workspace service's `environment` map, for example
`RMM_PLUGIN_TOKEN_DEV: ${RMM_PLUGIN_TOKEN_DEV:-}`. Merely adding it to `.env`
does not expose it to the container. The UI reports credential presence only.

The largest current plugin is about 75 MB. Set `RMM_ASSET_MAX_BYTES=134217728`
at the storage owner and `RMM_MAX_BODY_BYTES=134217728` on the central server
for base64 workspace synchronization. `RMM_WORKSPACE_MAX_BODY=134217728` also
allows equivalent manual uploads. Existing lower limits fail explicitly; they
are not silently bypassed.

## CLI

Browse the current published release:

```bash
python3 rmm_cli.py plugin releases --source default --tag latest
```

Copy the returned exact published tag, then install
one plugin. `TAG` below is that exact returned value:

```bash
python3 rmm_cli.py plugin install document-embeddings-autonomous.dll --source default --tag "$TAG"
```

Use `--sha256` with the displayed digest to pin the selection as well. Install
other plugins by name from the same manifest. Listing `latest` resolves it to
an exact release; installation rejects `latest` so a moving release cannot
silently change the selected version. Choose an older exact tag to roll back.
Use the existing CLI URL/token options to connect to your local workspace or
server. Installation adds the plugin to the runtime store; execution remains
an explicit operator action through existing plugin commands.

## WebUI

In the assets configuration panel, choose a saved source, enter `latest` or an exact release tag,
click **Browse release**, select a plugin, and click **Install selected plugin**.
The panel displays the repository, branch, version, size, and expected SHA-256.
It pins the source configuration revision, selected tag, and digest when
installing, and reports a verified install,
cache reuse, or a download/verification error. Refreshing the browser does not
remove installed plugins; the persistent asset store is authoritative.

In workspace mode, files persist under the workspace data directory's
`assets/plugins/` and synchronize to the workspace-specific runtime cache on
the central server. A synchronization error reports that the local install
succeeded; retry installation to republish it. Existing workspace startup
synchronization also republishes persisted files. The central server rejects
these release endpoints for workspace mode and workspace-scoped credentials.
In server mode, files persist under the server asset store and are atomically
copied to `RMM_PLUGINS_DIR`. Existing enabled/disabled settings are preserved.

## API and MCP

| Operation | REST | MCP |
| --- | --- | --- |
| List sources | `GET /api/v1/plugin-sources` | `list_plugin_sources` |
| Save source | `POST /api/v1/plugin-sources/{id}` | `save_plugin_source` |
| Remove source | `DELETE /api/v1/plugin-sources/{id}` | `remove_plugin_source` |
| Resolve and browse | `GET /api/v1/plugin-releases?source=default&tag=latest` | `list_plugin_release` |
| Install one selected binary | `POST /api/v1/plugin-releases/install` | `install_plugin_release` |

Install body: `{"source":"default","tag":"plugins-0d6e4079e367-v1.0","name":"example.dll","sha256":"<digest>","source_revision":"<revision>"}`.
The digest and configuration revision are optional for API/CLI callers and
included by the WebUI. A stale configuration revision is rejected before
fetching or replacing a binary. Source saves accept `repository`, `branch`,
`label`, and `credential_env`; tokens and unknown fields are rejected.
Success returns `asset`, `release`, and `cached`. A `502` with
`local_installed: true` means verified local persistence succeeded but runtime
synchronization failed. No install endpoint queues execution on an agent.
All routes use the normal operator API authentication.

## Integrity and availability

Only published, non-prerelease releases with a matching branch/version and
full manifest source commit are accepted. Legacy tag commits must match their
manifest commit. Asset filenames must be flat DLL or PS1
names; every binary must match its manifest size and SHA-256 before replacing
an installed file. When GitHub supplies an asset digest it is checked too.
Authentication is sent only to `api.github.com`; HTTPS redirects to GitHub's
asset hosts drop authorization. Download errors omit tokens and signed URLs.

An installed binary with the same digest is reused without downloading it
again. Release metadata and the manifest are still fetched to validate the
selection. Already installed plugins remain usable during a GitHub outage,
but new installation requests require access to release metadata. A checksum
protects integrity relative to the trusted repository; it is not an independent
publisher signature. Repository writers remain trusted publishers.

Release assets avoid the retention expiry of Actions artifacts. Failed builds
must not publish a partial release or change the previous usable release.
Normal RMM updates do not compile plugins. Upgrading an installed plugin is
explicit; publishing a new release does not automatically replace live stores
or restart running plugin processes.

## Implementation reference

| Symbol | Responsibility |
| --- | --- |
| `ReleaseError` | Safe download/validation failure without credentials |
| `ReleaseRedirect.redirect_request` | Restrict redirect hosts and strip authorization |
| `PluginReleases.__init__` | Load repository and process-scoped credential |
| `PluginReleases._latest_tag` | Resolve the selected branch's highest numeric version |
| `PluginReleases._request` | Bounded GitHub API/content request and safe errors |
| `PluginReleases._asset` | Asset-ID download with size/GitHub digest validation |
| `PluginReleases.resolve` | Resolve tag and validate manifest/asset correspondence |
| `PluginReleases.install` | Verify or reuse bytes and persist through `AssetStore` |
| `plugin_release_api` | Shared browse/install/error behavior for both owners |
| `RMMHandler._handle_plugin_releases` | Enforce server ownership and route operations |
| Nested `publish` callback | Verify stored digest and atomically publish runtime bytes |
| `PluginSourceError` | Report safe source validation/storage errors |
| `normalize_source` | Validate IDs, repositories, branches, and dedicated credential references |
| `public_source` | Return settings, revision fingerprint, and credential presence without secrets |
| `PluginSourceStore.__init__` | Initialize owner-local persistent settings and seed the default once |
| `PluginSourceStore._read`, `_write` | Validate stored records and write them atomically with private permissions |
| `PluginSourceStore.list`, `get` | Read public settings at the active storage owner |
| `PluginSourceStore.save`, `remove` | Upsert/delete one source without changing installed files |
| `plugin_sources_api` | Share source CRUD and error handling across server/workspace routes |
| `RMMHandler._handle_plugin_sources` | Enforce central storage ownership for source configuration |

The CLI, MCP, and browser controller use these shared endpoints; their source
contains function docstrings and the parity checker tracks both MCP tools.

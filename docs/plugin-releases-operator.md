# Install compiled plugins from GitHub

Use the same release deployment flow from CLI, WebUI, or MCP. The active RMM
server or operator workspace downloads the plugin from the configured private
GitHub repository, verifies its manifest checksum, and installs it into its
asset store. Installing a plugin does not execute it on a managed host.

Configure `RMM_PLUGIN_REPOSITORY` and `RMM_PLUGIN_GITHUB_TOKEN` in that service's
environment. The default repository is `CyrilleFranchet/Minimal-RMM-plugins`.
The browser, CLI, and agent do not receive the GitHub credential.
See [release deployment and service configuration](plugin-releases.md) for
the publication pipeline, authentication, validation, and storage details.

## CLI

```bash
python rmm_cli.py plugin releases --tag latest --json
# Copy the exact plugins-<40-character commit SHA> tag from the response.
python rmm_cli.py plugin install document-embeddings-autonomous.dll \
  --tag plugins-0123456789abcdef0123456789abcdef01234567
```

The example tag is a placeholder. Installation requires a real exact release
tag, not `latest`. Add `--sha256 <digest>` to pin the digest shown by the browse
command as well. Each command accepts `--json`. To choose an older version,
browse its exact tag and install from that tag.

## WebUI

Open **Configuration → Runtime assets and beacon transport → Compiled plugins
from GitHub**. Enter `latest` or an exact release tag and select **Browse
release**. Review the resolved tag and plugin checksum, choose a plugin, then
select **Install selected plugin**. The request includes the exact resolved tag
and selected digest. Editing the tag clears the previous selection.

Installed files appear in the managed asset list and persist in the active
server or workspace. A workspace synchronization error is reported separately
from a successful local installation. Retry the same installation to retry
synchronization. Use the existing asset controls to enable, disable, or remove
an installed plugin.

## REST and MCP

| REST | MCP tool | Purpose |
| --- | --- | --- |
| `GET /api/v1/plugin-releases?tag=latest` | `list_plugin_release(tag="latest")` | Resolve a release and inspect its compiled plugins |
| `POST /api/v1/plugin-releases/install` | `install_plugin_release(name, tag, sha256="")` | Install one plugin from an exact release; optional digest pin |

The install JSON contains `name`, `tag`, and optionally `sha256`. Use the normal
operator API authentication. GitHub credentials are configured separately in
the service environment and are not API parameters.

## Operator implementation reference

| Function or method | Responsibility |
| --- | --- |
| `RmmApiClient.list_plugin_release` | Encode a tag in the release browse request |
| `RmmApiClient.install_plugin_release` | Submit an exact tag and optional checksum pin |
| `cmd_plugin_release` | Dispatch CLI browsing/installation and format output |
| `tool_list_plugin_release`, MCP `list_plugin_release` | Share release browsing with AI and MCP clients |
| `tool_install_plugin_release`, MCP `install_plugin_release` | Share pinned installation and preserve failure details |
| Web `status` | Render accessible status/error text |
| Web `selected` | Resolve the plugin selected from the inspected manifest |
| Web `syncControls` | Apply busy state and display the expected digest |
| Web `clearRelease` | Invalidate stale selections when the tag changes |
| Web `browse` | Fetch and render a release with an exact source tag |
| Web `install` | Submit the selected tag/digest and refresh installed assets |
| Web `bind` | Connect controls to their handlers |

`tests/test_plugin_release_surfaces.py` checks CLI requests, mutable-tag
rejection, and shared-tool failure propagation. Its `PluginReleaseSurfaceTests`
methods cover inspect/install requests, rejection of `latest`, and preservation
of a failed workspace synchronization response. The UI test `fixture` provides
a minimal DOM and mock API; its four cases check pinned installation, stale
selection invalidation, partial synchronization failure, and browse failure.

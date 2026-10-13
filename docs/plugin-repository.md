# External agent plugin repository

This page defines the repository split for agent plugins. The runtime already
separates plugin **artifacts** from the server through `/app/plugins`. The
private [`CyrilleFranchet/Minimal-RMM-plugins`](https://github.com/CyrilleFranchet/Minimal-RMM-plugins)
repository now owns the plugin source history, tests, build pipeline, and
release bundle. Minimal-RMM is being migrated to consume those artifacts from
the runtime store.

## Decision

The Minimal-RMM repository remains the control-plane and agent-host repository.
A separate plugin repository owns the source, tests, build pipeline, release
manifests, and documentation for agent-side plugins.

| Responsibility | Minimal-RMM repository | Plugin repository |
| --- | --- | --- |
| Python control plane and operator API | Yes | No |
| PowerShell and Go beacon protocol | Yes | No |
| Generic Go PE loader and plugin ABI contract | Yes | No |
| `server-tun/` and `rmm-tun` helper | Yes; server-side helper | No |
| Go agent plugin source | No | Yes |
| PowerShell script plugin source | No | Yes |
| Plugin-specific tests | Loader contract tests only | Yes |
| Windows cross-compilation and release artifacts | No | Yes |
| Runtime plugin artifacts | Mounted into the server volume | Published as a release bundle |

`server-tun/` stays in Minimal-RMM because it creates the operator-side TUN
device and is part of the server or workspace data plane. It is not code that
is delivered to the managed host. The `tun` package in the external repository
is a different component: it is the privilege-free target-side transport.

## Plugin repository layout

The separate repository should keep one independently testable package per
plugin and a common release contract:

```text
Minimal-RMM-plugins/
├── rclone-exfil/              # Go PE plugin, exports Run
├── screenshot/                # Go PE plugin, exports Run
├── socks/                     # persistent Go pivot plugin
├── tun/                       # persistent Go pivot plugin
├── screenshot-powershell/     # PowerShell script plugin
├── socks-powershell/          # PowerShell script plugin
├── manifests/
│   └── plugins.json
├── scripts/
│   ├── build-bundle.sh
│   └── write-manifest.py
├── Makefile
└── dist/
    ├── *.dll
    ├── *.ps1
    └── plugins.json
```

The exact directory names can change, but the release outputs must remain
stable for the server and agent protocol. A release manifest should include at
least:

```json
{
  "name": "tun.dll",
  "version": "0.1.0",
  "kind": "persistent-agent-plugin",
  "target": "windows-amd64",
  "abi": "minimal-rmm-pe-v1",
  "exports": ["Run"],
  "capabilities": ["tun-plugin"],
  "sha256": "...",
  "min_agent_version": "..."
}
```

PowerShell plugins should use the same manifest with a script kind, supported
agent type, entrypoint, and checksum. The current server enforces safe artifact
names and the `.dll` / `.ps1` extensions; deployment automation should use the
manifest to select and verify files, and the plugin volume should not be used
as an arbitrary file server.

## Deployment contract

The plugin repository produces a release bundle. A deployment step copies the
bundle into the runtime plugin store:

```text
plugin CI release
        │
        ▼
operator or deployment automation
        │ atomic copy / rename
        ▼
RMM_PLUGIN_STORE or Docker volume mounted at /app/plugins
        │
        ▼
server_rmm.py inventories and serves artifacts
        │ authenticated download
        ▼
Go or PowerShell agent loads the selected plugin
```

The server does not need the plugin source repository, Go toolchain, mingw-w64,
or plugin-specific dependencies at runtime. Adding or replacing a one-shot
plugin artifact remains hot-loadable. A running SOCKS or TUN child keeps the
artifact it loaded at startup; restart that pivot to use a replacement.

For a named Docker volume, the deployment automation needs a controlled way to
copy the release bundle into the volume. For a host bind mount, set
`RMM_PLUGIN_STORE` and copy the bundle into that directory. Both paths should
write a temporary filename and rename it into place so the server never sees a
partially written DLL or script.

## Boundary implemented in Minimal-RMM

The split is now implemented with these boundaries:

| Current coupling | Target change |
| --- | --- |
| `Dockerfile` | Builds only the server-side `rmm-tun` helper; it has no plugin source or cross-compiler stage |
| `run_server.py` | Builds `rmm-tun` and reports artifacts already installed in `RMM_PLUGINS_DIR` |
| `server_rmm.py` | Inventories and serves runtime artifacts; it has no plugin compiler |
| `plugin_script_path()` | Resolves approved PowerShell artifacts from the runtime store |
| REST, CLI, MCP, and WebUI | Expose inventory and plugin loading; build commands live in the plugin repository |
| Tests | Server tests use temporary runtime artifacts; plugin behavior tests live with each external plugin |
| Feature docs | Refer to the external repository and runtime artifact contract |

The generic PE loader, `__PE_LOAD__`, `__PLUGIN_COMMAND__`, persistent lifecycle
flags, and `/tools/plugins/<name>` endpoint remain in Minimal-RMM. Those are
protocol contracts between the server and the agent.

## Remaining deployment verification

1. Build a Windows bundle in the external repository's CI or on a host with
   `x86_64-w64-mingw32-gcc`.
2. Install the bundle's `*.dll` and `*.ps1` files through `RMM_PLUGIN_STORE`.
3. Verify `GET /api/v1/plugins`, a PowerShell plugin fetch, a Go one-shot load,
   and SOCKS/TUN pivot startup against a lab agent.
4. Keep the manifest with the release metadata; the current server uses the
   artifact names and checksums returned by its own runtime inventory.

## Related documentation

- [Architecture overview](architecture.md)
- [Dynamic plugin runtime](plugin-architecture.md)
- [Go agent plugin ABI](go-agent-plugins.md)
- [Server startup and Docker store](server-startup.md)
- [Workspace deployment](workspace-deployment.md)

# Minimal-RMM documentation

This directory is the repository wiki for Minimal-RMM. Start with the
[deployment guide](deployment.md) for a complete installation path, then use
the [architecture overview](architecture.md) and feature references as needed.

The documentation has five roles:

- `architecture.md` is the canonical description of the current runtime
  topology, data flow, storage ownership, and privilege boundaries.
- `deployment.md` is the canonical installation path, through the deployment wrapper only, with single and distributed same-host
  topologies; cross-host overlay work is deferred.
- Feature pages describe setup, operator behavior, protocols, and code
  ownership for one subsystem.
- `progress.md` is the chronological work log. It records decisions and
  validation history, but it is not the runtime source of truth.
- `tech-plan.md` is the roadmap and design backlog. Planned behavior there is
  not necessarily implemented.

When adding a page, add it to the relevant table below and link back to this
index. Keep current behavior in the architecture or feature pages, and put
historical notes in `progress.md`.

## Start here

| Goal | Page |
| --- | --- |
| Review architecture defects, regression coverage, and remaining validation gaps | [Architecture review](architecture-review.md) |
| Review RMM, Profiles, and Plugins together | [Cross-repository review](cross-repository-review.md) |
| Inspect CLI, MCP, and browser lifecycle findings | [Operator surface review](operator-surface-review.md) |
| Configure services without maintaining environment-variable lists | [Persistent service settings](service-settings.md) |
| Choose a setup and deploy without mixing server/workspace settings | [Deployment guide](deployment.md) |
| Understand the complete system | [Architecture overview](architecture.md) |
| Find the supported launcher and compatibility reference | [Server startup reference](server-startup.md) |
| Stop services and understand cleanup guarantees | [Service shutdown](shutdown.md) and [agent shutdown](agent-shutdown.md) |
| Understand workspace claims, storage, and protocol details | [Workspace reference](workspace-deployment.md) |
| Install plugins and rclone files; preserve legacy skill assets | [Operator-managed assets](operator-assets.md) |
| Install and operate Cloudflared or another connector | [RMM-Profiles](rmm-profiles.md) |
| Use the API, CLI, WebUI, and MCP surfaces | [Root README](../README.md) and [operator parity](mcp-parity.md) |
| Check what is implemented and what remains | [Progress log](progress.md) |

## Architecture and boundaries

| Topic | Page |
| --- | --- |
| Runtime topology, channels, storage, privileges, and rebuild rules | [Architecture overview](architecture.md) |
| Separate administration and agent beacon listeners | [RMM-Profiles](rmm-profiles.md) |
| Dynamic plugin volume and plugin-owned commands | [Plugin architecture](plugin-architecture.md) |
| URL-installed plugins, legacy skill assets, and rclone files | [Operator-managed assets](operator-assets.md) |
| External repository for plugin source and releases | [External plugin repository](plugin-repository.md) |
| Install compiled private releases from CLI, WebUI, or MCP | [Plugin releases](plugin-releases.md) |
| Go agent PE loader and plugin ABI | [Go agent plugins](go-agent-plugins.md) |
| SOCKS and TUN plugin split | [Pivot plugins](pivot-plugins.md) |
| Multi-operator storage and Exegol ownership | [Workspace deployment](workspace-deployment.md) |
| REST, CLI, MCP, WebUI, and AI operator parity | [MCP operator parity](mcp-parity.md) |

## Deployment and agents

| Topic | Page |
| --- | --- |
| Supported service launcher | [Server startup](server-startup.md) |
| RMM-Profile volume and Cloudflared Quick/named tunnels | [RMM-Profiles](rmm-profiles.md) |
| Go agent generation, targets, and runtime settings | [Go agent](go-agent.md) |
| Build the main agent with inherited diagnostics and investigate cleanup | [Agent debug tracing](agent-debug.md) |
| PowerShell agent generation | [PowerShell agent generator](web-agent-generator.md) |
| Hidden command execution and shell dispatch | [Client command execution](client-command-execution.md) |
| Persistent session and artifact storage | [Session history](session-history.md) |
| Shell identity, session switching, and complete retained event recovery | [Shell transcript recovery](shell-transcript-recovery.md) |
| Versioned WebUI settings export/import | [WebUI configuration](web-ui-configuration.md) |

## Pivots and agent plugins

| Topic | Page |
| --- | --- |
| TUN device, routes, helper, and Exegol usage | [TUN pivot](tun-pivot.md) |
| WebUI pivot controls and relay inventory | [WebUI SOCKS and TUN](web-socks.md) |
| Agent-side SOCKS/TUN packages and task protocol | [Pivot plugins](pivot-plugins.md) |
| Generic Go plugin delivery and command dispatch | [Go agent plugins](go-agent-plugins.md) |
| Dynamic artifact installation and hot loading | [Plugin architecture](plugin-architecture.md) |
| Plugin source repository and release process | [External plugin repository](plugin-repository.md) |

## Agent capabilities

| Capability | Page |
| --- | --- |
| Saved document embedding profiles, CLI/WebUI, and result export | [Document embeddings](document-embeddings.md) |
| rclone exfiltration modes | [rclone exfil](rclone-exfil.md) |
| PowerShell and Go screenshots | [Go agent](go-agent.md) and [External plugin repository](plugin-repository.md) |
| Downloaded files browser | [Downloads browser](downloads-browser.md) |

## WebUI and AI

| Topic | Page |
| --- | --- |
| Shell history, completion, and local meta commands | [Web shell completion](web-shell-completion.md) |
| Full result retrieval after WebSocket truncation | [Live full results](web-live-full-results.md) |
| Provider validation and request format | [AI providers](ai-providers.md) |
| Model selection | [AI model selection](web-ai-models.md) |
| Single playbook, downloadable template, context limits, and troubleshooting | [AI playbooks](web-ai-skills.md) |
| BloodHound CE collection, ingestion, and candidate paths | [BloodHound workflow](bloodhound-workflow.md) |
| BloodHound technical validation and operator parity | [Audit findings](bloodhound-technical-audit.md) |
| Validate a live Exegol MCP endpoint (deployment/dev diagnostic) | [Exegol MCP verification](exegol-mcp-verification.md) |
| Move legacy standalone storage into a workspace | [Migration guide](workspace-migration.md) |
| Legacy skill asset API (no automatic chat loading) | [Operator-managed assets](operator-assets.md) |
| Per-session chat persistence | [AI chat memory](web-ai-chat-memory.md) |
| Light and dark theme | [WebUI theme](web-ui-theme.md) |
| Server-side logging of operator WebUI connections | [WebUI connection logging](webui-connection-logging.md) |

## Product and planning

| Topic | Page |
| --- | --- |
| Product goals and commercial security gate | [Product requirements](prd.md) |
| Planned work and feature designs | [Technical plan](tech-plan.md) |
| Completed work, open gaps, and validation notes | [Progress log](progress.md) |

## Quick navigation by code area

For the on-demand investigation library, see
[Claude-Red adaptations and attribution](claude-red-adaptations.md).
For supported graph investigations, see the
[BloodHound query catalog](bloodhound-query-catalog.md).

| Code area | Primary documentation |
| --- | --- |
| `server_rmm.py`, `rmm_socks.py`, `rmm_tun.py` | [Architecture](architecture.md), [TUN pivot](tun-pivot.md), [WebUI pivots](web-socks.md) |
| `rmm_profile.py`, `RMM_PROFILES_DIR` | [RMM-Profiles](rmm-profiles.md) |
| `rmm_asset_store.py`, `RMM_ASSETS_DIR` | [Operator-managed assets](operator-assets.md) |
| `workspace_service.py`, `workspace_pivot.py`, `rmm_workspace.py` | [Workspace deployment](workspace-deployment.md), [Architecture](architecture.md) |
| `agent-go/` | [Go agent](go-agent.md), [Go agent plugins](go-agent-plugins.md) |
| External `Minimal-RMM-plugins` repository | [External plugin repository](plugin-repository.md), [Plugin architecture](plugin-architecture.md) |
| `web/` | [WebUI configuration](web-ui-configuration.md), [WebUI pivots](web-socks.md), [AI pages](ai-providers.md) |
| `rmm_cli.py`, `rmm_tools.py`, `mcp_rmm_server.py` | [Operator parity](mcp-parity.md) |
| `Dockerfile`, `rmm_deployment.py`, `docker-entrypoint.sh` | [Server startup](server-startup.md), [Workspace deployment](workspace-deployment.md), [Plugin architecture](plugin-architecture.md) |

The full API and command reference remains in the [root README](../README.md).

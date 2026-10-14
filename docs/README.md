# Minimal-RMM documentation

This directory is the repository wiki for Minimal-RMM. Start with the
[architecture overview](architecture.md), then choose the deployment and
feature guide that matches the work you are doing.

The documentation has four roles:

- `architecture.md` is the canonical description of the current runtime
  topology, data flow, storage ownership, and privilege boundaries.
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
| Understand the complete system | [Architecture overview](architecture.md) |
| Start the local server and build helpers | [Server startup](server-startup.md) |
| Run the Docker control plane and an Exegol workspace | [Workspace deployment](workspace-deployment.md) |
| Install plugins, AI skills, and rclone files | [Operator-managed assets](operator-assets.md) |
| Install and operate Cloudflared or another connector | [RMM-Profiles](rmm-profiles.md) |
| Use the API, CLI, WebUI, and MCP surfaces | [Root README](../README.md) and [operator parity](mcp-parity.md) |
| Check what is implemented and what remains | [Progress log](progress.md) |

## Architecture and boundaries

| Topic | Page |
| --- | --- |
| Runtime topology, channels, storage, privileges, and rebuild rules | [Architecture overview](architecture.md) |
| Separate administration and agent beacon listeners | [RMM-Profiles](rmm-profiles.md) |
| Dynamic plugin volume and plugin-owned commands | [Plugin architecture](plugin-architecture.md) |
| URL-installed plugins, AI skills, and rclone files | [Operator-managed assets](operator-assets.md) |
| External repository for plugin source and releases | [External plugin repository](plugin-repository.md) |
| Go agent PE loader and plugin ABI | [Go agent plugins](go-agent-plugins.md) |
| SOCKS and TUN plugin split | [Pivot plugins](pivot-plugins.md) |
| Multi-operator storage and Exegol ownership | [Workspace deployment](workspace-deployment.md) |
| REST, CLI, MCP, WebUI, and AI operator parity | [MCP operator parity](mcp-parity.md) |

## Deployment and agents

| Topic | Page |
| --- | --- |
| Development and Docker startup paths | [Server startup](server-startup.md) |
| RMM-Profile volume and Cloudflared quick tunnel | [RMM-Profiles](rmm-profiles.md) |
| Go agent generation, targets, and runtime settings | [Go agent](go-agent.md) |
| PowerShell agent generation | [PowerShell agent generator](web-agent-generator.md) |
| Hidden command execution and shell dispatch | [Client command execution](client-command-execution.md) |
| Persistent session and artifact storage | [Session history](session-history.md) |
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
| Server-side AI skills | [AI skills](web-ai-skills.md) |
| Workspace and URL-installed AI skills | [Operator-managed assets](operator-assets.md) |
| Per-session chat persistence | [AI chat memory](web-ai-chat-memory.md) |
| Light and dark theme | [WebUI theme](web-ui-theme.md) |

## Product and planning

| Topic | Page |
| --- | --- |
| Product goals and commercial security gate | [Product requirements](prd.md) |
| Planned work and feature designs | [Technical plan](tech-plan.md) |
| Completed work, open gaps, and validation notes | [Progress log](progress.md) |

## Quick navigation by code area

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
| `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` | [Server startup](server-startup.md), [Workspace deployment](workspace-deployment.md), [Plugin architecture](plugin-architecture.md) |

The full API and command reference remains in the [root README](../README.md).

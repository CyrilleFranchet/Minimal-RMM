# Service startup reference

The only supported service launcher is **`rmm_deployment.py`**. Follow the
[deployment guide](deployment.md) for preparation and startup. This page is a
compatibility landing page, not an alternative installation recipe.

Supported infrastructure models are single mode inside Exegol and distributed
mode with central in a dedicated Docker container on the same host. Workspace
services run inside their existing Exegol environments. Different hosts with
an overlay such as Tailscale are future work.

All service configuration and mutable state follow the selected deployment root:
`central/settings.json` for central and `workspaces/<id>/settings.json` for each
workspace. See the complete [settings and variable reference](service-settings.md).

The underlying `server_rmm.py` and `workspace_service.py` modules remain callable
for internal tests and compatibility, but their implicit home/repository paths,
startup flags, and legacy shell/environment recipes are not supported deployment
instructions. Neither module automatically reads a `.env` file.

Optional prerequisites are installed separately: transport profile bundles under
`central/profiles/`, agent plugins in workspace assets, and the workspace's
`rmm-tun` helper when TUN is required. Startup does not compile or download them.
Use the feature references for their operational contracts:

- [RMM-Profiles](rmm-profiles.md)
- [Workspace protocols and ownership](workspace-deployment.md)
- [TUN](tun-pivot.md)
- [Service shutdown](shutdown.md)

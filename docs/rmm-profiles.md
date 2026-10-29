# RMM-Profiles

An **RMM-Profile** owns the external transport that makes the agent beacon
reachable. The RMM server only starts a profile process, passes it the local
beacon origin, and consumes generic lifecycle events. Transport commands,
provider-specific parsing, and provider credentials stay in the profile
bundle.

The first profile is Cloudflared. A future DNS, ngrok, or other connector can
use the same contract without changes to `server_rmm.py`.

## Separate listeners

The server has two HTTP listeners:

| Listener | Default | Consumers | Exposure |
| --- | ---: | --- | --- |
| Administration | `8080` | WebUI, CLI, MCP, operator API | Keep private or protect with an operator gateway |
| Agent beacon | `8081` | Agent `/register`, `/cmd`, `/result`, and pivot endpoints | Expose through the selected profile |

The administration listener does not accept beacon paths. The beacon listener
does not serve the WebUI or `/api/v1/*`. When both ports are explicitly set to
the same address and port, the server keeps the backward-compatible combined
listener.

Set the values with `RMM_ADMIN_PORT`, `RMM_BEACON_PORT`, `--admin-port`, and
`--beacon-port`. The generated PowerShell and Go agent configuration must use
the beacon URL, not the administration URL.

## Process contract

Install one profile in its own directory below `RMM_PROFILES_DIR`:

```text
/app/profiles/
└── cloudflared/
    ├── profile.json
    └── cloudflared_profile.py
```

The manifest must use `minimal-rmm-profile-v1`:

```json
{
  "schema": "minimal-rmm-profile-v1",
  "name": "cloudflared",
  "version": "0.1.0",
  "description": "Cloudflared transport for the RMM beacon listener",
  "entrypoint": "cloudflared_profile.py",
  "command": ["python3", "{entrypoint}"],
  "capabilities": ["https-ingress", "trycloudflare-url"]
}
```

The server starts the entrypoint with these environment variables:

| Variable | Meaning |
| --- | --- |
| `RMM_PROFILE_SCHEMA` | Contract identifier |
| `RMM_PROFILE_NAME` | Installed profile name |
| `RMM_PROFILE_LISTEN_HOST` | Local origin host selected by the server |
| `RMM_PROFILE_LISTEN_PORT` | Beacon listener port |
| `RMM_PROFILE_CONFIG_JSON` | Profile-specific JSON configuration |

The runner writes newline-delimited JSON to stdout. It must emit one ready
event after the external transport accepts traffic:

```json
{"event":"ready","public_url":"https://example.trycloudflare.com","hostname":"example.trycloudflare.com"}
```

It emits an error event when startup fails and keeps running until the server
sends `SIGTERM`:

```json
{"event":"error","message":"human-readable failure"}
```

Profile bundles are trusted server-side executables. They are deployed through
the profile volume or a release process; the operator asset URL API is for
agent plugins, AI skills, and rclone files.

## Cloudflared profile

Select Quick or Named in central service settings. Save the configuration and
restart the central service before starting the profile. The external profile
bundle must also be updated; updating only Minimal-RMM does not update a bundle
already copied into the profile store. No agent DLL rebuild is needed.

### Quick Tunnel

The Cloudflared bundle runs:

```text
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<beacon-port>
```

It extracts the generated `https://<name>.trycloudflare.com` URL from
Cloudflared output and returns it in the ready event. The server publishes that
URL through `GET /api/v1/transport` and `GET /api/v1/profile`, and the WebUI
uses it when generating an agent.

The profile needs a `cloudflared` executable in the control-plane container's
`PATH`, or a configured path in `RMM_PROFILE_CONFIG_JSON`:

```json
{"binary":"/usr/local/bin/cloudflared"}
```

The profile source and manifest are maintained in the private
[`Minimal-RMM-profiles`](https://github.com/CyrilleFranchet/Minimal-RMM-profiles)
repository. Install the `cloudflared/` directory under the persistent profile
store before starting the server.

For Compose, a host bind mount is easiest when installing or updating bundles:

```bash
git clone https://github.com/CyrilleFranchet/Minimal-RMM-profiles.git profiles
mkdir -p profile-store
cp -R profiles/cloudflared profile-store/
RMM_PROFILE_STORE=./profile-store docker compose up --build -d rmm-control
```

The private repository requires GitHub credentials. The same bundle can be
copied into the default named `rmm-profile-store` volume when a host bind mount
is not desired.

### Existing named tunnel

Create a remotely managed tunnel and a published hostname route in Cloudflare
first. Point that route at the central beacon origin, for example
`http://127.0.0.1:8081` when Cloudflared shares the server's network namespace.
Use the beacon listener, not the administration API.

Configure these central service settings:

| Setting | Value |
| --- | --- |
| `RMM_CLOUDFLARE_MODE` | `named` |
| `RMM_CLOUDFLARE_PUBLIC_URL` | Published HTTPS origin, such as `https://agents.example.com` |
| `RMM_CLOUDFLARE_TOKEN` | Tunnel connector token, supplied as a write-only secret |
| `RMM_CLOUDFLARE_BINARY` | `cloudflared` or its installed executable path |
| `RMM_PROFILE_NAME` | `cloudflared` |
| `RMM_PROFILE_AUTOSTART` | Enable to connect after the service restarts |

The runner starts the existing tunnel with the token supplied to the child
process securely rather than in command-line arguments. It reports the configured
public URL after Cloudflared registers a connection. This confirms connector
readiness; it does not prove that DNS or the externally managed hostname route
is correct. Verify the published route separately.

Minimal-RMM does not create Cloudflare accounts, tunnels, DNS records, or ingress
routes. Named tunnels require a Cloudflare account and an existing published
hostname configuration; Quick Tunnels retain their generated URL behavior.
`RMM_CLOUDFLARE_ORIGIN_HOST` configures the Quick Tunnel origin; named-tunnel
ingress is managed in Cloudflare. See Cloudflare's
[tunnel setup documentation](https://developers.cloudflare.com/tunnel/get-started/)
and [token documentation](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/).

A named hostname remains stable across connector restarts. Rotating the token
requires saving the replacement and restarting the central service. The secret
is not returned by settings/status APIs.

## Startup and operations

Start the server with `python3 server_rmm.py`. Startup does not compile plugins
or the TUN helper and does not fetch trusted profile executables. Prepare the
helper explicitly with `make tun-build` when TUN is needed outside Docker;
install plugin releases and trusted profile bundles separately. See
[server startup](server-startup.md).

With `RMM_PROFILE_AUTOSTART=1` (the default), the selected
`RMM_PROFILE_NAME` starts after the listeners bind. If it is not installed or
its provider binary is missing, the server remains available on the two local
listeners and reports the profile error through the log and API.

Operator controls are available on every surface:

```bash
python rmm_cli.py profile status
python rmm_cli.py profile start cloudflared
python rmm_cli.py profile stop
```

The WebUI exposes the same controls under **Config → Runtime assets and beacon
transport**. The API is:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/profile` | Installed profiles, active process, URL, and last error |
| `POST` | `/api/v1/profile` | Start `{ "name": "cloudflared" }` or stop `{ "stop": true }` |
| `GET` | `/api/v1/transport` | Administration URL, beacon URL, ports, and profile status |

MCP exposes `profile_status`, `start_profile`, and `stop_profile`.

## Adding a profile

1. Add a directory and a manifest to the profiles repository.
2. Implement the environment and NDJSON contract above.
3. Make the runner forward `SIGTERM` to its provider process.
4. Test it against a local fake beacon origin and a fake provider process.
5. Release or mount the bundle under `RMM_PROFILES_DIR`.
6. Start it with `profile start <name>` and verify the returned public URL.

The control plane must not gain provider-specific branches for a new profile.
Keep provider process launch and readiness parsing in the profile bundle; the\ncontrol plane supplies validated saved configuration through the generic contract.

## Implementation reference

| Symbol | Role |
| --- | --- |
| `profile_directory` | Resolve `RMM_PROFILES_DIR` or the log-directory fallback |
| `discover_profiles` | Validate manifests and safe entrypoints under the profile root |
| `_ready_event` | Validate public origins and retain only credential-free readiness fields |
| `RMMServer.profile_config` | Resolve active saved Cloudflare settings and per-start overrides |
| `RMMServer.beacon_public_url` | Return a live connector URL or the configured static fallback |
| `ProfileError` | Report missing, invalid, or failed profiles |
| `ProfileProcess` | Start one runner, consume NDJSON events, expose status, and stop it |
| `ProfileManager` | Discover profiles and keep one active connector |
| `ProfileManager.start` / `stop` / `status` | Control the active profile and public beacon URL |

The Cloudflared-specific implementation lives in
`Minimal-RMM-profiles/cloudflared/cloudflared_profile.py`, outside this
repository's control-plane code.

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
  "description": "Cloudflared quick tunnel for the RMM beacon listener",
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

## Startup and operations

`run_server.py` ensures that `tools/rmm-tun` is available, reports installed
agent plugins and profiles, then starts the server. It does not compile agent
plugins or fetch trusted profile executables.

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
Only the generic process manager and the profile bundle should change.

## Implementation reference

| Symbol | Role |
| --- | --- |
| `profile_directory` | Resolve `RMM_PROFILES_DIR` or the log-directory fallback |
| `discover_profiles` | Validate manifests and safe entrypoints under the profile root |
| `ProfileError` | Report missing, invalid, or failed profiles |
| `ProfileProcess` | Start one runner, consume NDJSON events, expose status, and stop it |
| `ProfileManager` | Discover profiles and keep one active connector |
| `ProfileManager.start` / `stop` / `status` | Control the active profile and public beacon URL |

The Cloudflared-specific implementation lives in
`Minimal-RMM-profiles/cloudflared/cloudflared_profile.py`, outside this
repository's control-plane code.

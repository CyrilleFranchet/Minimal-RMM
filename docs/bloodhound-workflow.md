# BloodHound CE workflow

The `bloodhound` operator tool persists a bounded collection/import/query run at
one storage owner. It uses an installed CE collector, verifies artifact format
and ingestion, and saves graph evidence from fixed query templates. It does not
execute attack paths, reset BloodHound, install packages, or claim detections.

## Supported first slice

- Existing `bloodhound-ce-python` executable and its CE version-6 JSON/ZIP format.
- Existing Kerberos **file** cache for the collection username and domain. The
  service's Python environment needs Impacket to validate it before collection.
- BloodHound CE signed API v2 with the documented upload and graph response
  contracts. Record the actual server version; compatibility is checked through
  responses, not inferred from the Exegol image name.
- LDAP-oriented `DCOnly` collection: Group, ACL, Trusts, ObjectProps, Container.
  Live sessions and host-local administrators are outside this slice.
- Sixteen bounded graph queries covering identity, privileged membership,
  account flags, delegation, object/GPO control, and replication-right candidates.
  See [query scope and provenance](bloodhound-query-catalog.md).

This implementation has fixture-backed and local HTTP integration tests. It
has not been tested against the operator's live CE instance or Ludus domain.

## Deployment and ownership

Configure these through the existing owner-local settings mechanism, or the
service's supported environment bootstrap. Do not put tokens in AI chat or CLI
arguments. See [service settings](service-settings.md).

| Setting | Purpose |
|---------|---------|
| `RMM_BLOODHOUND_URL` | Owner-local CE origin; default `http://127.0.0.1:1030`. Runtime requires loopback/localhost. No userinfo, query, or path prefix. |
| `RMM_BLOODHOUND_TOKEN_ID` | CE API token ID, stored as a secret setting. |
| `RMM_BLOODHOUND_TOKEN_KEY` | CE API signing key, stored as a secret setting. |
| `RMM_BLOODHOUND_COLLECTOR` | Installed `bloodhound-ce-python` command or absolute path with that basename. |
| `RMM_EXEGOL_MCP_URL` | Required authenticated Exegol MCP endpoint, reachable from the workspace runtime and central AI process. |
| `RMM_EXEGOL_MCP_TOKEN` | MCP bearer credential when required by that endpoint; stored as a secret. |
| `RMM_EXEGOL_CONTAINER` | Optional exact container-name override; leave unset for MCP discovery and workspace identity verification. |
| `KRB5CCNAME` | Service environment variable: raw existing cache file path, not a `FILE:` URI. Never returned by the API. |

The collector must be installed and configured by the operator. The workflow
checks its CLI flags and records executable/help hashes. The package version
reported by service metadata is explicitly labeled as such; it does not prove
that a collector installed under a different interpreter has that version.

In workspace mode, run this through the local Exegol workspace API. Artifacts
and credentials remain there under `<workspace data>/bloodhound`. The central
server relays workspace-scoped requests to their authenticated workspace instead
of reading its own files. Only structured requests and bounded replies pass
through the central relay; CE credentials and raw collection files stay local.
A single-workspace installation uses this same workspace-owned storage and
execution path. The central server remains independent of Exegol. Exegol identity is mandatory for AI and mutating BloodHound operations;
configuration, diagnostics, and ordinary agent management remain available when
that prerequisite is blocked. A MCP connection alone does not prove ownership.

Use one service process per storage directory. Restarted active phases become
uncertain rather than being replayed. Inspect any surviving collector process
and output before creating a replacement run. API keys must have permission to
upload data, read upload jobs, and execute graph queries. Use a dedicated lab CE
instance if existing data would make the demonstration ambiguous; this feature
never deletes graph data automatically.

## API, CLI, MCP, and AI

### Open the CE interface through its owner

The WebUI header's **Open BloodHound** button opens CE in another tab through
a dedicated proxy listener in the workspace process. The browser
never connects directly to CE's backend port. Opening it also asks the runtime
to ensure CE is available, as described below, and waits for bounded readiness.

| Setting | Default | Purpose |
|---------|---------|---------|
| `RMM_BLOODHOUND_UI_BIND` | `127.0.0.1` | Proxy listener address. |
| `RMM_BLOODHOUND_UI_PORT` | `1031` | Proxy listener port, separate from the WebUI and CE. |
| `RMM_BLOODHOUND_UI_PUBLIC_URL` | `http://127.0.0.1:1031` | Browser-reachable proxy origin, without a path prefix. |

For a remote Ludus workspace, set the bind address to its reachable interface
(or `0.0.0.0`) and the public URL to its browser-reachable hostname and proxy
port, for example `http://workspace.lab:1031`. Publish that port when using
container port mappings. With an existing HTTPS reverse proxy, use a dedicated
hostname such as `https://bloodhound.workspace.lab`, route it to this listener,
and preserve its public `Host` header. Keep CE itself on its local endpoint.
Each workspace needs its own proxy address. Restart after changing settings.

The API operation `bloodhound ui` (CLI), `bloodhound(action="ui")` (MCP), or
`POST /api/v1/bloodhound` with `{"action":"ui"}` returns a single-use launch
URL valid for 60 seconds once ready, or HTTP 202 with runtime progress while
startup is pending. Poll `health`, then request `ui` when ready. The browser
exchanges the launch URL for an HttpOnly proxy
session, then uses BloodHound's normal login. No adapter API key or RMM bearer
token is injected into CE. A proxy session lasts eight hours or until the
owning process restarts; closing the tab or disconnecting RMM does not revoke
that session. Treat launch URLs as temporary credentials.

The proxy starts lazily in the owning service process. Workspace-mode central
requests use the existing owner-scoped relay to obtain the launch link; actual
browser traffic goes to the workspace proxy. That proxy must be reachable from
the browser: the polling relay does not tunnel web traffic. The single preset
uses this same workspace proxy.

The proxy serves CE at the origin root to preserve `/ui/` routes and `/api/`
requests. It accepts a fixed configured upstream, checks browser admission and
write origins, bounds request/response sizes to 64 MiB, and rejects WebSocket
upgrades and redirects to external origins. External identity-provider login
redirects are therefore outside this initial implementation; use CE's local
login. Live CE/browser validation remains a deployment check.
Upstream connections have a 30-second socket inactivity timeout and responses
are buffered. A long silent query can outlive that timeout; a proxy failure
does not prove CE stopped processing it. Inspect CE before repeating work.

Implementation: `rmm_bloodhound_proxy.endpoint` validates configured origins;
`BrowserProxy` owns the listener, upstream connections, launch tickets, expiry,
and browser sessions; `BrowserHandler` validates and forwards requests, rewrites
redirects/cookies, and returns safe errors without logging credentials.
`open_ui` owns lazy startup and readiness checks; `close_ui` closes the listener
at process exit. In `web/bloodhound.js`,
`openBloodHound` reserves a tab, requests/polls the launch operation, validates
the destination, and reports failures through the text-only `message` helper.
`showRuntime` renders readiness; `startHealth`/`stopHealth` manage authenticated
connection polling and its `refresh` callback; `operation` follows owner relay
results and `pause` bounds waits with cancellation. These checks do not start CE.

### BloodHound CE service startup

The workspace service in both deployment presets starts a
background runtime at service startup. It verifies the required Exegol MCP
configuration, exact container, and ownership before starting or reusing CE.
It validates the live execution-tool schema and uses only the installed
`bloodhound-ce` launcher through Exegol MCP. No model or loaded skill is needed.
The CE endpoint must be local to the owner's network namespace.

`bloodhound-ce` runs in the foreground and never exits on its own, so the
runtime dispatches it detached (`LAUNCH_COMMAND` in `rmm_exegol_runtime.py`:
`setsid nohup`, output redirected, backgrounded, and `disown`ed) instead of
the bare command. Dispatching the bare command blocks the exec tool call for
the life of the server and gets the process killed with that call/session,
which otherwise surfaces as a 90-second `bloodhound_startup_uncertain` with
no BloodHound process ever actually running in the container.

Without `RMM_EXEGOL_CONTAINER`, the runtime calls MCP
`list_exegol_containers` and tests running candidates with a temporary identity
file, hostname, and network namespace. Exactly one candidate must match; its
name appears in runtime `container`. Discovery does not start stopped containers.
An explicit container override skips listing but must pass the same identity
check. Neither a shared Docker host nor a reachable MCP endpoint alone proves
workspace ownership.

Discovery accepts at most 32 container entries and 1 MiB of response content.
Each identity probe has a ten-second limit within the existing 120-second
lifecycle deadline. Missing/unsupported listing tools produce
`exegol_discovery_tool_unsupported`; malformed results produce
`exegol_discovery_response_invalid`. No matching workspace produces
`exegol_workspace_not_found`; multiple matching candidates produce
`exegol_workspace_identity_ambiguous`. Resolve the deployment mismatch or use
an explicit verified override for ambiguity; the runtime does not pick the
first container arbitrarily.

The discovery response contract was checked against
[Exegol MCP e9fe4e4](https://github.com/ThePorgs/Exegol-MCP/blob/e9fe4e445b66495f4c9d89fa82b7addffd3a46ed/exegol_mcp/assets/tools_orchestractor.py).
This does not remove the requirement that the owning service run in the Exegol
workspace's filesystem and network namespace. Remote server/Exegol deployment
over Tailscale or WireGuard remains in the technical backlog.

Readiness is tied to the verified owner configuration. Changing the MCP URL,
token, container, or CE endpoint invalidates it with
`runtime_configuration_changed`; use `ensure` to verify the new configuration.
No configuration secrets are included in the status response.

`health` reads runtime readiness without launching services. `ensure` requests
a single-flight check/start operation. The WebUI checks health on connection
and reconnect; **Open BloodHound** can recover a stopped, previously confirmed
instance through `ensure`. Repeated tabs and concurrent calls do not create
parallel launches. A startup intent is persisted before dispatch. Lost replies
or interrupted launches become `uncertain`; another request or process restart
does not replay that launch. Read-only readiness can reconcile a running CE.

States distinguish checking/starting, ready, blocked prerequisites, failed
checks, and uncertain startup. CE failure leaves the workspace available.
Verified Exegol permits other AI work even when CE is unavailable. Missing
Exegol prerequisites block AI and mutating BloodHound operations with a visible
reason. Fix configuration through owner-local settings and restart.

Runtime `mcp_status=verified` records a completed MCP check after its connection
closes; it does not claim a persistent live connection. `exegol_verified_at`
records the last successful owner-identity verification. Read-only CE health
updates `updated_at` without changing that verification timestamp.

Startup never installs CE, resets databases or credentials, or changes its
endpoint. `/api/version` readiness does not validate signed API permissions;
run `preflight` before collection/import. A 401/403 is a configuration blocker,
not a reason to restart. CE stays running when RMM stops. No live Exegol startup
or Ludus collection was performed during implementation.

### Workflow operations

`POST /api/v1/bloodhound` accepts exactly `action`, optional `run_id`, and
optional `options`. It uses existing operator authentication. Options never
contain endpoint overrides, API secrets, arbitrary Cypher, filesystem paths,
collection command strings, or ownership selectors.

The interactive CLI and WebUI shell expose
`/bloodhound ACTION --run-id ID --options-json JSON`; one-shot CLI invocations
use `python rmm_cli.py bloodhound ACTION --run-id ID --options-json JSON` (omit optional flags when
not needed). The WebUI intercepts this as an operator command; it never sends
it to the selected Windows agent. Use `result` for pending relay requests and
`status` for workflow progress. The main page offers **Open BloodHound** and
connection status; advanced actions remain in CLI, MCP, and the WebUI shell.
The MCP and Web AI tool is `bloodhound(action, run_id, options)`.
Load `python-bloodhound-collection` for Python
BloodHound CE/DCOnly collection, import, and verification; load
`bloodhound-analysis` separately to analyze a verified run. ldeep is planned
as a second collection method and is not an automatic fallback.

Centrally hosted AI uses a workspace-scoped relay. An initial response contains
`workspace_request_id`; call `result` with that ID as `run_id` until the local
reply is available. The reply may contain a separate workflow `run.id`. Never
repeat the original action merely because the relay is pending. Direct local
workspace API calls return the workflow reply without this transport step.

Central AI workers resolve MCP readiness and credentials through a private
workspace relay request before invoking the model. Browser-supplied MCP
overrides are ignored. `BloodHoundBroker.resolve_ai_context` bounds that wait
to 20 seconds and removes the response after consumption; private replies are
not accessible through public `result` calls and never enter the disk journal.
The workspace worker handles this read-only context request from owner settings.
Periodic `health` requests also bypass the disk journal, and their completed
central replies expire after 30 seconds to avoid filling the relay queue.

1. `preflight`: check independent collector, cache-configuration, and API flags.
   API success only means the check ran. AD connectivity is explicitly not
   tested, and the matching cache/TGT is validated at collection time.
2. `create`: provide the six fields below. Retain the returned `run.id`.
3. `collect`: submit once, then inspect `status`. The process runs independently
   of the HTTP/model request, with a 30-minute collector deadline.
4. `import`: submit once after collection completes. Poll `status` for ingestion.
5. `verify`: check expected objects in both current artifacts and the CE graph.
6. `queries`: inspect the catalog; `query` saves evidence using typed parameters.
7. `report`: retrieve phase outcomes, artifacts, limitations, and next action.

Example create options (documentation-only identities):

```json
{
  "domain": "lab.example",
  "dc": "dc.lab.example",
  "nameserver": "192.0.2.10",
  "username": "reader",
  "source_sid": "S-1-5-21-1-2-3-1100",
  "target_sid": "S-1-5-21-1-2-3-512"
}
```

Example query options:

```json
{
  "query_id": "candidate_path",
  "parameters": {
    "source_sid": "S-1-5-21-1-2-3-1100",
    "target_sid": "S-1-5-21-1-2-3-512"
  }
}
```

The collector username is the authenticated reader; it need not be the analysis
starting principal. A valid existing TGT must match the reader and domain. No
password, hash, or cache content is persisted in the run record.

## State, evidence, and recovery

Runs move through `created`, `collecting`, `collected`, `uploading`, `ingesting`,
`ingested`, and `verified`. Failure states include `collection_failed`,
`ingestion_incomplete`, and `uncertain`. Each accepted phase is admitted once
per run. Repeated submissions return its existing state, and invalid ordering
is rejected. A new run ID intentionally starts new work; `create` is not an
idempotent retry of an earlier create request.

The central relay admits eight pending requests per workspace and 128 total.
It marks pending requests uncertain after five minutes and retains results for
one hour in memory. A central restart loses that transient queue, not the local
workflow. Workspace request IDs and replies are cached under `relay-results/`;
the local `result` action can recover a reply. Persisted intent prevents
redispatched requests from executing twice after a lost reply. A crash between
intent and a saved result returns uncertainty and requires inspection. The
dedicated workspace relay thread does not block session/pivot synchronization.

Upload creation, each file, and finalization are checkpointed. An ambiguous
POST is never retried automatically. If finalization's response is lost,
`status` can reconcile the recorded ingestion ID without reuploading. Earlier
ambiguity remains recorded for operator inspection. Status polls perform one
HTTP request, not a blocking polling loop; after 30 minutes of pending ingestion
the run is marked uncertain but can still be reconciled read-only.

CE partial completion, failed files, unknown statuses, and missing job IDs never
count as verified ingestion. Artifact validation checks CE v6 metadata, counts,
file hashes, and presence of expected domain/source/target objects. The graph
must also contain those objects. This does **not** isolate the imported dataset:
CE may merge older records and relationships. Every report states that limit.

Evidence files preserve exact queries, graph responses, observation times,
run IDs, and ingestion IDs. Previews omit arbitrary graph properties and are
limited to 25 nodes and 50 edges. Full evidence remains local with private file
permissions. Candidate chains are reconstructed from edge direction; array
order is not assumed. No result proves credentials, current access, or successful
execution. Detection is always `not_verified` until independently observed by
another workflow; this adapter has no sensor integration.

## Bounds and security

Two collection/import workers are admitted per owner. Collection has a
30-minute subprocess timeout. HTTP uses a 30-second **socket inactivity** timeout,
not a guaranteed total wall-clock deadline. Responses are capped at 8 MiB;
collection files, expanded archives, and the imported run total are capped at
64 MiB. Each archive may contain at most 32 JSON entries; no extraction occurs.
Each run admits at most 100 saved query results. These are a deliberately small
lab slice, not an enterprise-size collector pipeline.

Queries come only from `bloodhound-queries/catalog.json`; request parameters
are validated DNS names or SIDs, and templates contain fixed depth/result
limits. CE's documented API has no separate Cypher parameter map, so the adapter
renders only these validated literal types. There is no arbitrary query API.
Signed HTTP requests disable redirects and proxy environment inheritance;
TLS verification remains enabled. Errors expose stable codes rather than raw
provider bodies or exception strings. The collector uses an argument vector
without a shell. Raw collector logs stay local and may contain directory data.

## Implementation map

`rmm_exegol_runtime.py` owns prerequisite verification and service lifecycle:

- `RuntimeErrorCode`, `timestamp`, `configuration`: safe errors, observation
  times, and validation of fixed owner settings.
- `probe_ce`, `execution_arguments`, `output_strings`: bounded local readiness,
  execution-schema matching, and structured MCP output interpretation.
- `discovery_candidates`, `discover_container`: validate bounded container
  listing output, probe running candidates, and select one verified workspace.
- `ExegolRuntime.__init__`, `_update`, `status`: synchronized state and read-only
  health refresh; `ensure`/`start`, `_run`, `_check_and_start`, and `stop`: bounded
  single-flight lifecycle, namespace verification, and shutdown cancellation.
- `_intent` and `_save`: durable dispatch/ready checkpoints; `get_runtime`:
  one runtime per owner root in the process.

`rmm_bloodhound.py` contains the workflow:

- `WorkflowError`, `validate`, `now`: safe failures, typed inputs, UTC timestamps.
- `query_catalog`, `render_query`, `ordered_path`: maintained queries and directed
  candidate-chain reconstruction.
- `NoRedirect`, `BloodHoundClient.request`/`graph`: exact-byte HMAC authentication,
  response bounds, endpoint validation, and graph-envelope checks.
- `collector_info`, `validate_cache`, `validate_artifact` and its local `inspect`
  helper: installed CLI evidence, matching TGT validation, and CE artifact checks.
- `BloodHoundWorkflow`: `create`/`get`/`save` persist runs; `launch`/`_work` admit
  asynchronous phases; `collect` and `import_artifacts` preserve checkpoints;
  `client_for` pins the instance; `status`, `verify`, `query`, `report`, and
  `preflight` implement the operator actions.
- `bloodhound_api`: owner-local action dispatch and redacted error mapping.

`RMMHandler._handle_bloodhound` and the workspace POST handler route ownership.
`RmmApiClient.bloodhound`, `cmd_bloodhound`, `tool_bloodhound`, and MCP
`bloodhound` expose the same contract. Operator parity includes this tool.

`rmm_bloodhound_broker.BloodHoundBroker` provides owner-scoped `submit`, `pending`,
`complete`, and `result`, with `_expire` bounding transient retention.
`execute_workspace_request` persists local dispatch intent/results. Workspace
`bloodhound_sync_once` and `_bloodhound_loop` poll the authenticated
`/workspace/bloodhound/requests` transport and post replies. Tests in
`test_bloodhound_broker.py` cover the central-tool-to-workspace round trip,
ownership, duplicate delivery, interrupted dispatch, capacity, and late replies.

Tests in `test_bloodhound.py` cover lifecycle, ambiguity, input validation,
cache matching, artifact integrity, query limits/direction, and real signed
HTTP against a loopback CE fixture. `test_bloodhound_surfaces.py` checks API
ownership, authentication gates, CLI/tool wiring, and secret settings. No test
contacts AD or performs a live collection.

## Primary contracts

Ingestion completion requires both `failed_files` and `partial_failed_files`
to be zero. A CE job labeled complete with partial file failures remains
incomplete in the adapter; inspect the recorded ingestion counters before
attempting verification or analysis.

- [CE collector CLI and output](https://github.com/dirkjanm/BloodHound.py/tree/bloodhound-ce)
- [BloodHound API authentication](https://bloodhound.specterops.io/integrations/bloodhound-api/working-with-api)
- [CE OpenAPI definitions](https://github.com/SpecterOps/BloodHound/tree/main/packages/go/openapi/src)
- [Supported Cypher syntax](https://bloodhound.specterops.io/analyze-data/explore/cypher-supported)
- [SpecterOps query library](https://github.com/SpecterOps/BloodHoundQueryLibrary)

The catalog combines project identity/path templates with bounded adaptations
from a pinned SpecterOps query-library revision. The complete 236-query snapshot
is separately available as reference material through `library_search` and
`library_get`; only the curated catalog is executable. See the
[reference discovery contract](bloodhound-query-catalog.md).
Verify deployed-version compatibility during preflight and the first
graph checks before relying on a lab result. `deferred_query_catalog` exposes
non-executable ideas and their collection/schema blockers through `queries`.

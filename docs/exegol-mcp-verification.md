# Exegol MCP verification

A deployment/development diagnostic for a live Exegol MCP endpoint,
independent of BloodHound or any other feature that happens to use it.

## Why a separate script, not a CLI/MCP operator command

Operator features follow REST API → `rmm_cli.py` → MCP
([`docs/mcp-parity.md`](mcp-parity.md)): they are reachable from an agent
session, `rmm_tools.py`, and `mcp_rmm_server.py`, so `scripts/check_operator_parity.py`
can enforce the same surface everywhere. This check is not one of those —
no agent session ever reaches it, and it exists purely so the person who
holds the Exegol MCP credentials can validate a new deployment, or a change
like this one, by running it directly. Putting it behind a `/mcp_list_*`
slash command would have required REST/CLI/MCP parity for something with no
operator use case, duplicated the connection and validation logic that
already lives in `rmm_exegol_runtime.py`, and — the real risk — printed raw
MCP tool output to an operator terminal instead of the bounded, validated
values the rest of the project is careful to use.

## What it checks

`scripts/check_exegol_mcp.py`:

1. Validates `RMM_EXEGOL_MCP_URL` with the same scheme/hostname checks
   `rmm_exegol_runtime.configuration()` applies, but without requiring
   `RMM_BLOODHOUND_URL` to be set, since this check has nothing to do with
   BloodHound.
2. Opens an `McpHttpSession` and lists the advertised tool names, flagging
   whether `list_exegol_containers` and `execute_command_in_container` (the
   only two tools anything in this project ever calls) are present.
3. Calls `list_exegol_containers` (read-only) and parses the result through
   the same `discovery_candidates` used by the real runtime, so only
   regex-validated container names are ever printed.
4. Runs the same one-line `printf` identity probe
   `rmm_exegol_runtime._check_and_start` runs before ever touching
   `bloodhound-ce`, through `execute_command_in_container`, against
   `--container` or the single running candidate if there is exactly one.
   This is the only command the script ever executes in the container, and
   it never starts, stops, or otherwise changes anything.
5. Never falls back to a static choice of container: with more than one
   running candidate and no `--container`, it reports them and stops
   instead of guessing.

Failures are classified through the same `runtime_failure_code` the runtime
uses (`exegol_mcp_connection_failed`, `exegol_mcp_authentication_failed`,
`exegol_mcp_timeout`, …), so the vocabulary matches what operators already
see in `runtime.log` and `health`/`ensure` responses.

## Usage

```shell
make check-exegol-mcp
# or
python3 scripts/check_exegol_mcp.py [--container NAME] [--no-probe] [--json]
                                     [--config PATH] [--data-dir PATH]
```

Exit code `0` means the MCP endpoint answered, the required tools are
present, and (unless `--no-probe` or more than one container was running)
the identity round trip through the exact tool and command path the runtime
uses was confirmed. Any other result is `1`.

### Reads the same configuration the workspace uses

`RMM_EXEGOL_MCP_URL`, `RMM_EXEGOL_MCP_TOKEN`, and `RMM_EXEGOL_CONTAINER` are
workspace settings (`rmm_settings.FIELDS`) — an operator normally saves them
from the workspace Config dialog, not from a shell environment variable.
Earlier versions of this script only read `os.environ`, so a value saved
through Config was invisible to it unless the operator also exported it by
hand before running `make check-exegol-mcp`.

The script now calls `rmm_settings.bootstrap("workspace", ...)` before
resolving any of those keys — the same call `workspace_service.py` makes at
startup — so it loads the persisted `settings.json` the running workspace
actually uses. `--config`/`--data-dir` select that file exactly as they do
for `workspace_service.py`; with neither flag, it resolves the same default
location (`RMM_WORKSPACE_DATA` or `~/.minimal-rmm`). A real environment
variable still takes precedence over the saved value, matching
`rmm_settings.getenv`'s normal override order, so exporting
`RMM_EXEGOL_MCP_URL` for a one-off check against a different endpoint still
works.

Not part of `make check`: it needs a real `RMM_EXEGOL_MCP_URL` to mean
anything, so it stays a manual, opt-in step — run it once per new
deployment, and again after a change that touches `rmm_exegol_runtime.py`
or `rmm_mcp_client.py`.

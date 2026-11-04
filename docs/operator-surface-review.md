# Operator surface regression review

Review date: November 3, 2026. This pass checked CLI dispatch, MCP response
conversion, asynchronous browser state, and Exegol runtime error reporting.
No live Exegol or BloodHound instance was contacted.

## Corrected findings

| Severity | Trigger | Previous behavior | Corrected behavior |
| --- | --- | --- | --- |
| High | `exec --command-file path` | The command payload overwrote the argparse subcommand destination with `None`, entering interactive mode | `subcommand` and remote `command` are separate; file execution reaches the execution handler |
| Medium | `run health` or `exec health` | The payload was interpreted as the CLI health action | Dispatch uses only the parsed subcommand |
| Medium | `exec --json` | The wait banner contaminated JSON stdout | Progress is written to stderr; stdout contains JSON |
| Medium | MCP response with only `structuredContent` | Valid structured tool results were discarded as empty | The converter reads the SDK protocol field and preserves structured data |
| High | Previous-login REST request completes after disconnect or a new login | Old session/history payloads could update the UI, or an old 401 could disconnect the new login | Responses are rejected as stale before exposing their body or status to consumers |
| Medium | BloodHound launch response completes after disconnect | A stale launch ticket could navigate its tab and stale errors could overwrite the new connection UI | Abort is checked after the response and lifecycle generations gate UI changes |
| Medium | MCP failure on Python 3.10 | Error classification referenced the unavailable `BaseExceptionGroup` builtin | Classification recognizes nested exception tuples without requiring that builtin |

## Implementation contracts

`rmm_cli.build_parser` uses `subcommand` only for top-level dispatch; payload
handlers retain their existing `args.command` argument. `main` selects the
handler from that subcommand. `cmd_exec` writes diagnostic progress to stderr.

`rmm_mcp_client._tool_result_text` prefers text content when present and otherwise
serializes `structuredContent`. This does not convert a successful HTTP exchange
into proof that an MCP tool succeeded; callers still need to interpret results.

The shared WebUI `api` function captures an authentication generation independently
of the transport generation. Starting a login or disconnecting changes the former;
switching selected sessions or reconnecting a WebSocket only changes the latter.
Stale responses return status zero with `stale_connection_response`, including
when authentication changes while the response body is still being read.

BloodHound `operation` checks cancellation after every awaited API request.
`stopHealth` cancels launch work, clears previous messages, and restores the button.
`openBloodHound` only updates controls for its originating health generation.

`runtime_failure_code` follows bounded exception chains and tuple-based nested
exceptions, including the AnyIO exceptiongroup backport shape. It never includes
provider exception text in public diagnostics.

## Added coverage and limits

- `CliDispatchTests.invoke` exercises the real parser and entry point with a mocked
  API client. Tests cover file commands, a payload named `health`, the health
  subcommand, and machine-readable JSON output.
- `McpResultEncodingTests` uses actual MCP SDK result models for structured-only and
  text responses. It requires the optional MCP dependency set.
- Browser regression tests use the production functions with controlled deferred
  promises, including old-owner launch tickets, old-login success and 401 replies,
  and a login change during response-body consumption.
- Runtime tests simulate nested backport-style exceptions and an unavailable
  Python 3.11 builtin. This is not a complete test run under Python 3.10.

The browser tests execute JavaScript contracts in Node; they do not certify visual
layout or behavior in a live browser. The existing shared beacon-secret trust
boundary and live host-to-container MCP reachability are outside this review.

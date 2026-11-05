# Web AI providers

The web AI panel supports OpenAI, Anthropic, and Mistral. An operator enters a
provider key in the browser and validates it before the provider becomes
selectable. After connection, keys, validation state, selected provider, and
selected model are synchronized to the authenticated server/workspace SQLite
UI state so the operator does not have to re-enter them after a browser
restart. Keep that database owner-only; the RMM API token remains a login
credential in the browser.

## API

POST /api/v1/ai/providers/validate accepts a provider and API key. It validates the key with the provider model-list API and returns available model IDs. A rejected key or provider outage returns provider_validation_failed.

POST /api/v1/ai/chat accepts provider, api_key, model, and messages. Supported provider values are openai, anthropic, and mistral. The legacy openai_api_key request field remains compatible with OpenAI. Mistral model lists include only models that report both chat-completion and function-calling capability.

## Implementation

OpenAI and Mistral use Chat Completions function calls. Anthropic uses Messages API tool_use and tool_result blocks. rmm_ai.py keeps the RMM tool loop provider-neutral: validate_ai_provider validates credentials, _anthropic_tools adapts tool schemas, _run_provider_loop executes native tool turns, and run_ai_chat always requires the configured Exegol MCP path; there is no per-run enable switch or RMM-only fallback. The Mistral adapter accepts both the standard `choices[].message` result used by custom function calls and the `choices[].messages` result used by connector completions. It also sends the function name with every Mistral tool result and converts text-content blocks to a plain string before persistence and display.

The agent loop allows 32 provider requests per internal checkpoint by default; tool turns and final replies each consume requests. When a checkpoint is reached, the server automatically asks the provider to continue with the existing conversation and tool results. It permits four continuations, for a total emergency ceiling of 160 rounds. Set `RMM_AI_MAX_TOOL_ROUNDS` to a positive integer before starting the server to change the checkpoint size. If the emergency ceiling is reached, the web UI displays the recorded tool-call trace returned by the server.

The module helpers are `_json_request` for JSON HTTP requests, `_provider_headers` and `_provider_models_url` for provider metadata, `supports_rmm_tools` for Mistral capability filtering, `_selected_session_context` for selected-session prompting, `_build_openai_convo` and `_build_anthropic_messages` for stored chat conversion, `_message_text` for text-content normalization, `_chat_completion_message` for Chat Completions response normalization, and `_run_ai_chat_mcp` for the supported tool backend. `_run_ai_chat_direct` remains an internal compatibility helper; production chat requires MCP and its configured Exegol connection.

Provider validation calls an external HTTPS API. Revalidate a key after rotating it or if its provider later revokes access.

Chat history is owned by Minimal-RMM and stored per selected session. The provider key is used to authorize a request; it is not used to retrieve an existing provider-side conversation. The current integration sends the stored history on each request.

## Manual UI verification

1. Open `/ui/`, authenticate with the RMM API token, and open **AI**.
2. Confirm the Provider and Model selectors are disabled before a key is validated.
3. Enter an invalid key and select **Validate**. Confirm the failure is shown and the provider remains unavailable.
4. Enter a valid provider key and select **Validate**. Confirm that provider becomes selectable and its returned models populate the Model selector.
5. Switch between two validated providers and confirm each retains its own selected model.
6. Edit the selected provider key. Confirm it disappears from the Provider selector until validation succeeds again.
7. Send a chat request and confirm the selected provider and model produce a response with RMM tool activity when appropriate.
8. Start validation and confirm the key input and validation button are disabled until the request completes.
9. With a custom model selected, type a provider key and confirm focus remains in that key field.

## Background execution

The WebUI uses the asynchronous `/api/v1/ai/jobs` API with the same provider
configuration and a unique request ID. The legacy `/ai/chat` API stays
synchronous. Provider loops accept an optional progress callback, invoked at
model/tool boundaries, which records safe job metadata and checks shutdown.
See [Background AI jobs](ai-background-jobs.md). Request credentials stay in
worker memory; completed chat and tool traces remain sensitive persisted data.

## Playbook and context limits

The chat injects only the explicit playbook supplied in `skill_documents`.
It does not resolve `skill_ids`, load default skills, or require per-step
preparation. All available operational schemas remain exposed throughout the
conversation. See [Web AI playbooks](web-ai-skills.md) for payload validation
and mandatory Exegol availability. Context budgeting still belongs to the central AI worker:
`RMM_AI_CONTEXT_BUDGET` and `RMM_AI_OUTPUT_RESERVE`. Context failures include
content-free request size measurements. Round continuation does not itself
compact context.

Provider/tool failures after execution retain a credential-safe diagnostic
classification, the latest step state, and scoped evidence references. Raw
exception messages are not used as this failure response. Returned-call records
mean output was received, not that the remote operation succeeded. Cancellation
still stops at cooperative checkpoints and does not replay work.

## Output-limit recovery

Empty final responses are not success. `_final_response_error` classifies a
response with no visible text and no tool calls as `empty_provider_response`.
The provider loop's `retry_empty` helper requests continuation at most twice
per task within the existing round ceiling. It preserves tool results and
conversation state, and never dispatches or replays tools itself. Cancellation is checked
through the recovery progress callback. Exhaustion returns an explicit failure
and retains the tool trace instead of displaying only server timing. This
does not establish semantic completion of a nonempty model report.

`tests/test_ai_empty_response.py` covers prerequisite checks followed by an
empty generation and subsequent startup, bounded exhaustion, the last round,
and a substantive blocker report across all three providers, using mocked
tools without creating a network relay.

`provider_output_truncated` means the provider reported `finish_reason=length`
or `stop_reason=max_tokens`. This is different from the local
`context_budget_exceeded` input-admission error. The number of task rounds
remaining does not increase the output allowance for one provider response.

`RMM_AI_OUTPUT_RESERVE` sets that allowance on the central AI worker, with a
default of 4,096 tokens. For OpenAI reasoning models, the allowance includes
reasoning as well as visible output, so a small budget can be exhausted before
a complete tool call appears. See the official
[Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
The reported task trace alone does not establish which tokens exhausted it.

V2 discards the entire incomplete generation, including complete-looking tool
calls in an incomplete batch. It retries generation at most twice across the
entire submitted task, within the existing total round ceiling, with a concise
current-step instruction. A successful intervening step does not reset that
recovery allowance. Cancellation is checked at the recovery progress checkpoint
before another provider request. It does
not append partial text, JSON, or reasoning blocks to subsequent requests.
The supplied playbook and prior returned results remain in place. Recovery neither dispatches the discarded batch nor resubmits prior tool
calls. This does not guarantee that a model will never independently propose
a duplicate operation later.

The output limit remains operator-configured; retries do not silently raise it.
For a reasoning-heavy task, consider 16,384 tokens if the selected model supports
it and the configured context budget can still fit the prompt, schemas, and
instructions. A larger reserve reduces available input space and can increase
latency and cost. Submit a new job after changing the worker setting; a failed
job is not automatically restarted. Inspect session events before resubmitting
operations whose outcome is uncertain.

On exhaustion, the response retains `provider_output_truncated`, the correct
round count, evidence references, and numeric `diagnostics.provider_output`
fields: configured limit, retries, and available input/output/reasoning counters.
The UI shows the limit, output/reasoning counts, and recovery attempts without
rendering provider text or arbitrary diagnostic fields. A truncated response
with no visible text is never treated as successful task completion.

Implementation: `_run_provider_conversation.request` applies the configured
limit to all protocols before context admission, so the request-size measurement
includes that field. Its `retry_truncated` helper preserves preparation and
discards incomplete output, and `truncated_failure` extracts safe counters.
`formatAiFailureDetails` renders only the permitted numeric diagnostics.

Verification uses mocked provider responses and local fixture tools. Across
OpenAI, Anthropic, and Mistral it checks successful recovery, discarded tool
batches, preservation of preparation and earlier evidence, global exhaustion,
the last permitted round, cancellation, a nondefault configured reserve, and
exact request-size accounting. UI checks verify numeric diagnostics and exclusion
of arbitrary provider text. No provider credentials, live target commands, or
PCAP capture are used in these tests. A sufficient output allowance for the
operator's particular live model/task still needs to be configured separately.

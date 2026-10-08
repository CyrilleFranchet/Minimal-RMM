# Web AI providers

The web AI panel supports OpenAI, Anthropic, and Mistral. An operator enters a provider key in the browser and validates it before the provider becomes selectable. Keys, validation state, selected provider, and selected model remain in sessionStorage for the browser tab. They are never written to RMM_logs.

## API

POST /api/v1/ai/providers/validate accepts a provider and API key. It validates the key with the provider model-list API and returns available model IDs. A rejected key or provider outage returns provider_validation_failed.

POST /api/v1/ai/chat accepts provider, api_key, model, and messages. Supported provider values are openai, anthropic, and mistral. The legacy openai_api_key request field remains compatible with OpenAI. Mistral model lists include only models that report both chat-completion and function-calling capability.

## Implementation

OpenAI and Mistral use Chat Completions function calls. Anthropic uses Messages API tool_use and tool_result blocks. rmm_ai.py keeps the RMM tool loop provider-neutral: validate_ai_provider validates credentials, _anthropic_tools adapts tool schemas, _run_provider_loop executes native tool turns, and run_ai_chat selects MCP or direct RMM execution. The Mistral adapter accepts both the standard `choices[].message` result used by custom function calls and the `choices[].messages` result used by connector completions. It also sends the function name with every Mistral tool result and converts text-content blocks to a plain string before persistence and display.

The module helpers are `_json_request` for JSON HTTP requests, `_provider_headers` and `_provider_models_url` for provider metadata, `supports_rmm_tools` for Mistral capability filtering, `_selected_session_context` for selected-session prompting, `_build_openai_convo` and `_build_anthropic_messages` for stored chat conversion, `_message_text` for text-content normalization, `_chat_completion_message` for Chat Completions response normalization, and `_run_ai_chat_mcp` and `_run_ai_chat_direct` for the RMM tool backends.

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

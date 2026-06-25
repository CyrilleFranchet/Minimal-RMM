# Web AI — OpenAI model list

The AI panel model dropdown is populated from `web/ai-models.js`.

## Default

`RMM_OPENAI_DEFAULT_MODEL` is **`gpt-5.2`** (balanced for tool calling). Users who previously selected `gpt-4o-mini` keep their choice in `sessionStorage` until they change it.

## Groups

| Group | Examples | Notes |
|-------|----------|-------|
| Recommended | `gpt-5.2`, `gpt-5.4`, `gpt-5.4-mini` | Start here |
| GPT-5 family | `gpt-5.1`, `gpt-5-mini`, `gpt-5.5`, … | Newer tiers if your API key has access |
| Reasoning (o-series) | `o3`, `o3-mini`, `o4-mini` | Slower; better for hard planning |
| GPT-4 family | `gpt-4.1`, `gpt-4o`, `gpt-4o-mini` | Legacy / cost-sensitive |
| Other | Custom model ID | Any Chat Completions model string |

## Custom model

Choose **Custom model ID…** and type the exact OpenAI model name (e.g. `gpt-5.2-pro`). Stored in `sessionStorage` as `rmm_openai_model_custom`.

## Updating the list

Edit `web/ai-models.js` when OpenAI adds or deprecates models. No server change required — the browser sends the chosen `model` to `POST /api/v1/ai/chat`.

## Files

| File | Role |
|------|------|
| `web/ai-models.js` | Model catalog + default |
| `web/ai.js` | Dropdown population, custom field, persistence |
| `web/index.html` | `#openai-model-select`, `#openai-model-custom` |
| `rmm_ai.py` | Server-side fallback default if `model` omitted |

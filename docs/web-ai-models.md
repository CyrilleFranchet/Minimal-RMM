# Web AI model selection

The AI panel has no bundled model catalog. After the operator validates an OpenAI, Anthropic, or Mistral API key, the provider model-list API supplies the selectable models for that key.

Each provider keeps its own selected model in sessionStorage for the browser tab. A custom model ID is also available for a model that is absent from the returned list. Changing a key removes the provider from the selector until the new key is validated.

See ai-providers.md for endpoint, protocol, and security details.

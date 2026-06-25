/**
 * OpenAI chat models for the web AI panel (Chat Completions API).
 * Update when OpenAI ships new IDs — or use "Custom model ID" in the UI.
 */
window.RMM_OPENAI_DEFAULT_MODEL = "gpt-5.2";

window.RMM_OPENAI_MODEL_GROUPS = [
  {
    label: "Recommended",
    models: [
      { id: "gpt-5.2", hint: "Balanced — good default for RMM tool use" },
      { id: "gpt-5.4", hint: "Latest general-purpose" },
      { id: "gpt-5.4-mini", hint: "Faster / cheaper GPT-5.4" },
    ],
  },
  {
    label: "GPT-5 family",
    models: [
      { id: "gpt-5.4-nano", hint: "Fastest GPT-5.4 tier" },
      { id: "gpt-5.2-pro", hint: "Higher accuracy" },
      { id: "gpt-5.1", hint: "Prior flagship" },
      { id: "gpt-5.1-mini", hint: "Smaller GPT-5.1" },
      { id: "gpt-5.1-codex", hint: "Coding-focused" },
      { id: "gpt-5", hint: "Unified GPT-5 router" },
      { id: "gpt-5-mini", hint: "Cost-efficient GPT-5" },
      { id: "gpt-5-nano", hint: "Fastest GPT-5" },
      { id: "gpt-5.5", hint: "Newer flagship (if enabled on your API key)" },
      { id: "gpt-5.5-pro", hint: "Top tier (if enabled on your API key)" },
    ],
  },
  {
    label: "Reasoning (o-series)",
    models: [
      { id: "o3", hint: "Strong reasoning — slower, higher cost" },
      { id: "o3-mini", hint: "Smaller reasoning model" },
      { id: "o4-mini", hint: "Fast reasoning" },
    ],
  },
  {
    label: "GPT-4 family",
    models: [
      { id: "gpt-4.1", hint: "Smart non-reasoning; 1M context" },
      { id: "gpt-4.1-mini", hint: "Smaller GPT-4.1" },
      { id: "gpt-4.1-nano", hint: "Fast GPT-4.1" },
      { id: "gpt-4o", hint: "Multimodal GPT-4o" },
      { id: "gpt-4o-mini", hint: "Fast / cheap (legacy default)" },
    ],
  },
  {
    label: "Other",
    models: [{ id: "__custom__", hint: "Type any Chat Completions model ID" }],
  },
];

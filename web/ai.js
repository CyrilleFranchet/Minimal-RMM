/**
 * Web AI assistant — provider-neutral chat via MCP at POST /api/v1/ai/chat
 */
(function () {
  const PROVIDERS = ["openai", "anthropic", "mistral"];
  const PROVIDER_LABELS = { openai: "OpenAI", anthropic: "Anthropic", mistral: "Mistral" };
  const PROVIDER_STATE_STORAGE = "rmm_ai_provider_state";
  const PROVIDER_STORAGE = "rmm_ai_provider";
  const AI_PANEL_OPEN_STORAGE = "rmm_ai_panel_open";
  const EXEGOL_ENABLED_STORAGE = "rmm_exegol_mcp_enabled";
  const EXEGOL_URL_STORAGE = "rmm_exegol_mcp_url";
  const EXEGOL_TOKEN_STORAGE = "rmm_exegol_mcp_token";
  const AI_SKILLS_ENABLED_STORAGE = "rmm_ai_skills_enabled";
  const LEGACY_AI_CHAT_STORAGE_KEY = "rmm_ai_chat_v1";
  const DEFAULT_EXEGOL_MCP_URL = "http://127.0.0.1:8000/mcp";
  const CUSTOM_MODEL_VALUE = "__custom__";

  const $ = (sel) => document.querySelector(sel);

  /** @type {{ role: string, content: string, toolCalls?: object[] }[]} */
  let chatHistory = [];
  /** Session UUID whose messages are loaded, or null when none selected */
  let loadedChatSessionId = undefined;
  let chatLoadToken = 0;
  let sending = false;
  let aiSkills = [];
  const providerValidationTokens = {};

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function normalizeStoredMessages(messages) {
    if (!Array.isArray(messages)) return [];
    return messages
      .filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
      .map((m) => {
        const toolCalls = Array.isArray(m.toolCalls)
          ? m.toolCalls
          : Array.isArray(m.tool_calls_made)
            ? m.tool_calls_made
            : undefined;
        return {
          role: m.role,
          content: m.content,
          toolCalls: toolCalls?.length ? toolCalls : undefined,
        };
      });
  }

  function activeSessionId() {
    const st = window.rmmState;
    return st?.selectedId || st?.selectedHistoryId || null;
  }

  function welcomeText() {
    const exegolOn = sessionStorage.getItem(EXEGOL_ENABLED_STORAGE) === "1";
    return exegolOn
      ? "RMM tools (sessions, commands, config) plus Exegol MCP (containers, in-container pentest tools). Select an RMM session in the sidebar for beacon context."
      : "RMM tools via MCP (list sessions, run commands, change beacon sleep, etc.). Enable Exegol MCP in settings to add container orchestration and offensive tools.";
  }

  function getProviderState() {
    try {
      const state = JSON.parse(sessionStorage.getItem(PROVIDER_STATE_STORAGE) || "{}");
      return state && typeof state === "object" ? state : {};
    } catch {
      return {};
    }
  }

  function saveProviderState(state) {
    sessionStorage.setItem(PROVIDER_STATE_STORAGE, JSON.stringify(state));
  }

  function getProviderKey(provider) {
    return ($(`#${provider}-key-input`)?.value || sessionStorage.getItem(`rmm_${provider}_api_key`) || "").trim();
  }

  function getActiveProvider() {
    return $("#ai-provider-select")?.value || sessionStorage.getItem(PROVIDER_STORAGE) || "";
  }

  function getModel() {
    const sel = $("#ai-model-select");
    const custom = $("#ai-model-custom");
    const state = getProviderState();
    const providerState = state[getActiveProvider()] || {};
    if (sel?.value === CUSTOM_MODEL_VALUE) {
      return (custom?.value || providerState.customModel || "").trim();
    }
    return sel ? sel.value : providerState.model || "";
  }

  function syncCustomModelField(focus = false) {
    const sel = $("#ai-model-select");
    const custom = $("#ai-model-custom");
    if (!sel || !custom) return;
    const show = sel.value === CUSTOM_MODEL_VALUE;
    custom.classList.toggle("hidden", !show);
    if (show && focus) custom.focus();
  }

  function populateModelSelect() {
    const sel = $("#ai-model-select");
    if (!sel) return;
    const state = getProviderState();
    const provider = getActiveProvider();
    const models = state[provider]?.models || [];
    sel.replaceChildren();
    for (const model of models) {
      const opt = document.createElement("option");
      opt.value = model;
      opt.textContent = model;
      sel.appendChild(opt);
    }
    const custom = document.createElement("option");
    custom.value = CUSTOM_MODEL_VALUE;
    custom.textContent = "Custom model ID…";
    sel.appendChild(custom);
    sel.disabled = !provider;
  }

  function restoreModelSelection() {
    const sel = $("#ai-model-select");
    const custom = $("#ai-model-custom");
    if (!sel) return;
    const providerState = getProviderState()[getActiveProvider()] || {};
    const saved = providerState.model || "";
    const savedCustom = providerState.customModel || "";
    const known = Array.from(sel.options).some((o) => o.value === saved);
    if (known) {
      sel.value = saved;
    } else if (saved) {
      sel.value = CUSTOM_MODEL_VALUE;
      if (custom) custom.value = saved;
    } else {
      sel.value = sel.options[0]?.value || CUSTOM_MODEL_VALUE;
    }
    if (custom && savedCustom) custom.value = savedCustom;
    syncCustomModelField();
  }

  function renderProviderControls() {
    const select = $("#ai-provider-select");
    if (!select) return;
    const state = getProviderState();
    const selected = sessionStorage.getItem(PROVIDER_STORAGE) || "";
    select.replaceChildren();
    for (const provider of PROVIDERS) {
      if (!state[provider]?.valid || !getProviderKey(provider)) continue;
      const option = document.createElement("option");
      option.value = provider;
      option.textContent = PROVIDER_LABELS[provider];
      select.appendChild(option);
    }
    select.disabled = !select.options.length;
    if (Array.from(select.options).some((option) => option.value === selected)) {
      select.value = selected;
    }
    if (!select.value && select.options.length) select.selectedIndex = 0;
    sessionStorage.setItem(PROVIDER_STORAGE, select.value || "");
    populateModelSelect();
    restoreModelSelection();
  }

  function setProviderStatus(provider, text, valid) {
    const status = $(`#${provider}-provider-status`);
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("ai-provider-valid", valid === true);
    status.classList.toggle("ai-provider-invalid", valid === false);
  }

  async function validateProvider(provider) {
    const apiFn = window.rmmApi;
    const key = getProviderKey(provider);
    const keyInput = $(`#${provider}-key-input`);
    const button = $(`.ai-provider-validate[data-provider="${provider}"]`);
    if (!apiFn || !window.rmmState?.token) {
      setProviderStatus(provider, "Connect to RMM before validating a key.", false);
      return;
    }
    if (!key) {
      setProviderStatus(provider, "Enter an API key first.", false);
      return;
    }
    const validationToken = (providerValidationTokens[provider] || 0) + 1;
    providerValidationTokens[provider] = validationToken;
    if (keyInput) keyInput.disabled = true;
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    setProviderStatus(provider, "Validating…");
    try {
      const { status, data } = await apiFn("/ai/providers/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, api_key: key }),
      });
      if (status !== 200 || !data.ok) {
        throw new Error(data.detail || data.error || `HTTP ${status}`);
      }
      if (providerValidationTokens[provider] !== validationToken || getProviderKey(provider) !== key) {
        return;
      }
      sessionStorage.setItem(`rmm_${provider}_api_key`, key);
      const state = getProviderState();
      state[provider] = { valid: true, models: Array.isArray(data.models) ? data.models : [] };
      saveProviderState(state);
      setProviderStatus(provider, `Validated. ${state[provider].models.length} models available.`, true);
      renderProviderControls();
    } catch (error) {
      if (providerValidationTokens[provider] !== validationToken || getProviderKey(provider) !== key) {
        return;
      }
      const state = getProviderState();
      delete state[provider];
      saveProviderState(state);
      setProviderStatus(provider, `Validation failed: ${error.message || error}`, false);
      renderProviderControls();
    } finally {
      if (providerValidationTokens[provider] === validationToken) {
        if (keyInput) keyInput.disabled = false;
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
      }
    }
  }

  function refreshProviderInputs() {
    const state = getProviderState();
    for (const provider of PROVIDERS) {
      const input = $(`#${provider}-key-input`);
      if (input) input.value = sessionStorage.getItem(`rmm_${provider}_api_key`) || "";
      setProviderStatus(
        provider,
        state[provider]?.valid && getProviderKey(provider)
          ? "Previously validated for this browser tab."
          : "Not configured.",
        Boolean(state[provider]?.valid && getProviderKey(provider))
      );
    }
    renderProviderControls();
  }

  function getExegolMcpSettings() {
    const enabled = $("#exegol-mcp-enabled")?.checked ?? false;
    const url = ($("#exegol-mcp-url-input")?.value || "").trim() || DEFAULT_EXEGOL_MCP_URL;
    const token = ($("#exegol-mcp-token-input")?.value || "").trim();
    return { enabled, url, token };
  }

  function persistExegolMcpSettings() {
    const { enabled, url, token } = getExegolMcpSettings();
    sessionStorage.setItem(EXEGOL_ENABLED_STORAGE, enabled ? "1" : "0");
    sessionStorage.setItem(EXEGOL_URL_STORAGE, url);
    sessionStorage.setItem(EXEGOL_TOKEN_STORAGE, token);
  }

  function loadEnabledSkillIds() {
    const raw = sessionStorage.getItem(AI_SKILLS_ENABLED_STORAGE);
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          return parsed.map((id) => String(id).trim()).filter(Boolean);
        }
      } catch {
        /* ignore */
      }
    }
    return aiSkills.filter((s) => s.default).map((s) => s.id);
  }

  function persistEnabledSkillIds(ids) {
    sessionStorage.setItem(AI_SKILLS_ENABLED_STORAGE, JSON.stringify(ids));
  }

  function getEnabledSkillIds() {
    const known = new Set(aiSkills.map((s) => s.id));
    const selected = loadEnabledSkillIds().filter((id) => known.has(id));
    const always = aiSkills.filter((skill) => skill.always).map((skill) => skill.id);
    return [...new Set([...always, ...selected])];
  }

  function renderAiSkillsList() {
    const container = $("#ai-skills-list");
    if (!container) return;
    if (!aiSkills.length) {
      container.innerHTML = '<p class="ai-skills-empty">No skills on server.</p>';
      return;
    }
    const enabled = new Set(getEnabledSkillIds());
    container.replaceChildren();
    for (const skill of aiSkills) {
      const label = document.createElement("label");
      label.className = "ai-skill-item";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = skill.id;
      input.checked = enabled.has(skill.id);
      input.disabled = Boolean(skill.always);
      input.addEventListener("change", () => {
        const next = [];
        for (const el of container.querySelectorAll('input[type="checkbox"]')) {
          if (el.checked) next.push(el.value);
        }
        persistEnabledSkillIds(next);
      });
      const text = document.createElement("span");
      text.className = "ai-skill-label";
      text.title = skill.description || skill.id;
      text.textContent = skill.title || skill.id;
      label.appendChild(input);
      label.appendChild(text);
      container.appendChild(label);
    }
  }

  async function fetchAiSkills() {
    const apiFn = window.rmmApi;
    const st = window.rmmState;
    const container = $("#ai-skills-list");
    if (!apiFn || !st?.token) {
      if (container) {
        container.innerHTML = '<p class="ai-skills-empty">Connect to load skills.</p>';
      }
      return;
    }
    const { status, data } = await apiFn("/ai/skills");
    if (status !== 200) {
      if (container) {
        container.innerHTML = '<p class="ai-skills-empty">Failed to load skills.</p>';
      }
      return;
    }
    aiSkills = data.skills || [];
    renderAiSkillsList();
  }

  function setAiPanelOpen(open) {
    const panel = $("#ai-panel");
    const body = $("#app-body");
    const btn = $("#ai-toggle-btn");
    if (!panel || !body) return;
    panel.classList.toggle("hidden", !open);
    body.classList.toggle("ai-open", open);
    btn?.classList.toggle("active", open);
    sessionStorage.setItem(AI_PANEL_OPEN_STORAGE, open ? "1" : "0");
  }

  function renderToolCalls(toolCalls) {
    if (!toolCalls?.length) return "";
    return toolCalls
      .map(
        (t) =>
          `<div class="ai-msg-tool">→ ${escapeHtml(t.name)}(${escapeHtml(JSON.stringify(t.arguments || {}))})</div>`
      )
      .join("");
  }

  function appendChatMessage(role, content, extraHtml = "") {
    const log = $("#ai-chat-log");
    if (!log) return;
    const block = document.createElement("div");
    block.className = `ai-msg ai-msg-${role}`;
    block.innerHTML = `
      <div class="ai-msg-role">${escapeHtml(role)}</div>
      <div class="ai-msg-body">${escapeHtml(content)}</div>
      ${extraHtml}
    `;
    log.appendChild(block);
    log.scrollTop = log.scrollHeight;
  }

  function appendWelcomeMessage() {
    appendChatMessage("assistant", welcomeText());
  }

  function renderChatLog() {
    const log = $("#ai-chat-log");
    if (!log) return;
    log.replaceChildren();
    if (!chatHistory.length) {
      appendWelcomeMessage();
      return;
    }
    for (const msg of chatHistory) {
      const toolsHtml = renderToolCalls(msg.toolCalls);
      appendChatMessage(msg.role, msg.content, toolsHtml);
    }
  }

  async function syncAiChatWithSession(sessionId) {
    if (loadedChatSessionId === sessionId) return;
    loadedChatSessionId = sessionId;
    const token = ++chatLoadToken;

    if (!sessionId) {
      chatHistory = [];
      renderChatLog();
      return;
    }

    const apiFn = window.rmmApi;
    if (!apiFn || !window.rmmState?.token) {
      chatHistory = [];
      renderChatLog();
      return;
    }

    try {
      const { status, data } = await apiFn(
        `/sessions/${encodeURIComponent(sessionId)}/ai/chat`
      );
      if (token !== chatLoadToken || loadedChatSessionId !== sessionId) return;
      chatHistory = status === 200 ? normalizeStoredMessages(data.messages) : [];
    } catch {
      if (token !== chatLoadToken) return;
      chatHistory = [];
    }
    renderChatLog();
  }

  async function resetAiChatForCurrentSession() {
    const sessionId = loadedChatSessionId;
    const label = sessionId ? `session ${sessionId.slice(0, 8)}` : "this chat";
    if (!confirm(`Clear the AI chat for ${label}? This cannot be undone.`)) {
      return;
    }

    if (sessionId && window.rmmApi && window.rmmState?.token) {
      try {
        await window.rmmApi(`/sessions/${encodeURIComponent(sessionId)}/ai/chat`, {
          method: "DELETE",
        });
      } catch {
        appendChatMessage("error", "Failed to clear AI chat on server.");
        return;
      }
    }

    chatHistory = [];
    renderChatLog();
  }

  function clearAiChatMemory(sessionId) {
    if (!sessionId || loadedChatSessionId !== sessionId) return;
    chatHistory = [];
    renderChatLog();
  }

  function purgeAiChatsForSessions(sessionIds) {
    if (!sessionIds?.length || !loadedChatSessionId) return;
    if (sessionIds.includes(loadedChatSessionId)) {
      chatHistory = [];
      renderChatLog();
    }
  }

  async function sendAiMessage(text) {
    if (sending || !text.trim()) return;
    const apiFn = window.rmmApi;
    const st = window.rmmState;
    if (!apiFn || !st?.token) {
      appendChatMessage("error", "Connect to RMM first (API token required).");
      return;
    }
    const provider = getActiveProvider();
    const apiKey = getProviderKey(provider);
    if (!provider || !apiKey) {
      appendChatMessage("error", "Validate an AI provider key in the panel settings.");
      return;
    }

    const model = getModel();
    if (!model) {
      appendChatMessage("error", "Choose a model or enter a custom model ID.");
      return;
    }
    persistExegolMcpSettings();
    const exegol = getExegolMcpSettings();

    const sessionId = activeSessionId();
    if (loadedChatSessionId === undefined) {
      loadedChatSessionId = sessionId;
    }

    chatHistory.push({ role: "user", content: text.trim() });
    appendChatMessage("user", text.trim());

    const input = $("#ai-chat-input");
    const sendBtn = $("#ai-send-btn");
    sending = true;
    if (input) input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    try {
      const { status, data } = await apiFn("/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          api_key: apiKey,
          model: getModel(),
          messages: chatHistory,
          selected_session_id: sessionId,
          exegol_mcp_enabled: exegol.enabled,
          exegol_mcp_url: exegol.enabled ? exegol.url : null,
          exegol_mcp_token: exegol.enabled ? exegol.token || null : null,
          skill_ids: getEnabledSkillIds(),
        }),
      });

      if (status !== 200 || !data.ok) {
        const err =
          data.detail || data.error || (typeof data.message === "string" ? data.message : `HTTP ${status}`);
        const failedTools = Array.isArray(data.tool_calls_made) ? data.tool_calls_made : [];
        appendChatMessage("error", String(err), renderToolCalls(failedTools));
        chatHistory.pop();
        return;
      }

      const reply = data.message || "(empty response)";
      const toolCalls = data.tool_calls_made || [];
      const toolsHtml = renderToolCalls(toolCalls);
      chatHistory.push({
        role: "assistant",
        content: reply,
        toolCalls: toolCalls.length ? toolCalls : undefined,
      });
      appendChatMessage("assistant", reply, toolsHtml);
    } catch (e) {
      appendChatMessage("error", e.message || String(e));
      chatHistory.pop();
    } finally {
      sending = false;
      if (input) {
        input.disabled = false;
        input.value = "";
        input.focus();
      }
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  function initAiPanel() {
    const modelSelect = $("#ai-model-select");
    const modelCustom = $("#ai-model-custom");
    for (const provider of PROVIDERS) {
      const keyInput = $(`#${provider}-key-input`);
      if (keyInput) {
        keyInput.value = sessionStorage.getItem(`rmm_${provider}_api_key`) || "";
        keyInput.addEventListener("input", () => {
          const state = getProviderState();
          delete state[provider];
          saveProviderState(state);
          setProviderStatus(provider, "Key changed. Validate it to enable this provider.");
          renderProviderControls();
        });
      }
      const status = $(`#${provider}-provider-status`);
      if (getProviderState()[provider]?.valid) {
        setProviderStatus(provider, "Previously validated for this browser tab.", true);
      } else if (status) {
        setProviderStatus(provider, "Not configured.");
      }
    }
    for (const button of document.querySelectorAll(".ai-provider-validate")) {
      button.addEventListener("click", () => validateProvider(button.dataset.provider));
    }
    $("#ai-provider-select")?.addEventListener("change", () => {
      sessionStorage.setItem(PROVIDER_STORAGE, getActiveProvider());
      populateModelSelect();
      restoreModelSelection();
    });
    renderProviderControls();
    if (modelSelect) {
      modelSelect.addEventListener("change", () => {
        const state = getProviderState();
        const provider = getActiveProvider();
        if (state[provider]) state[provider].model = modelSelect.value;
        saveProviderState(state);
        syncCustomModelField(true);
      });
    }
    if (modelCustom) {
      modelCustom.addEventListener("change", () => {
        const state = getProviderState();
        const provider = getActiveProvider();
        if (state[provider]) state[provider].customModel = modelCustom.value.trim();
        saveProviderState(state);
      });
    }
    window.addEventListener("rmm-config-imported", refreshProviderInputs);

    const exegolEnabled = $("#exegol-mcp-enabled");
    const exegolUrl = $("#exegol-mcp-url-input");
    const exegolToken = $("#exegol-mcp-token-input");
    if (exegolEnabled) {
      exegolEnabled.checked = sessionStorage.getItem(EXEGOL_ENABLED_STORAGE) === "1";
      exegolEnabled.addEventListener("change", persistExegolMcpSettings);
    }
    if (exegolUrl) {
      exegolUrl.value =
        sessionStorage.getItem(EXEGOL_URL_STORAGE) || DEFAULT_EXEGOL_MCP_URL;
      exegolUrl.addEventListener("change", persistExegolMcpSettings);
    }
    if (exegolToken) {
      exegolToken.value = sessionStorage.getItem(EXEGOL_TOKEN_STORAGE) || "";
      exegolToken.addEventListener("change", persistExegolMcpSettings);
    }

    if (sessionStorage.getItem(AI_PANEL_OPEN_STORAGE) === "1") {
      setAiPanelOpen(true);
    }

    $("#ai-toggle-btn")?.addEventListener("click", () => {
      const panel = $("#ai-panel");
      setAiPanelOpen(panel?.classList.contains("hidden"));
    });
    $("#ai-panel-close")?.addEventListener("click", () => setAiPanelOpen(false));
    $("#ai-chat-reset-btn")?.addEventListener("click", resetAiChatForCurrentSession);

    $("#ai-chat-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("#ai-chat-input");
      if (input) sendAiMessage(input.value);
    });

    loadedChatSessionId = undefined;
    chatHistory = [];
    try {
      localStorage.removeItem(LEGACY_AI_CHAT_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    renderChatLog();
    fetchAiSkills().catch(() => {});
  }

  window.initAiPanel = initAiPanel;
  window.fetchAiSkills = fetchAiSkills;
  window.syncAiChatWithSession = syncAiChatWithSession;
  window.clearAiChatMemory = clearAiChatMemory;
  window.purgeAiChatsForSessions = purgeAiChatsForSessions;
})();

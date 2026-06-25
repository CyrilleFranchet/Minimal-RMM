/**
 * Web AI assistant — OpenAI chat via MCP (mcp_rmm_server.py) at POST /api/v1/ai/chat
 */
(function () {
  const OPENAI_KEY_STORAGE = "rmm_openai_api_key";
  const OPENAI_MODEL_STORAGE = "rmm_openai_model";
  const OPENAI_MODEL_CUSTOM_STORAGE = "rmm_openai_model_custom";
  const AI_PANEL_OPEN_STORAGE = "rmm_ai_panel_open";
  const EXEGOL_ENABLED_STORAGE = "rmm_exegol_mcp_enabled";
  const EXEGOL_URL_STORAGE = "rmm_exegol_mcp_url";
  const EXEGOL_TOKEN_STORAGE = "rmm_exegol_mcp_token";
  const AI_SKILLS_ENABLED_STORAGE = "rmm_ai_skills_enabled";
  const DEFAULT_EXEGOL_MCP_URL = "http://127.0.0.1:8000/mcp";
  const CUSTOM_MODEL_VALUE = "__custom__";
  const DEFAULT_MODEL =
    (typeof window !== "undefined" && window.RMM_OPENAI_DEFAULT_MODEL) || "gpt-5.2";

  const $ = (sel) => document.querySelector(sel);

  let chatHistory = [];
  let sending = false;
  let aiSkills = [];

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function getOpenAiKey() {
    return ($("#openai-key-input")?.value || sessionStorage.getItem(OPENAI_KEY_STORAGE) || "").trim();
  }

  function getModel() {
    const sel = $("#openai-model-select");
    const custom = $("#openai-model-custom");
    if (sel?.value === CUSTOM_MODEL_VALUE) {
      return (custom?.value || sessionStorage.getItem(OPENAI_MODEL_CUSTOM_STORAGE) || "").trim();
    }
    return sel ? sel.value : sessionStorage.getItem(OPENAI_MODEL_STORAGE) || DEFAULT_MODEL;
  }

  function syncCustomModelField() {
    const sel = $("#openai-model-select");
    const custom = $("#openai-model-custom");
    if (!sel || !custom) return;
    const show = sel.value === CUSTOM_MODEL_VALUE;
    custom.classList.toggle("hidden", !show);
    if (show) custom.focus();
  }

  function populateModelSelect() {
    const sel = $("#openai-model-select");
    if (!sel) return;
    const groups = window.RMM_OPENAI_MODEL_GROUPS || [];
    sel.replaceChildren();
    for (const group of groups) {
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.label;
      for (const model of group.models || []) {
        const opt = document.createElement("option");
        opt.value = model.id;
        opt.textContent = model.id === CUSTOM_MODEL_VALUE ? "Custom model ID…" : model.id;
        opt.title = model.hint || model.id;
        optgroup.appendChild(opt);
      }
      sel.appendChild(optgroup);
    }
  }

  function restoreModelSelection() {
    const sel = $("#openai-model-select");
    const custom = $("#openai-model-custom");
    if (!sel) return;
    const saved = sessionStorage.getItem(OPENAI_MODEL_STORAGE) || DEFAULT_MODEL;
    const savedCustom = sessionStorage.getItem(OPENAI_MODEL_CUSTOM_STORAGE) || "";
    const known = Array.from(sel.options).some((o) => o.value === saved);
    if (known) {
      sel.value = saved;
    } else if (saved) {
      sel.value = CUSTOM_MODEL_VALUE;
      if (custom) custom.value = saved;
    } else {
      sel.value = DEFAULT_MODEL;
    }
    if (custom && savedCustom) custom.value = savedCustom;
    syncCustomModelField();
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
    return loadEnabledSkillIds().filter((id) => known.has(id));
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

  function renderToolCalls(toolCalls) {
    if (!toolCalls?.length) return "";
    return toolCalls
      .map(
        (t) =>
          `<div class="ai-msg-tool">→ ${escapeHtml(t.name)}(${escapeHtml(JSON.stringify(t.arguments || {}))})</div>`
      )
      .join("");
  }

  async function sendAiMessage(text) {
    if (sending || !text.trim()) return;
    const apiFn = window.rmmApi;
    const st = window.rmmState;
    if (!apiFn || !st?.token) {
      appendChatMessage("error", "Connect to RMM first (API token required).");
      return;
    }
    const openaiKey = getOpenAiKey();
    if (!openaiKey) {
      appendChatMessage("error", "Set your OpenAI API key in the panel settings.");
      return;
    }

    sessionStorage.setItem(OPENAI_KEY_STORAGE, openaiKey);
    const model = getModel();
    if (!model) {
      appendChatMessage("error", "Choose a model or enter a custom model ID.");
      return;
    }
    sessionStorage.setItem(OPENAI_MODEL_STORAGE, $("#openai-model-select")?.value || model);
    if ($("#openai-model-select")?.value === CUSTOM_MODEL_VALUE) {
      sessionStorage.setItem(OPENAI_MODEL_CUSTOM_STORAGE, model);
    }
    persistExegolMcpSettings();
    const exegol = getExegolMcpSettings();

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
          openai_api_key: openaiKey,
          model: getModel(),
          messages: chatHistory,
          selected_session_id: st.selectedId || null,
          exegol_mcp_enabled: exegol.enabled,
          exegol_mcp_url: exegol.enabled ? exegol.url : null,
          exegol_mcp_token: exegol.enabled ? exegol.token || null : null,
          skill_ids: getEnabledSkillIds(),
        }),
      });

      if (status !== 200 || !data.ok) {
        const err =
          data.detail || data.error || (typeof data.message === "string" ? data.message : `HTTP ${status}`);
        appendChatMessage("error", String(err));
        return;
      }

      const reply = data.message || "(empty response)";
      const toolsHtml = renderToolCalls(data.tool_calls_made);
      chatHistory.push({ role: "assistant", content: reply });
      appendChatMessage("assistant", reply, toolsHtml);
    } catch (e) {
      appendChatMessage("error", e.message || String(e));
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
    const keyInput = $("#openai-key-input");
    const modelSelect = $("#openai-model-select");
    const modelCustom = $("#openai-model-custom");
    populateModelSelect();
    if (keyInput) {
      keyInput.value = sessionStorage.getItem(OPENAI_KEY_STORAGE) || "";
      keyInput.addEventListener("change", () => {
        sessionStorage.setItem(OPENAI_KEY_STORAGE, keyInput.value.trim());
      });
    }
    if (modelSelect) {
      restoreModelSelection();
      modelSelect.addEventListener("change", () => {
        sessionStorage.setItem(OPENAI_MODEL_STORAGE, modelSelect.value);
        syncCustomModelField();
      });
    }
    if (modelCustom) {
      modelCustom.addEventListener("change", () => {
        sessionStorage.setItem(OPENAI_MODEL_CUSTOM_STORAGE, modelCustom.value.trim());
      });
    }

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

    $("#ai-chat-form")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("#ai-chat-input");
      if (input) sendAiMessage(input.value);
    });

    const exegolOn = sessionStorage.getItem(EXEGOL_ENABLED_STORAGE) === "1";
    appendChatMessage(
      "assistant",
      exegolOn
        ? "RMM tools (sessions, commands, config) plus Exegol MCP (containers, in-container pentest tools). Select an RMM session in the sidebar for beacon context."
        : "RMM tools via MCP (list sessions, run commands, change beacon sleep, etc.). Enable Exegol MCP in settings to add container orchestration and offensive tools."
    );
    chatHistory = [];
    fetchAiSkills().catch(() => {});
  }

  window.initAiPanel = initAiPanel;
  window.fetchAiSkills = fetchAiSkills;
})();

/**
 * Web AI assistant — OpenAI chat via MCP (mcp_rmm_server.py) at POST /api/v1/ai/chat
 */
(function () {
  const OPENAI_KEY_STORAGE = "rmm_openai_api_key";
  const OPENAI_MODEL_STORAGE = "rmm_openai_model";
  const AI_PANEL_OPEN_STORAGE = "rmm_ai_panel_open";
  const EXEGOL_ENABLED_STORAGE = "rmm_exegol_mcp_enabled";
  const EXEGOL_URL_STORAGE = "rmm_exegol_mcp_url";
  const EXEGOL_TOKEN_STORAGE = "rmm_exegol_mcp_token";
  const DEFAULT_EXEGOL_MCP_URL = "http://127.0.0.1:8000/mcp";

  const $ = (sel) => document.querySelector(sel);

  let chatHistory = [];
  let sending = false;

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
    return sel ? sel.value : sessionStorage.getItem(OPENAI_MODEL_STORAGE) || "gpt-4o-mini";
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
    sessionStorage.setItem(OPENAI_MODEL_STORAGE, getModel());
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
    if (keyInput) {
      keyInput.value = sessionStorage.getItem(OPENAI_KEY_STORAGE) || "";
      keyInput.addEventListener("change", () => {
        sessionStorage.setItem(OPENAI_KEY_STORAGE, keyInput.value.trim());
      });
    }
    if (modelSelect) {
      const saved = sessionStorage.getItem(OPENAI_MODEL_STORAGE);
      if (saved) modelSelect.value = saved;
      modelSelect.addEventListener("change", () => {
        sessionStorage.setItem(OPENAI_MODEL_STORAGE, modelSelect.value);
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
  }

  window.initAiPanel = initAiPanel;
})();

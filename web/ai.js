/**
 * Web AI assistant — OpenAI chat via MCP (mcp_rmm_server.py) at POST /api/v1/ai/chat
 */
(function () {
  const OPENAI_KEY_STORAGE = "rmm_openai_api_key";
  const OPENAI_MODEL_STORAGE = "rmm_openai_model";
  const AI_PANEL_OPEN_STORAGE = "rmm_ai_panel_open";

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

    appendChatMessage(
      "assistant",
      "I control RMM agents through the MCP server (list sessions, run commands, change beacon sleep, etc.). Select a session in the sidebar for context."
    );
    chatHistory = [];
  }

  window.initAiPanel = initAiPanel;
})();

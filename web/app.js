/**
 * Minimal RMM web operator UI — REST API + WebSocket event stream.
 */
const STORAGE_KEY = "rmm_api_token";
const EVENT_CURSORS_KEY = "rmm_event_cursors";

const state = {
  token: sessionStorage.getItem(STORAGE_KEY) || "",
  sessions: [],
  selectedId: null,
  lastEventId: 0,
  localEventSeq: 0,
  ws: null,
  wsConnected: false,
  pollTimer: null,
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

function formatAgo(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "?";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function statusClass(status) {
  if (status === "online") return "status-online";
  if (status === "stale") return "status-stale";
  if (status === "offline") return "status-offline";
  return "";
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

  ws.onopen = () => {
    setWsStatus(true);
    if (state.selectedId) {
      wsSubscribe(state.selectedId);
    }
  };

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

function stopEventPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollSessionEvents() {
  if (!state.selectedId || !state.token) return;
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/events?since=${state.lastEventId}&limit=100`
  );
  if (status === 401) {
    disconnect();
    return;
  }
  if (status !== 200) return;
  for (const ev of data.events || []) {
    appendEvent(ev);
  }
}

function startEventPolling() {
  stopEventPolling();
  state.pollTimer = setInterval(() => {
    pollSessionEvents().catch(() => {});
  }, 2500);
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
  startEventPolling();
  await refreshSessions();
}

function disconnect() {
  disconnectWebSocket();
  stopEventPolling();
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
    const ago =
      s.last_seen_ago_seconds != null
        ? formatAgo(s.last_seen_ago_seconds)
        : formatTime(s.last_seen);
    const st = s.beacon_status || "?";
    li.innerHTML = `
      <div class="id">${escapeHtml(s.id.slice(0, 8))} <span class="beacon-status ${statusClass(st)}">${escapeHtml(st)}</span></div>
      <div class="meta">${escapeHtml(s.username)}@${escapeHtml(s.hostname)}</div>
      <div class="sub">sleep ${s.sleep_seconds}s · jitter ${s.jitter_percent}% · ${escapeHtml(ago)}</div>
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
  startEventPolling();

  const { status, data } = await api(
    `/sessions/${encodeURIComponent(id)}/events?since=0&limit=500`
  );
  if (status === 200) {
    const events = data.events || [];
    for (const ev of events) {
      appendEvent(ev);
    }
    if (events.length) {
      state.lastEventId = events.reduce((m, e) => Math.max(m, e.id || 0), 0);
      saveEventCursor(id, state.lastEventId);
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

function appendLocalEvent(partial) {
  appendEvent(
    {
      id: `local-${++state.localEventSeq}`,
      timestamp: new Date().toISOString(),
      ...partial,
    },
    { local: true }
  );
}

function loadEventCursors() {
  try {
    return JSON.parse(sessionStorage.getItem(EVENT_CURSORS_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveEventCursor(sessionId, eventId) {
  if (!sessionId || typeof eventId !== "number") return;
  const cursors = loadEventCursors();
  cursors[sessionId] = Math.max(cursors[sessionId] || 0, eventId);
  sessionStorage.setItem(EVENT_CURSORS_KEY, JSON.stringify(cursors));
}

function appendEvent(ev, { local = false } = {}) {
  if (!local) {
    if (typeof ev.id === "number" && ev.id <= state.lastEventId) return;
    if (typeof ev.id === "number") {
      state.lastEventId = Math.max(state.lastEventId, ev.id);
      if (state.selectedId) {
        saveEventCursor(state.selectedId, state.lastEventId);
      }
    }
  }
  const log = $("#output-log");
  const block = document.createElement("div");
  block.className = "output-line" + (local ? " output-line-local" : "");
  const cmd = ev.command ? ` » ${ev.command}` : "";
  const idLabel = local ? "·" : `[${ev.id}]`;
  const typeClass = ev.type === "operator" ? "ev-operator" : ev.type === "output" ? "ev-output" : "";
  block.innerHTML = `
    <div class="head ${typeClass}">${idLabel} ${escapeHtml(ev.type)}${escapeHtml(cmd)} · ${formatTime(ev.timestamp)}</div>
    <div class="body">${renderEventBody(ev)}</div>
  `;
  log.appendChild(block);
  log.scrollTop = log.scrollHeight;
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
        appendLocalEvent({
          type: "error",
          body: "Command timed out (beacon interval may be long — try Queue + wait for poll)",
          command: cmd,
        });
      } else if (status === 200 && data.event) {
        appendEvent(data.event);
      } else {
        appendLocalEvent({
          type: "error",
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
        appendLocalEvent({
          type: "queued",
          body: "Command queued — result appears after the next agent beacon (polling + WebSocket)",
          command: cmd,
        });
        pollSessionEvents().catch(() => {});
      } else {
        appendLocalEvent({
          type: "error",
          body: data.error || `HTTP ${status}`,
          command: cmd,
        });
      }
    }
  } finally {
    btnRun.disabled = false;
    btnExec.disabled = false;
    $("#command-input").value = "";
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
  appendLocalEvent({
    type: status === 200 ? "queued" : "error",
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
  appendLocalEvent({
    type: status === 200 ? "queued" : "error",
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
  appendLocalEvent({
    type: status === 200 ? "queued" : "error",
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
      // Enter = run and wait; Shift+Enter = newline; Queue button = queue only
      runCommand(true);
    }
  });

  if (state.token) {
    $("#token-input").value = state.token;
    connect();
  } else {
    showLogin();
  }
});

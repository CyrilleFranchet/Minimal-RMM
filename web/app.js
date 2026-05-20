/**
 * Minimal RMM web operator UI — uses /api/v1/ on same origin.
 */
const STORAGE_KEY = "rmm_api_token";

const state = {
  token: sessionStorage.getItem(STORAGE_KEY) || "",
  sessions: [],
  selectedId: null,
  lastEventId: 0,
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
  await refreshSessions();
  startSessionPoll();
}

function disconnect() {
  stopPolling();
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
    li.dataset.id = s.id;
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

  await loadEvents();
  startEventPoll();
}

function showEmptyConsole() {
  state.selectedId = null;
  stopEventPoll();
  show($("#empty-state"));
  hide($("#console-panel"));
  renderSessionList();
}

function appendEvent(ev) {
  const log = $("#output-log");
  const block = document.createElement("div");
  block.className = "output-line";
  const cmd = ev.command ? ` » ${ev.command}` : "";
  const artifact = ev.artifact
    ? `\n[artifact] ${ev.artifact}`
    : "";
  block.innerHTML = `
    <div class="head">[${ev.id}] ${escapeHtml(ev.type)}${escapeHtml(cmd)} · ${formatTime(ev.timestamp)}</div>
    <div class="body">${escapeHtml(String(ev.body || ""))}${escapeHtml(artifact)}</div>
  `;
  log.appendChild(block);
  log.scrollTop = log.scrollHeight;
  if (ev.id > state.lastEventId) state.lastEventId = ev.id;
}

async function loadEvents() {
  if (!state.selectedId) return;
  const { status, data } = await api(
    `/sessions/${encodeURIComponent(state.selectedId)}/events?since=${state.lastEventId}&limit=100`
  );
  if (status !== 200) return;
  for (const ev of data.events || []) {
    appendEvent(ev);
  }
}

function startSessionPoll() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshSessions, 5000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  stopEventPoll();
}

let eventPollTimer = null;

function startEventPoll() {
  stopEventPoll();
  eventPollTimer = setInterval(loadEvents, 2000);
}

function stopEventPoll() {
  if (eventPollTimer) {
    clearInterval(eventPollTimer);
    eventPollTimer = null;
  }
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
          body: "Command queued — output will appear when the agent beacons",
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
    body: JSON.stringify({
      sleep_seconds: sleep,
      jitter_percent: jitter,
    }),
  });
  await refreshSessions();
}

// --- Init ---

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

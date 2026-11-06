/** Read-only, owner-scoped AI job activity; never render raw tool content. */
(function () {
  const jobs = new Map();
  const evidence = new Map();
  let generation = 0;
  let authIdentity;
  let refreshTimer, refreshVersion = 0, refreshFeedback;
  let dialog, content, select, feedback, selectedId, fingerprint;
  const label = value => typeof value === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(value) ? value : "";
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  const timestamp = value => typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : undefined;
  const statuses = { running: "Call started", completed: "Response received", outcome_unknown: "Outcome unknown", failed: "Failed" };

  function diagnostics(value) {
    return (Array.isArray(value) ? value : []).slice(0, 32).map(item => ({
      type: label(item?.type),
      ...(count(item?.http_status) !== undefined ? { http_status: item.http_status } : {}),
      traceback_lines: (Array.isArray(item?.traceback_lines) ? item.traceback_lines : []).filter(n => count(n) !== undefined).slice(-12),
    }));
  }

  function snapshot(job) {
    const progress = {};
    for (const key of ["phase", "status", "tool", "tool_path", "mcp_status", "exegol_status"]) {
      if (label(job.progress?.[key])) progress[key] = label(job.progress[key]);
    }
    for (const key of ["round", "step", "context_compactions"]) {
      if (count(job.progress?.[key]) !== undefined) progress[key] = job.progress[key];
    }
    return {
      id: label(job.id), session_id: label(job.session_id), status: label(job.status),
      created_at: timestamp(job.created_at), started_at: timestamp(job.started_at),
      completed_at: timestamp(job.completed_at), updated_at: timestamp(job.updated_at), progress,
      activity_omitted: count(job.activity_omitted) || 0,
      activity: (Array.isArray(job.activity) ? job.activity : []).slice(-500).map(event => {
        const safe = { timestamp: timestamp(event.timestamp) };
        for (const key of ["phase", "status", "tool", "call_id", "tool_path", "backend", "mcp_status", "exegol_status", "artifact_id", "arguments_artifact_id"]) {
          if (label(event[key])) safe[key] = label(event[key]);
        }
        for (const key of ["round", "duration_ms", "output_bytes"]) {
          if (count(event[key]) !== undefined) safe[key] = event[key];
        }
        if (Array.isArray(event.arguments_keys)) safe.arguments_keys = event.arguments_keys.map(label).filter(Boolean).slice(0, 50);
        if (event.diagnostics) safe.diagnostics = { exception_chain: diagnostics(event.diagnostics.exception_chain) };
        return safe;
      }),
      diagnostics: { exception_chain: diagnostics(job.result?.diagnostics?.exception_chain) },
    };
  }

  function node(tag, text, parent) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    parent?.appendChild(element);
    return element;
  }

  function stopRefresh() {
    refreshVersion += 1;
    clearTimeout(refreshTimer);
  }

  function scheduleRefresh() {
    const version = refreshVersion;
    const scope = generation;
    const id = selectedId;
    const identity = authIdentity;
    const current = () => dialog?.open && version === refreshVersion && scope === generation && selectedId === id &&
      identity === `${window.rmmState?.authGeneration || 0}/${window.rmmState?.connectionGeneration || 0}`;
    refreshTimer = setTimeout(async () => {
      if (!current()) return;
      try {
        const { status, data } = await window.rmmApi(`/ai/jobs/${encodeURIComponent(id)}`);
        if (!current()) return;
        if (status !== 200 || data?.job?.id !== id) throw new Error("Job unavailable");
        remember(data.job);
        refreshFeedback.textContent = "Details update automatically while this dialog is open. Refreshing does not repeat commands.";
      } catch {
        if (!current()) return;
        refreshFeedback.textContent = "Could not refresh operation details. Showing the last recorded data; reconnecting without repeating commands…";
      }
      if (current()) scheduleRefresh();
    }, 2000);
  }

  function render() {
    const job = jobs.get(selectedId);
    if (!dialog?.open || !job) return;
    const next = JSON.stringify(job);
    if (next === fingerprint) return;
    fingerprint = next;
    const expanded = new Set(Array.from(content.querySelectorAll("details[open]"), d => d.dataset.event));
    const focused = document.activeElement?.dataset?.focus;
    const scroll = content.scrollTop;
    content.replaceChildren();
    node("h3", `${job.status || "Unknown status"} · ${job.id}`, content);
    const overview = node("dl", undefined, content);
    for (const [key, value] of Object.entries({ Session: job.session_id || "Sessionless operation", Created: job.created_at,
      Started: job.started_at, Updated: job.updated_at, Completed: job.completed_at,
      "Last recorded activity": Object.entries(job.progress).map(([k, v]) => `${k}: ${v}`).join(" · ") })) {
      if (!value) continue;
      node("dt", key, overview); node("dd", value, overview);
    }
    if (job.started_at) {
      const duration = Math.max(0, (Date.parse(job.completed_at || job.updated_at) || Date.now()) - Date.parse(job.started_at));
      node("p", `Elapsed at last update: ${(duration / 1000).toFixed(1)} seconds`, content);
    }
    node("p", "A call start does not prove execution. Response received means the tool returned; inspect its result before claiming success. Interrupted calls may continue remotely.", content);
    node("h3", "Activity timeline", content);
    if (job.activity_omitted) node("p", `${job.activity_omitted} older events are outside the retained timeline.`, content);
    if (!job.activity.length) node("p", "No activity events were recorded for this operation. Older jobs cannot reconstruct missing events.", content);
    else {
      node("p", "Showing the most recent recorded events (up to 500). Command arguments and output may contain secrets. Load protected evidence explicitly; it is excluded from Copy diagnostics.", content);
      const list = node("ol", undefined, content);
      const terminal = !["queued", "running"].includes(job.status);
      const ended = new Set(job.activity.filter(event => event.status !== "running").map(event => event.call_id).filter(Boolean));
      job.activity.forEach((event, index) => {
        const eventKey = `${event.call_id || "event"}/${event.status || ""}/${event.timestamp || index}`;
        const item = node("li", undefined, list);
        const details = node("details", undefined, item);
        details.dataset.event = eventKey;
        details.open = expanded.has(eventKey);
        const unfinished = terminal && event.phase === "tool" && event.status === "running" && !ended.has(event.call_id);
        const summary = node("summary", [event.timestamp, event.backend, event.tool || event.phase || "Activity",
          unfinished ? "Outcome unknown (no final event recorded)" : statuses[event.status] || event.status,
          event.duration_ms !== undefined ? `${event.duration_ms} ms` : ""].filter(Boolean).join(" · "), details);
        summary.dataset.event = eventKey;
        summary.dataset.focus = eventKey;
        node("pre", JSON.stringify(event, null, 2), details);
        for (const [field, title] of [["arguments_artifact_id", "command arguments"], ["artifact_id", "tool output"]]) {
          if (event[field]) evidenceView(details, job.id, event[field], title);
        }
      });
    }
    node("h3", "Failure diagnostics", content);
    node("pre", job.diagnostics.exception_chain.length ? JSON.stringify(job.diagnostics, null, 2) : "No exception diagnostics recorded.", content);
    content.scrollTop = scroll;
    if (focused !== undefined) Array.from(content.querySelectorAll("*")).find(item => item.dataset.focus === focused)?.focus();
  }

  function evidenceView(parent, jobId, artifactId, title) {
    const key = `${jobId}/${artifactId}`;
    const cached = evidence.get(key);
    node("pre", cached?.content || "", parent);
    const button = node("button", cached?.pending ? "Loading…" : cached?.error ? `Could not load ${title}. Retry`
      : cached?.done ? "End of preview (up to 65,536 characters)" : cached?.content ? `Load more ${title}` : `Load ${title}`, parent);
    button.type = "button";
    button.dataset.focus = key;
    button.disabled = !!(cached?.done || cached?.pending);
    button.addEventListener("click", async () => {
      const version = generation;
      const previous = evidence.get(key);
      if (previous?.pending || previous?.done) return;
      const offset = previous?.offset || 0;
      evidence.set(key, { ...previous, pending: true, error: false });
      button.disabled = true;
      button.textContent = "Loading…";
      try {
        const { status, data } = await window.rmmApi(`/ai/jobs/${encodeURIComponent(jobId)}/evidence/${encodeURIComponent(artifactId)}?offset=${offset}&limit=4096`);
        if (version !== generation) return;
        const page = data?.evidence;
        if (status !== 200 || typeof page?.content !== "string" ||
            (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset))) throw new Error("Evidence unavailable");
        const text = ((previous?.content || "") + page.content).slice(0, 65536);
        const done = page.next_offset === null || page.next_offset >= page.characters || text.length >= 65536;
        evidence.set(key, { content: text, offset: page.next_offset, done, pending: false, error: false });
        if (selectedId === jobId) { fingerprint = undefined; render(); }
      } catch {
        if (version !== generation) return;
        evidence.set(key, { ...previous, pending: false, error: true });
        if (selectedId === jobId) { fingerprint = undefined; render(); }
      }
    });
  }

  function ensureDialog() {
    if (dialog) return;
    dialog = node("dialog", undefined, document.body);
    dialog.id = "ai-operation-dialog";
    dialog.className = "ai-operation-dialog";
    dialog.setAttribute("aria-labelledby", "ai-operation-title");
    const header = node("div", undefined, dialog);
    header.className = "ai-operation-header";
    node("h2", "Operation details", header).id = "ai-operation-title";
    const close = node("button", "Close", header);
    close.type = "button";
    close.addEventListener("click", () => { stopRefresh(); dialog.close(); });
    dialog.addEventListener("close", stopRefresh);
    const controls = node("div", undefined, dialog);
    controls.className = "ai-operation-controls";
    node("label", "Operation", controls).htmlFor = "ai-operation-select";
    select = node("select", undefined, controls);
    select.id = "ai-operation-select";
    select.addEventListener("change", () => { stopRefresh(); selectedId = select.value; render(); scheduleRefresh(); });
    const copy = node("button", "Copy diagnostics", controls);
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(JSON.stringify(jobs.get(selectedId), null, 2));
        feedback.textContent = "Diagnostics copied. Commands and output content are excluded.";
      } catch { feedback.textContent = "Clipboard unavailable. Expand the timeline to inspect recorded metadata."; }
    });
    feedback = node("p", "", dialog);
    feedback.className = "ai-operation-feedback";
    feedback.setAttribute("role", "status");
    refreshFeedback = node("p", "", dialog);
    refreshFeedback.className = "ai-operation-feedback";
    refreshFeedback.setAttribute("role", "status");
    content = node("div", undefined, dialog);
    content.className = "ai-operation-content";
    content.tabIndex = 0;
    content.setAttribute("aria-label", "Operation activity and diagnostics");
  }

  function remember(job) {
    if (!label(job?.id)) return;
    const currentIdentity = `${window.rmmState?.authGeneration || 0}/${window.rmmState?.connectionGeneration || 0}`;
    if (authIdentity !== undefined && authIdentity !== currentIdentity) reset();
    authIdentity = currentIdentity;
    const previous = jobs.get(job.id);
    if (previous?.updated_at && job.updated_at && Date.parse(job.updated_at) < Date.parse(previous.updated_at)) return;
    jobs.set(job.id, snapshot(job));
    if (jobs.size > 50) jobs.delete(jobs.keys().next().value);
    if (dialog?.open) { updateOptions(); render(); }
  }

  function updateOptions() {
    select.replaceChildren();
    for (const job of Array.from(jobs.values()).sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0))) {
      const option = node("option", `${job.created_at || job.id} · ${job.status}`, select);
      option.value = job.id;
    }
    select.value = selectedId;
  }

  function show(job) {
    stopRefresh();
    remember(job);
    ensureDialog();
    selectedId = job.id;
    feedback.textContent = "";
    refreshFeedback.textContent = "Details update automatically while this dialog is open. Refreshing does not repeat commands.";
    updateOptions();
    if (!dialog.open) dialog.showModal();
    render();
    scheduleRefresh();
  }

  function reset() {
    stopRefresh();
    generation += 1;
    dialog?.close();
    jobs.clear();
    evidence.clear();
    selectedId = undefined;
    fingerprint = undefined;
    content?.replaceChildren();
  }

  window.rmmAiOperationDetails = { remember, show, reset };
})();

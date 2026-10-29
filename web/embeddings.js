/* Saved document embedding profiles and explicitly selected result collections. */
(function () {
  const $ = (id) => document.getElementById(`embedding-${id}`);
  let profiles = [];
  let eventSession = "";
  let eventCursor = 0;
  let labelRows = [];
  let labelSerial = 0;
  const modelFields = ["repo", "revision", "onnx_file", "tokenizer_file", "sha256", "input_names", "output_name", "pooling"];
  let defaultProfile = null;

  function message(value, error = false) {
    $("status").textContent = value;
    $("status").classList.toggle("error", error);
  }

  async function request(path, method = "GET", body) {
    const result = await window.rmmApi(`/embeddings/${path}`, {
      method, headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (result.status !== 200) throw new Error(result.data?.error || `HTTP ${result.status}`);
    return result.data;
  }

  function modelFromForm() {
    const model = {};
    for (const key of modelFields) model[key] = $(key).value.trim();
    model.input_names = model.input_names.split(",").map((x) => x.trim()).filter(Boolean);
    return model;
  }

  function fillModel(model) {
    for (const key of modelFields) {
      const value = model?.[key] ?? "";
      $(key).value = Array.isArray(value) ? value.join(", ") : value;
    }
    syncPreset();
  }

  function syncPreset() {
    const preset = defaultProfile && JSON.stringify(modelFromForm()) === JSON.stringify(Object.fromEntries(modelFields.map((key) => [key, defaultProfile.model[key]])));
    $("preset").value = preset ? "default" : "custom";
    $("model-help").textContent = preset
      ? `Repository default: ${defaultProfile.model.repo}. Its settings come from the server or workspace; no manual model configuration is needed.`
      : "Custom model settings are preserved in Advanced settings. Check that the model is compatible with the plugin before running.";
  }

  function addLabel(label = { name: "", text: "" }, selected = false) {
    const id = ++labelSerial;
    const container = document.createElement("div"); container.className = "embedding-label-row";
    const fields = [];
    for (const [key, title, tag] of [["name", "Label name", "input"], ["text", "Description", "textarea"]]) {
      const wrapper = document.createElement("div");
      const caption = document.createElement("label"); caption.textContent = title; caption.htmlFor = `embedding-label-${id}-${key}`;
      const field = document.createElement(tag); field.id = caption.htmlFor; field.value = label[key];
      if (key === "text") field.rows = 2;
      field.addEventListener("input", updateEditor);
      wrapper.append(caption, field); container.append(wrapper); fields.push(field);
    }
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = "Remove label";
    container.append(remove);
    const copyContainer = document.createElement("label");
    const copy = document.createElement("input"); copy.type = "checkbox"; copy.checked = selected;
    const copyCaption = document.createElement("span");
    copyContainer.append(copy, copyCaption); copy.addEventListener("change", updateSummary);
    const row = { container, name: fields[0], text: fields[1], copyContainer, copy, copyCaption };
    remove.addEventListener("click", () => {
      labelRows = labelRows.filter((item) => item !== row);
      renderLabels(); $("add-label").focus();
    });
    labelRows.push(row);
    return row;
  }

  function renderLabels() {
    $("labels").replaceChildren(...labelRows.map((row) => row.container));
    $("copy_labels").replaceChildren(...labelRows.map((row) => row.copyContainer));
    updateEditor();
  }

  function updateEditor() {
    $("label-empty").hidden = labelRows.length > 0;
    $("add-label").disabled = labelRows.length >= 16;
    for (const row of labelRows) row.copyCaption.textContent = ` ${row.name.value || "Unnamed label"}`;
    const copying = $("copy_documents").checked;
    $("copy-options").hidden = !copying; $("copy-options").disabled = !copying;
    $("copy-selection").hidden = $("copy-mode").value !== "selected";
    $("threshold-options").hidden = !$("threshold-enabled").checked;
    $("copy_threshold").disabled = !copying || !$("threshold-enabled").checked;
    updateSummary();
  }

  function savedFormConfig(config) {
    return { model: Object.fromEntries(modelFields.map((key) => [key, config.model[key]])), auth: config.auth,
      prompt: config.prompt ?? "", label_prompt: config.label_prompt ?? "", max_tokens: config.max_tokens ?? 256,
      labels: (config.labels || []).map(({ name, text }) => ({ name, text })),
      copy_documents: config.copy_documents ?? false, copy_labels: config.copy_labels ?? [], copy_threshold: config.copy_threshold ?? 0 };
  }

  function sameConfig(left, right) {
    const ordered = (config) => JSON.stringify({ ...config, copy_labels: [...new Set(config.copy_labels)].sort() });
    return ordered(left) === ordered(right);
  }

  function documentPaths() {
    return $("documents").value.split(/\r?\n/).map((path) => path.trim()).filter(Boolean);
  }

  function updateSummary() {
    try {
      const config = readForm();
      const saved = profiles.find((profile) => profile.name === $("profiles").value);
      const dirty = !saved || $("name").value.trim() !== saved.name || !sameConfig(config, savedFormConfig(saved.config));
      const copies = config.copy_documents
        ? `Local copies: ${config.copy_labels.length ? config.copy_labels.join(", ") : "all successfully embedded documents"}${config.copy_threshold ? `; score ≥ ${config.copy_threshold}` : ""}.`
        : "Local copies: off.";
      $("run-summary").textContent = `Session: ${$("session").value.trim() || "not selected"}. ${documentPaths().length} input paths (up to 32 expanded documents). Model: ${config.model.repo}. Labels: ${config.labels.map((label) => label.name).join(", ") || "none"}. ${copies} ${dirty ? "Save profile changes before running." : `Saved profile: ${saved.name}.`}`;
    } catch (error) {
      $("run-summary").textContent = `Finish the profile: ${error.message}`;
    }
  }

  function readForm() {
    const model = modelFromForm();
    const labels = labelRows.map((row) => ({ name: row.name.value, text: row.text.value }));
    if (labels.length > 16) throw new Error("Use at most 16 labels.");
    if (labels.some((label) => !label.name.trim() || !label.text.trim())) throw new Error("Each label needs a name and description.");
    if (new Set(labels.map((label) => label.name)).size !== labels.length) throw new Error("Label names must be unique.");
    const copying = $("copy_documents").checked;
    const selected = copying && $("copy-mode").value === "selected";
    const copyLabels = selected ? labelRows.filter((row) => row.copy.checked).map((row) => row.name.value) : [];
    if (selected && !copyLabels.length) throw new Error("Select at least one copy label, or choose all documents.");
    const threshold = copying && $("threshold-enabled").checked ? Number($("copy_threshold").value) : 0;
    if (copying && $("threshold-enabled").checked && (!Number.isFinite(threshold) || threshold <= 0 || threshold > 1 || !labels.length)) {
      throw new Error("A similarity threshold requires labels and a number greater than 0 and at most 1.");
    }
    return { model, auth: { method: $("auth").value }, prompt: $("prompt").value,
      label_prompt: $("label_prompt").value, max_tokens: Number($("max_tokens").value),
      labels, copy_documents: copying, copy_labels: copyLabels, copy_threshold: threshold };
  }

  function fillForm(config, name = "") {
    const labels = config.labels ?? [];
    const copyLabels = config.copy_labels ?? [];
    if (!Array.isArray(labels) || labels.length > 16 || labels.some((label) => !label || typeof label.name !== "string" || typeof label.text !== "string") ||
        !Array.isArray(copyLabels) || copyLabels.some((name) => typeof name !== "string" || !labels.some((label) => label.name === name))) {
      throw new Error("Profile labels must have names and descriptions, and copy labels must reference them.");
    }
    $("name").value = name;
    fillModel(config.model);
    $("advanced").open = $("preset").value === "custom";
    $("auth").value = config.auth?.method || "none";
    $("prompt").value = config.prompt || "";
    $("label_prompt").value = config.label_prompt || "";
    $("max_tokens").value = config.max_tokens || 256;
    labelRows = [];
    for (const label of labels) addLabel(label, copyLabels.includes(label.name));
    $("copy_documents").checked = config.copy_documents ?? false;
    $("copy-mode").value = copyLabels.length ? "selected" : "all";
    $("copy_threshold").value = config.copy_threshold ?? 0;
    $("threshold-enabled").checked = (config.copy_threshold ?? 0) > 0;
    renderLabels();
  }

  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function refreshProfiles(selected = $("profiles").value) {
    profiles = (await request("profiles")).profiles;
    $("profiles").replaceChildren(new Option("New profile…", ""));
    for (const profile of profiles) $("profiles").add(new Option(profile.name, profile.name));
    $("profiles").value = profiles.some((p) => p.name === selected) ? selected : "";
  }

  async function refreshResults() {
    const { results } = await request("results");
    $("results").replaceChildren();
    if (!results.length) $("results").textContent = "No saved result collections.";
    for (const result of results) {
      const row = document.createElement("div"); row.className = "asset-item";
      const label = document.createElement("span");
      label.textContent = `${result.model} · ${result.document_count} result rows · ${result.session_id} / event ${result.event_id}`;
      row.append(label);
      for (const [title, handler] of [
        ["Export JSON", async () => download(`${result.name}.json`, (await request(`results/${result.name}`)).record)],
        ["Delete", async () => {
          if (!window.confirm("Delete this saved collection? The original session event remains.")) return;
          await request(`results/${result.name}`, "DELETE"); await refreshResults();
        }],
      ]) {
        const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
        button.textContent = title; button.addEventListener("click", () => perform(button, handler)); row.append(button);
      }
      $("results").append(row);
    }
  }

  async function perform(button, action) {
    button.disabled = true;
    const running = button === $("run");
    if (running) {
      $("run-status").textContent = "Validating saved profile…";
      $("run-status").classList.toggle("error", false);
    }
    try { await action(); } catch (error) {
      message(error.message, true);
      if (running) {
        $("run-status").textContent = `Run failed: ${error.message}`;
        $("run-status").classList.toggle("error", true);
      }
    }
    finally { button.disabled = false; }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("editor").disabled = true;
    const bind = (id, action) => $(id).addEventListener("click", () => perform($(id), action));
    $("add-label").addEventListener("click", () => {
      if (labelRows.length >= 16) return;
      const row = addLabel(); renderLabels(); row.name.focus();
    });
    $("preset").addEventListener("change", () => {
      if ($("preset").value === "default") {
        fillModel(defaultProfile.model); $("auth").value = defaultProfile.auth.method;
        $("prompt").value = defaultProfile.prompt; $("label_prompt").value = defaultProfile.label_prompt; $("max_tokens").value = defaultProfile.max_tokens;
        $("advanced").open = false;
      } else {
        $("advanced").open = true;
        $("model-help").textContent = "Edit the model details below, or import an existing profile. Switching to the repository default restores its model settings and prefixes.";
      }
      updateSummary();
    });
    for (const key of modelFields) $(key).addEventListener("input", () => { syncPreset(); updateSummary(); });
    for (const id of ["copy_documents", "copy-mode", "threshold-enabled"]) $(id).addEventListener("change", updateEditor);
    for (const id of ["name", "auth", "prompt", "label_prompt", "max_tokens", "copy_threshold", "session", "documents"]) {
      $(id).addEventListener("input", updateSummary);
    }
    bind("open", async () => {
      $("dialog").showModal(); message("Loading saved workspace configuration…");
      if (!$("session").value.trim()) $("session").value = window.rmmState?.selectedId || "";
      if (!defaultProfile) {
        try {
          defaultProfile = (await request("defaults")).config;
          fillForm(defaultProfile);
          $("editor").disabled = false;
        } catch (error) {
          defaultProfile = null;
          throw new Error(`Cannot load the default profile: ${error.message}. Close and reopen to retry.`);
        }
      }
      await refreshProfiles(); await refreshResults();
      const response = await window.rmmApi("/sessions");
      $("sessions").replaceChildren();
      for (const session of response.data?.sessions || []) {
        $("sessions").append(new Option(`${session.hostname} (${session.agent_type})`, session.id));
      }
      message("Profiles and saved results persist in the active server or workspace database.");
      updateSummary();
    });
    $("close").addEventListener("click", () => $("dialog").close());
    $("profiles").addEventListener("change", () => {
      const profile = profiles.find((p) => p.name === $("profiles").value);
      fillForm(profile?.config || defaultProfile, profile?.name || "");
    });
    bind("save", async () => {
      const name = $("name").value.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)) throw new Error("Use a profile name with letters, digits, dots, underscores, or hyphens (maximum 64).");
      await request(`profiles/${encodeURIComponent(name)}`, "POST", { config: readForm() });
      await refreshProfiles(name); updateSummary(); message(`Saved ${name}. It will survive browser and service restarts.`);
    });
    bind("delete", async () => {
      const name = $("profiles").value;
      if (!name) throw new Error("Select a saved profile first.");
      if (!window.confirm(`Delete profile ${name}?`)) return;
      await request(`profiles/${encodeURIComponent(name)}`, "DELETE");
      await refreshProfiles(); fillForm(defaultProfile); message("Profile deleted.");
    });
    bind("export", async () => download(`${$("name").value || "embedding-profile"}.json`, { config: readForm() }));
    $("import").addEventListener("change", () => perform($("import"), async () => {
      const file = $("import").files[0]; if (!file) return;
      if (file.size > 65536) throw new Error("Profile files must be under 64 KiB.");
      let value = JSON.parse(await file.text()); value = value.record || value;
      fillForm(value.config || value, value.name || ""); $("profiles").value = "";
      updateSummary();
      message("Profile imported into the form. Save it to persist it.");
    }));
    bind("run", async () => {
      const profile = $("profiles").value;
      if (!profile) throw new Error("Save and select a profile before running.");
      const saved = profiles.find((p) => p.name === profile);
      if (!saved || $("name").value.trim() !== saved.name || !sameConfig(readForm(), savedFormConfig(saved.config))) {
        throw new Error("Save your configuration changes before running.");
      }
      const documents = documentPaths();
      if (!$("session").value.trim()) throw new Error("Enter a session ID before running.");
      if (!documents.length || documents.length > 32) throw new Error("Enter 1 to 32 file or directory paths, one per line.");
      $("run-status").textContent = "Submitting embedding run…";
      const data = await request("run", "POST", { session_ref: $("session").value.trim(), profile,
        documents });
      const status = `Queued for ${data.session_id}. Inspect the session output, then save its exact event ID below.`;
      $("run-status").textContent = status;
      message(status);
    });
    bind("save-result", async () => {
      await request("results", "POST", { session_ref: $("session").value.trim(), event_id: Number($("event").value) });
      await refreshResults(); message("Selected result saved in your storage owner’s database.");
    });
    bind("load-events", async () => {
      const session = $("session").value.trim();
      if (!session) throw new Error("Enter a session ID first.");
      if (eventSession !== session) {
        eventSession = session; eventCursor = 0; $("events").replaceChildren();
      }
      let scanned = 0;
      for (let page = 0; page < 10; page++) {
        const response = await window.rmmApi(`/history/${encodeURIComponent(session)}/events?limit=1000&since=${eventCursor}`);
        if (response.status !== 200) throw new Error(response.data?.error || "Cannot load session events.");
        const events = response.data?.events || [];
        const previous = eventCursor;
        for (const event of events) {
          eventCursor = Math.max(eventCursor, Number(event.id) || 0);
          if (event.type !== "output") continue;
          try {
            const result = JSON.parse(event.body);
            if (result.model && Array.isArray(result.results)) {
              $("events").append(new Option(`${result.model} · ${result.results.length} result rows · ${event.timestamp}`, String(event.id)));
            }
          } catch { /* Other command output is not an embedding collection. */ }
        }
        scanned += events.length;
        if (events.length < 1000 || eventCursor <= previous) break;
      }
      message(`${$("events").children.length} embedding output events available; scanned ${scanned} events. Load again for additional events, or enter a known ID.`);
    });
    bind("refresh", refreshResults);
  });
})();

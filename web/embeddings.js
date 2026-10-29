/* Saved document embedding profiles and explicitly selected result collections. */
(function () {
  const $ = (id) => document.getElementById(`embedding-${id}`);
  let profiles = [];
  let eventSession = "";
  let eventCursor = 0;
  const baseModel = {
    repo: "Xenova/all-MiniLM-L6-v2", revision: "08a308f628bc9d6774b7922f319eb1b65afa1a82",
    onnx_file: "onnx/model.onnx", tokenizer_file: "tokenizer.json",
    sha256: "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e",
    input_names: ["input_ids", "attention_mask", "token_type_ids"], output_name: "last_hidden_state", pooling: "mean",
  };

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

  function readForm() {
    const model = {};
    for (const key of Object.keys(baseModel)) model[key] = $(key).value.trim();
    model.input_names = model.input_names.split(",").map((x) => x.trim()).filter(Boolean);
    return { model, auth: { method: $("auth").value }, prompt: $("prompt").value,
      label_prompt: $("label_prompt").value, max_tokens: Number($("max_tokens").value),
      labels: JSON.parse($("labels").value || "[]"),
      copy_documents: $("copy_documents").checked,
      copy_labels: JSON.parse($("copy_labels").value || "[]"),
      copy_threshold: Number($("copy_threshold").value) };
  }

  function fillForm(config, name = "") {
    $("name").value = name;
    for (const key of Object.keys(baseModel)) {
      const value = config.model?.[key] ?? "";
      $(key).value = Array.isArray(value) ? value.join(", ") : value;
    }
    $("auth").value = config.auth?.method || "none";
    $("prompt").value = config.prompt || "";
    $("label_prompt").value = config.label_prompt || "";
    $("max_tokens").value = config.max_tokens || 256;
    $("labels").value = JSON.stringify(config.labels || [], null, 2);
    $("copy_documents").checked = config.copy_documents ?? false;
    $("copy_labels").value = JSON.stringify(config.copy_labels || [], null, 2);
    $("copy_threshold").value = config.copy_threshold ?? 0;
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
    try { await action(); } catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  }

  document.addEventListener("DOMContentLoaded", () => {
    fillForm({ model: baseModel, max_tokens: 256 });
    const bind = (id, action) => $(id).addEventListener("click", () => perform($(id), action));
    bind("open", async () => {
      $("dialog").showModal(); message("Loading saved workspace configuration…");
      await refreshProfiles(); await refreshResults();
      const response = await window.rmmApi("/sessions");
      $("sessions").replaceChildren();
      for (const session of response.data?.sessions || []) {
        $("sessions").append(new Option(`${session.hostname} (${session.agent_type})`, session.id));
      }
      message("Profiles and saved results persist in the active server or workspace database.");
    });
    $("close").addEventListener("click", () => $("dialog").close());
    $("profiles").addEventListener("change", () => {
      const profile = profiles.find((p) => p.name === $("profiles").value);
      fillForm(profile?.config || { model: baseModel, max_tokens: 256 }, profile?.name || "");
    });
    bind("save", async () => {
      const name = $("name").value.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)) throw new Error("Use a profile name with letters, digits, dots, underscores, or hyphens (maximum 64).");
      await request(`profiles/${encodeURIComponent(name)}`, "POST", { config: readForm() });
      await refreshProfiles(name); message(`Saved ${name}. It will survive browser and service restarts.`);
    });
    bind("delete", async () => {
      const name = $("profiles").value;
      if (!name) throw new Error("Select a saved profile first.");
      if (!window.confirm(`Delete profile ${name}?`)) return;
      await request(`profiles/${encodeURIComponent(name)}`, "DELETE");
      await refreshProfiles(); fillForm({ model: baseModel }); message("Profile deleted.");
    });
    bind("export", async () => download(`${$("name").value || "embedding-profile"}.json`, { config: readForm() }));
    $("import").addEventListener("change", () => perform($("import"), async () => {
      const file = $("import").files[0]; if (!file) return;
      if (file.size > 65536) throw new Error("Profile files must be under 64 KiB.");
      let value = JSON.parse(await file.text()); value = value.record || value;
      fillForm(value.config || value, value.name || ""); $("profiles").value = "";
      message("Profile imported into the form. Save it to persist it.");
    }));
    bind("run", async () => {
      const profile = $("profiles").value;
      if (!profile) throw new Error("Save and select a profile before running.");
      const saved = profiles.find((p) => p.name === profile);
      if (JSON.stringify(readForm()) !== JSON.stringify({ model: Object.fromEntries(Object.keys(baseModel).map((key) => [key, saved.config.model[key]])), auth: saved.config.auth,
        prompt: saved.config.prompt, label_prompt: saved.config.label_prompt, max_tokens: saved.config.max_tokens, labels: saved.config.labels,
        copy_documents: saved.config.copy_documents ?? false, copy_labels: saved.config.copy_labels ?? [], copy_threshold: saved.config.copy_threshold ?? 0 })) {
        throw new Error("Save your configuration changes before running.");
      }
      const data = await request("run", "POST", { session_ref: $("session").value.trim(), profile,
        documents: $("documents").value.split(/\r?\n/).map((p) => p.trim()).filter(Boolean) });
      message(`Queued for ${data.session_id}. Inspect the session output, then save its exact event ID below.`);
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

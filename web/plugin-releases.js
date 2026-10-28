/* Configure plugin sources and install verified, explicitly selected releases. */
(function () {
  const $ = (id) => document.getElementById(`plugin-release-${id}`);
  const exactTag = /^plugins-(?:[a-f0-9]{40}|[a-f0-9]{12}-v\d+\.\d+)$/;
  const sourceFields = ["source-id", "source-label", "source-repository", "source-branch", "source-credential"];
  let sources = [];
  let release = null;
  let choices = [];
  let busy = false;
  let dirty = false;
  let editorPrevious = "";
  let creating = false;

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
    $("source-status").textContent = message;
    $("source-status").classList.toggle("error", error);
  }

  function source() {
    return sources.find((row) => row.id === $("source").value);
  }

  function selected() {
    return choices.filter((choice) => choice.input.checked).map((choice) => choice.row);
  }

  function syncControls() {
    const rows = selected();
    $("browse").disabled = busy || !source() || dirty;
    $("tag").disabled = busy;
    $("source").disabled = busy;
    $("source-refresh").disabled = busy;
    $("source-new").disabled = busy;
    $("source-edit").disabled = busy || !source();
    $("source-cancel").disabled = busy;
    $("source-save").disabled = busy;
    $("source-remove").disabled = busy || !source() || dirty;
    for (const id of sourceFields) $(id).disabled = busy;
    $("source-id").readOnly = !creating;
    $("name").disabled = busy || !release?.plugins?.length;
    $("install").disabled = busy || !rows.length || dirty;
    $("install").textContent = `Install selected (${rows.length})`;
    for (const id of ["select-all", "select-none"]) $(id).disabled = busy || !choices.length || dirty;
    $("digest").textContent = release ? `${rows.length} of ${choices.length} selected. Each download is verified against its published SHA-256.` : "Load plugins to see the available downloads.";
  }

  function clearRelease() {
    release = null;
    choices = [];
    $("name").replaceChildren();
    $("info").textContent = "";
    syncControls();
  }

  function editSource() {
    const row = source();
    $("source-id").value = row?.id || "";
    $("source-label").value = row?.label || "";
    $("source-repository").value = row?.repository || "";
    $("source-branch").value = row?.branch || "main";
    $("source-credential").value = row?.credential_env || "RMM_PLUGIN_GITHUB_TOKEN";
    $("source-auth").textContent = row ? (row.credential_configured ? `GitHub credential detected in ${row.credential_env}. Repository access is checked when you load plugins.` : `GitHub credential missing. Set ${row.credential_env} on the workspace or server process, then restart that service. This field takes the variable name, not your OAuth token. Your RMM login token is separate.`) : "";
    dirty = false;
    clearRelease();
  }

  async function loadSources(preferred) {
    if (busy || $("source-editor").open) return;
    const previous = typeof preferred === "string" ? preferred : $("source").value;
    busy = true;
    clearRelease();
    try {
      if (!window.rmmApi) throw new Error("Connect to the RMM service first.");
      const response = await window.rmmApi("/plugin-sources");
      if (response.status !== 200) throw new Error(response.data?.detail || response.data?.error || `HTTP ${response.status}`);
      sources = response.data?.sources || [];
      renderSources(previous);
      editSource();
      status(sources.length ? "Choose a source and browse its published plugins." : "Add a repository and branch to get started.");
    } catch (error) {
      status(`Could not refresh sources: ${error.message}.${sources.length ? " Previously loaded sources are still shown." : " Check the connection and try Refresh sources."}`, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  function renderSources(preferred) {
    $("source").replaceChildren();
    for (const row of sources) {
      const option = document.createElement("option");
      option.value = row.id;
      option.textContent = `${row.label || row.id} [${row.id}] · ${row.repository} · ${row.branch}`;
      $("source").appendChild(option);
    }
    $("source").value = sources.some((row) => row.id === preferred) ? preferred : sources[0]?.id || "";
  }

  function newSource() {
    if (busy) return;
    editorPrevious = $("source").value;
    creating = true;
    $("source").value = "";
    editSource();
    $("source-repository").value = "CyrilleFranchet/Minimal-RMM-plugins";
    $("source-title").textContent = "New source";
    $("source-editor").showModal();
    status("Enter a source ID, repository, and branch, then save the source.");
    $("source-id").focus?.();
  }

  function openSourceEditor() {
    if (busy || !source()) return;
    editorPrevious = $("source").value;
    creating = false;
    editSource();
    $("source-title").textContent = "Edit source";
    status("Update the source settings, then save.");
    $("source-editor").showModal();
  }

  function cancelSourceEditor() {
    if (busy) return;
    $("source-editor").close();
    $("source").value = editorPrevious;
    creating = false;
    editSource();
    status("Source editing canceled. No changes saved.");
  }

  async function saveSource() {
    if (busy) return;
    const id = $("source-id").value.trim();
    const body = { label: $("source-label").value.trim() || id, repository: $("source-repository").value.trim(),
      branch: $("source-branch").value.trim(), credential_env: $("source-credential").value.trim() };
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(id) || !body.repository || !body.branch) {
      status("Enter a source ID, owner/repository, and branch.", true);
      return;
    }
    if (creating && sources.some((row) => row.id === id)) {
      status("That Source ID already exists. Choose another ID or cancel and use Edit source.", true);
      return;
    }
    if (!/^(?:RMM_PLUGIN_GITHUB_TOKEN|RMM_PLUGIN_TOKEN_[A-Z0-9_]+)$/.test(body.credential_env)) {
      status("Enter RMM_PLUGIN_GITHUB_TOKEN or RMM_PLUGIN_TOKEN_NAME, never a secret value.", true);
      return;
    }
    busy = true;
    clearRelease();
    try {
      const response = await window.rmmApi(`/plugin-sources/${encodeURIComponent(id)}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (![200, 201].includes(response.status)) throw new Error(response.data?.detail || response.data?.error || `HTTP ${response.status}`);
      const saved = response.data?.source;
      if (!saved || saved.id !== id || saved.repository !== body.repository || saved.branch !== body.branch) {
        throw new Error("The service did not confirm the saved source. Refresh sources to verify persistence.");
      }
      sources = sources.filter((row) => row.id !== id).concat(saved);
      renderSources(id);
      creating = false;
      $("source-editor").close();
      editSource();
      status(`Saved source “${saved.label || id}” [${id}] on this service. It is now selected.`);
    } catch (error) {
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  async function removeSource() {
    const row = source();
    if (busy || dirty || !row) return;
    busy = true;
    clearRelease();
    let removed = false;
    try {
      const response = await window.rmmApi(`/plugin-sources/${encodeURIComponent(row.id)}`, { method: "DELETE" });
      if (response.status !== 200) throw new Error(response.data?.detail || response.data?.error || `HTTP ${response.status}`);
      removed = true;
    } catch (error) {
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
    if (removed) {
      $("source-editor").close();
      sources = sources.filter((item) => item.id !== row.id);
      renderSources();
      editSource();
      status(`Removed source [${row.id}]. Installed plugins are unchanged.`);
    }
  }

  async function browse() {
    const configured = source();
    if (busy || dirty || !configured) return;
    clearRelease();
    busy = true;
    syncControls();
    status("Loading published plugins…");
    try {
      const tag = $("tag").value.trim() || "latest";
      const response = await window.rmmApi(`/plugin-releases?tag=${encodeURIComponent(tag)}&source=${encodeURIComponent(configured.id)}`);
      if (response.status !== 200) throw new Error(response.data?.error || `HTTP ${response.status}`);
      const data = response.data;
      if (!exactTag.test(data?.tag || "") || !Array.isArray(data.plugins) || data.source !== configured.id
          || !/^[a-f0-9]{64}$/.test(data.source_revision || "")) {
        throw new Error("The service returned an invalid plugin release or source.");
      }
      release = data;
      $("info").textContent = `${data.repository} · ${data.branch || configured.branch} · version ${data.version} · ${data.tag}`;
      for (const row of data.plugins) {
        const label = document.createElement("label");
        label.className = "plugin-choice";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = false;
        input.addEventListener("change", syncControls);
        const text = document.createElement("span");
        text.textContent = `${row.name} · ${Math.ceil(row.size / 1024).toLocaleString()} KB`;
        label.appendChild(input);
        label.appendChild(text);
        $("name").appendChild(label);
        choices.push({ row, input });
      }
      status(data.plugins.length ? "Check the plugins you want, then select Install selected." : "This release has no plugins.");
    } catch (error) {
      clearRelease();
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  async function install() {
    const rows = selected();
    if (busy || dirty || !rows.length) return;
    const { tag, source: sourceId, source_revision: sourceRevision } = release;
    busy = true;
    syncControls();
    const failures = [];
    let installed = 0;
    try {
      for (const row of rows) {
        status(`Downloading and verifying ${row.name} (${installed + failures.length + 1}/${rows.length})…`);
        try {
          const response = await window.rmmApi("/plugin-releases/install", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: row.name, tag, sha256: row.sha256, source: sourceId, source_revision: sourceRevision }),
          });
          if (![200, 201].includes(response.status)) {
            const prefix = response.data?.local_installed ? "Installed locally, but server synchronization failed: " : "";
            throw new Error(prefix + (response.data?.error || `HTTP ${response.status}`));
          }
          installed++;
          choices.find((choice) => choice.row === row).input.checked = false;
        } catch (error) {
          failures.push(`${row.name}: ${error.message}`);
        }
      }
      status(`${installed} of ${rows.length} installed. SHA-256 verified for successful downloads.${failures.length ? " Retry checked items. " + failures.join("; ") : ""}`, failures.length > 0);
      try { await window.refreshAssets?.(); }
      catch { status("Installation finished, but the installed asset list could not refresh. " + failures.join("; "), true); }
    } finally {
      busy = false;
      syncControls();
    }
  }

  function bind() {
    $("browse")?.addEventListener("click", browse);
    $("install")?.addEventListener("click", install);
    for (const id of ["select-all", "select-none"]) $(id)?.addEventListener("click", () => {
      if (busy || dirty) return;
      for (const choice of choices) choice.input.checked = id === "select-all";
      syncControls();
    });
    $("tag")?.addEventListener("input", clearRelease);
    $("source")?.addEventListener("change", editSource);
    $("source-refresh")?.addEventListener("click", loadSources);
    $("source-new")?.addEventListener("click", newSource);
    $("source-edit")?.addEventListener("click", openSourceEditor);
    $("source-cancel")?.addEventListener("click", cancelSourceEditor);
    $("source-editor")?.addEventListener("cancel", (event) => {
      event.preventDefault();
      cancelSourceEditor();
    });
    $("source-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      return saveSource();
    });
    $("source-remove")?.addEventListener("click", removeSource);
    for (const id of sourceFields) $(id)?.addEventListener("input", () => {
      dirty = true;
      clearRelease();
      status("Save the source changes before browsing or installing plugins.");
    });
    syncControls();
  }

  window.refreshPluginSources = loadSources;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

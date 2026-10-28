/* Configure plugin sources and install verified, explicitly selected releases. */
(function () {
  const $ = (id) => document.getElementById(`plugin-release-${id}`);
  const exactTag = /^plugins-(?:[a-f0-9]{40}|[a-f0-9]{12}-v\d+\.\d+)$/;
  const sourceFields = ["source-id", "source-label", "source-repository", "source-branch", "source-credential"];
  let sources = [];
  let release = null;
  let busy = false;
  let dirty = false;

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
  }

  function source() {
    return sources.find((row) => row.id === $("source").value);
  }

  function selected() {
    return release?.plugins?.find((row) => row.name === $("name").value);
  }

  function syncControls() {
    const row = selected();
    $("browse").disabled = busy || !source() || dirty;
    $("tag").disabled = busy;
    $("source").disabled = busy;
    $("source-refresh").disabled = busy;
    $("source-new").disabled = busy;
    $("source-save").disabled = busy;
    $("source-remove").disabled = busy || !source() || dirty;
    for (const id of sourceFields) $(id).disabled = busy;
    $("name").disabled = busy || !release?.plugins?.length;
    $("install").disabled = busy || !row || dirty;
    $("digest").textContent = row ? `${row.size} bytes · Expected SHA-256: ${row.sha256}` : "";
  }

  function clearRelease() {
    release = null;
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
    $("source-auth").textContent = row ? (row.credential_configured ? "Credential is configured on this service." : "Credential is not configured on this service; private repositories require one.") : "";
    dirty = false;
    clearRelease();
  }

  async function loadSources(preferred) {
    if (busy) return;
    const previous = typeof preferred === "string" ? preferred : $("source").value;
    busy = true;
    clearRelease();
    try {
      if (!window.rmmApi) throw new Error("Connect to the RMM service first.");
      const response = await window.rmmApi("/plugin-sources");
      if (response.status !== 200) throw new Error(response.data?.error || `HTTP ${response.status}`);
      sources = response.data?.sources || [];
      $("source").replaceChildren();
      for (const row of sources) {
        const option = document.createElement("option");
        option.value = row.id;
        option.textContent = `${row.label || row.id} · ${row.repository} · ${row.branch}`;
        $("source").appendChild(option);
      }
      $("source").value = sources.some((row) => row.id === previous) ? previous : sources[0]?.id || "";
      editSource();
      status(sources.length ? "Choose a source and browse its published plugins." : "Add a repository and branch to get started.");
    } catch (error) {
      sources = [];
      $("source").replaceChildren();
      editSource();
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  function newSource() {
    $("source").value = "";
    editSource();
    $("source-editor").open = true;
    status("Enter a source ID, repository, and branch, then save the source.");
    $("source-id").focus?.();
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
    if (!/^(?:RMM_PLUGIN_GITHUB_TOKEN|RMM_PLUGIN_TOKEN_[A-Z0-9_]+)$/.test(body.credential_env)) {
      status("Enter RMM_PLUGIN_GITHUB_TOKEN or RMM_PLUGIN_TOKEN_NAME, never a secret value.", true);
      return;
    }
    busy = true;
    clearRelease();
    let saved = false;
    try {
      const response = await window.rmmApi(`/plugin-sources/${encodeURIComponent(id)}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (![200, 201].includes(response.status)) throw new Error(response.data?.error || `HTTP ${response.status}`);
      saved = true;
    } catch (error) {
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
    if (saved) await loadSources(id);
  }

  async function removeSource() {
    const row = source();
    if (busy || dirty || !row) return;
    busy = true;
    clearRelease();
    let removed = false;
    try {
      const response = await window.rmmApi(`/plugin-sources/${encodeURIComponent(row.id)}`, { method: "DELETE" });
      if (response.status !== 200) throw new Error(response.data?.error || `HTTP ${response.status}`);
      removed = true;
    } catch (error) {
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
    if (removed) await loadSources();
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
        const option = document.createElement("option");
        option.value = row.name;
        option.textContent = row.name;
        $("name").appendChild(option);
      }
      status(data.plugins.length ? "Select a plugin to install from this exact release." : "This release has no plugins.");
    } catch (error) {
      clearRelease();
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  async function install() {
    const row = selected();
    if (busy || dirty || !row) return;
    const { tag, source: sourceId, source_revision: sourceRevision } = release;
    busy = true;
    syncControls();
    status(`Downloading and verifying ${row.name}…`);
    try {
      const response = await window.rmmApi("/plugin-releases/install", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: row.name, tag, sha256: row.sha256, source: sourceId, source_revision: sourceRevision }),
      });
      if (![200, 201].includes(response.status)) {
        if (response.data?.local_installed) {
          await window.refreshAssets?.();
          throw new Error(`Installed locally, but server synchronization failed: ${response.data?.error || "retry installation to synchronize"}`);
        }
        throw new Error(response.data?.error || `HTTP ${response.status}`);
      }
      status(`Installed ${row.name} from ${tag}. SHA-256 verified${response.data.cached ? "; reused cached bytes" : ""}.`);
      await window.refreshAssets?.();
    } catch (error) {
      status(error.message, true);
    } finally {
      busy = false;
      syncControls();
    }
  }

  function bind() {
    $("browse")?.addEventListener("click", browse);
    $("install")?.addEventListener("click", install);
    $("name")?.addEventListener("change", syncControls);
    $("tag")?.addEventListener("input", clearRelease);
    $("source")?.addEventListener("change", editSource);
    $("source-refresh")?.addEventListener("click", loadSources);
    $("source-new")?.addEventListener("click", newSource);
    $("source-save")?.addEventListener("click", saveSource);
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

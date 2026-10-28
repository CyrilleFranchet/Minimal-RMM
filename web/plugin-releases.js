/* Browse compiled GitHub releases and install one pinned plugin. */
(function () {
  const $ = (id) => document.getElementById(`plugin-release-${id}`);
  let release = null;
  let busy = false;

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
  }

  function selected() {
    return release?.plugins?.find((row) => row.name === $("name").value);
  }

  function syncControls() {
    const row = selected();
    $("browse").disabled = busy;
    $("tag").disabled = busy;
    $("name").disabled = busy || !release?.plugins?.length;
    $("install").disabled = busy || !row;
    $("digest").textContent = row ? `${row.size} bytes · Expected SHA-256: ${row.sha256}` : "";
  }

  function clearRelease() {
    release = null;
    $("name").replaceChildren();
    $("info").textContent = "";
    syncControls();
  }

  async function browse() {
    if (busy) return;
    clearRelease();
    busy = true;
    syncControls();
    status("Loading published plugins…");
    try {
      if (!window.rmmApi) throw new Error("Connect to the RMM service first.");
      const tag = $("tag").value.trim() || "latest";
      const response = await window.rmmApi(`/plugin-releases?tag=${encodeURIComponent(tag)}`);
      if (response.status !== 200) throw new Error(response.data?.error || `HTTP ${response.status}`);
      const data = response.data;
      if (!/^plugins-[a-f0-9]{40}$/.test(data?.tag || "") || !Array.isArray(data.plugins)) {
        throw new Error("The service returned an invalid plugin release.");
      }
      release = data;
      $("info").textContent = `${data.repository} · ${data.tag}`;
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
    if (busy || !row) return;
    const tag = release.tag;
    busy = true;
    syncControls();
    status(`Downloading and verifying ${row.name}…`);
    try {
      const response = await window.rmmApi("/plugin-releases/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: row.name, tag, sha256: row.sha256 }),
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
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

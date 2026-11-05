/* Dynamic operator assets and RMM-Profile controls. */
(function () {
  const $ = (selector) => document.querySelector(selector);

  function setText(selector, message, error = false) {
    const node = $(selector);
    if (!node) return;
    node.textContent = message || "";
    node.classList.toggle("error", Boolean(error));
  }

  function api() {
    return window.rmmApi;
  }

  function connectionContext() {
    return { generation: window.rmmState?.authGeneration || 0, token: window.rmmState?.token };
  }

  function isCurrent(context) {
    const current = connectionContext();
    return current.generation === context.generation && current.token === context.token;
  }

  function assetLabel(row) {
    const state = row.enabled === false ? "disabled" : "enabled";
    return `${row.kind}/${row.name} · ${state} · ${row.size ?? "?"} bytes`;
  }

  async function refreshAssets() {
    if (!api()) return;
    const context = connectionContext();
    const { status, data } = await api()("/assets");
    if (!isCurrent(context)) return;
    const list = $("#asset-list");
    if (!list) return;
    if (status !== 200) {
      list.innerHTML = '<p class="ai-hint">Unable to load managed assets.</p>';
      return;
    }
    const rows = (data?.assets || []).filter((row) => row.kind !== "skills");
    list.replaceChildren();
    if (!rows.length) {
      list.innerHTML = '<p class="ai-hint">No managed uploads.</p>';
      return;
    }
    for (const row of rows) {
      const item = document.createElement("div");
      item.className = "asset-item";
      const label = document.createElement("span");
      label.textContent = assetLabel(row);
      item.appendChild(label);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "secondary";
      toggle.textContent = row.enabled === false ? "Enable" : "Disable";
      toggle.addEventListener("click", async () => {
        if (!isCurrent(context)) return;
        const next = row.enabled === false;
        const result = await api()(
          `/assets/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.name)}`,
          { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: next }) },
        );
        if (!isCurrent(context)) return;
        if (result.status !== 200) setText("#asset-status", result.data?.error || "Asset update failed.", true);
        await refreshAssets();
      });
      item.appendChild(toggle);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        if (!isCurrent(context)) return;
        const result = await api()(`/assets/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.name)}`, { method: "DELETE" });
        if (!isCurrent(context)) return;
        if (result.status !== 200) setText("#asset-status", result.data?.error || "Asset removal failed.", true);
        await refreshAssets();
      });
      item.appendChild(remove);
      list.appendChild(item);
    }
  }

  async function refreshTransport() {
    if (!api()) return;
    const context = connectionContext();
    const { status, data } = await api()("/transport");
    if (!isCurrent(context)) return;
    if (status !== 200) {
      setText("#asset-profile-status", "Unable to read beacon transport status.", true);
      return;
    }
    const beaconUrl = data?.beacon_url || data?.profile?.beacon_public_url || "";
    const output = $("#asset-beacon-url");
    if (output) output.textContent = beaconUrl || "not connected";
    window.rmmBeaconUrl = beaconUrl || "";
    ["#agent-server-url", "#agent-go-server-url"].forEach((selector) => {
      const input = $(selector);
      if (input && !input.value.trim() && beaconUrl) input.value = beaconUrl;
    });
    const active = data?.profile?.active;
    const profileInput = $("#asset-profile-name");
    if (active?.name && profileInput) profileInput.value = active.name;
    const options = $("#asset-profile-options");
    if (options) {
      options.replaceChildren();
      for (const profile of data?.profile?.installed || []) {
        const option = document.createElement("option");
        option.value = profile.name;
        option.label = profile.description || profile.version || profile.name;
        options.appendChild(option);
      }
    }
    setText("#asset-profile-status", active?.running ? `Profile ${active.name} is active.` : "No active RMM-Profile.");
  }

  async function installAsset() {
    const context = connectionContext();
    const kind = $("#asset-kind")?.value || "plugins";
    const url = $("#asset-url")?.value.trim() || "";
    const name = $("#asset-name")?.value.trim() || "";
    if (!url) {
      setText("#asset-status", "Enter an HTTP(S) asset URL.", true);
      return;
    }
    const button = $("#asset-add");
    if (button) button.disabled = true;
    setText("#asset-status", `Downloading ${kind} asset…`);
    try {
      const result = await api()(`/assets/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, name: name || undefined, enabled: true }),
      });
      if (!isCurrent(context)) return;
      if (![200, 201].includes(result.status)) throw new Error(result.data?.error || `HTTP ${result.status}`);
      setText("#asset-status", `Installed ${result.data?.asset?.name || kind}.`);
      $("#asset-url").value = "";
      $("#asset-name").value = "";
      await refreshAssets();
      if (kind === "rclone") window.refreshRmmExfilStatus?.();
    } catch (error) {
      if (isCurrent(context)) setText("#asset-status", error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function syncAssetForm() {
    const kind = $("#asset-kind")?.value || "plugins";
    const url = $("#asset-url");
    const install = $("#asset-add");
    const file = $("#asset-file");
    const upload = $("#asset-upload");
    const rclone = kind === "rclone";
    url?.classList.toggle("hidden", rclone);
    install?.classList.toggle("hidden", rclone);
    if (url) {
      url.placeholder = rclone ? "Upload a profiles.json file" : "https://example.invalid/release.dll";
      url.value = rclone ? "" : url.value;
    }
    if (file) {
      file.accept = rclone ? ".json,application/json" : kind === "skills" ? ".md,text/markdown" : ".dll,.ps1";
    }
    if (upload) upload.textContent = rclone ? "Upload profiles.json" : "Upload file";
  }

  function fileToBase64(file) {
    return file.arrayBuffer().then((buffer) => {
      const bytes = new Uint8Array(buffer);
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
      }
      return btoa(binary);
    });
  }

  async function uploadAsset() {
    const context = connectionContext();
    const kind = $("#asset-kind")?.value || "plugins";
    const file = $("#asset-file")?.files?.[0];
    const name = $("#asset-name")?.value.trim() || file?.name || "";
    if (!file) {
      setText("#asset-status", "Choose a file first.", true);
      return;
    }
    if (!name) {
      setText("#asset-status", "The uploaded file needs a name.", true);
      return;
    }
    const button = $("#asset-upload");
    if (button) button.disabled = true;
    setText("#asset-status", `Uploading ${name}…`);
    try {
      const content = await fileToBase64(file);
      if (!isCurrent(context)) return;
      const result = await api()(`/assets/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, content_b64: content, enabled: true }),
      });
      if (!isCurrent(context)) return;
      if (![200, 201].includes(result.status)) throw new Error(result.data?.error || `HTTP ${result.status}`);
      setText("#asset-status", `Uploaded ${result.data?.asset?.name || name}.`);
      $("#asset-file").value = "";
      $("#asset-name").value = "";
      await refreshAssets();
      if (kind === "rclone") window.refreshRmmExfilStatus?.();
    } catch (error) {
      if (isCurrent(context)) setText("#asset-status", error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function startProfile() {
    const context = connectionContext();
    const name = $("#asset-profile-name")?.value.trim() || "cloudflared";
    const result = await api()("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!isCurrent(context)) return;
    await refreshTransport();
    if (isCurrent(context) && result.status !== 200) setText("#asset-profile-status", result.data?.detail || "Profile start failed.", true);
  }

  async function stopProfile() {
    const context = connectionContext();
    const result = await api()("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stop: true }),
    });
    if (!isCurrent(context)) return;
    await refreshTransport();
    if (isCurrent(context) && result.status !== 200) setText("#asset-profile-status", result.data?.detail || "Profile stop failed.", true);
  }

  function bind() {
    $("#asset-add")?.addEventListener("click", installAsset);
    $("#asset-upload")?.addEventListener("click", () => $("#asset-file")?.click());
    $("#asset-file")?.addEventListener("change", uploadAsset);
    $("#asset-kind")?.addEventListener("change", syncAssetForm);
    $("#asset-profile-refresh")?.addEventListener("click", () => Promise.all([refreshAssets(), refreshTransport()]));
    $("#asset-profile-start")?.addEventListener("click", startProfile);
    $("#asset-profile-stop")?.addEventListener("click", stopProfile);
    syncAssetForm();
  }

  window.refreshAssets = refreshAssets;
  window.refreshRmmTransport = refreshTransport;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

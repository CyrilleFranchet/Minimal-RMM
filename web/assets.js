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

  function assetLabel(row) {
    const state = row.enabled === false ? "disabled" : "enabled";
    return `${row.kind}/${row.name} · ${state} · ${row.size ?? "?"} bytes`;
  }

  async function refreshAssets() {
    if (!api()) return;
    const { status, data } = await api()("/assets");
    const list = $("#asset-list");
    if (!list) return;
    if (status !== 200) {
      list.innerHTML = '<p class="ai-hint">Unable to load managed assets.</p>';
      return;
    }
    const rows = data?.assets || [];
    list.replaceChildren();
    if (!rows.length) {
      list.innerHTML = '<p class="ai-hint">No URL-installed assets.</p>';
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
        const next = row.enabled === false;
        const result = await api()(
          `/assets/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.name)}`,
          { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: next }) },
        );
        if (result.status !== 200) setText("#asset-status", result.data?.error || "Asset update failed.", true);
        await refreshAssets();
        if (row.kind === "skills" && typeof window.fetchAiSkills === "function") window.fetchAiSkills();
      });
      item.appendChild(toggle);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        const result = await api()(`/assets/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.name)}`, { method: "DELETE" });
        if (result.status !== 200) setText("#asset-status", result.data?.error || "Asset removal failed.", true);
        await refreshAssets();
      });
      item.appendChild(remove);
      list.appendChild(item);
    }
  }

  async function refreshTransport() {
    if (!api()) return;
    const { status, data } = await api()("/transport");
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
      if (![200, 201].includes(result.status)) throw new Error(result.data?.error || `HTTP ${result.status}`);
      setText("#asset-status", `Installed ${result.data?.asset?.name || kind}.`);
      $("#asset-url").value = "";
      $("#asset-name").value = "";
      await refreshAssets();
      if (kind === "skills" && typeof window.fetchAiSkills === "function") window.fetchAiSkills();
    } catch (error) {
      setText("#asset-status", error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function startProfile() {
    const name = $("#asset-profile-name")?.value.trim() || "cloudflared";
    const result = await api()("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (result.status !== 200) setText("#asset-profile-status", result.data?.detail || "Profile start failed.", true);
    await refreshTransport();
  }

  async function stopProfile() {
    const result = await api()("/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stop: true }),
    });
    if (result.status !== 200) setText("#asset-profile-status", result.data?.detail || "Profile stop failed.", true);
    await refreshTransport();
  }

  function bind() {
    $("#asset-add")?.addEventListener("click", installAsset);
    $("#asset-profile-refresh")?.addEventListener("click", () => Promise.all([refreshAssets(), refreshTransport()]));
    $("#asset-profile-start")?.addEventListener("click", startProfile);
    $("#asset-profile-stop")?.addEventListener("click", stopProfile);
  }

  window.refreshAssets = refreshAssets;
  window.refreshRmmTransport = refreshTransport;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

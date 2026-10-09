/** Authenticated Go PE plugin inventory and server-side build UI. */
(function () {
  const TOKEN_KEY = "rmm_api_token";
  const $ = (selector) => document.querySelector(selector);

  function token() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function headers(json) {
    const value = { Authorization: `Bearer ${token()}` };
    if (json) value["Content-Type"] = "application/json";
    return value;
  }

  function status(message, error = false) {
    const node = $("#agent-plugins-status");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("error", error);
  }

  function formatBytes(size) {
    if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
    return `${size} bytes`;
  }

  function renderInventory(plugins) {
    const list = $("#agent-plugins-list");
    if (!list) return;
    if (!plugins.length) {
      list.replaceChildren();
      return;
    }
    list.replaceChildren(
      ...plugins.map((plugin) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = plugin.name;
        const meta = document.createElement("span");
        meta.className = "agent-plugins-meta";
        meta.textContent = `${formatBytes(plugin.size)}  sha256 ${String(plugin.sha256).slice(0, 16)}…`;
        item.append(name, meta);
        return item;
      }),
    );
  }

  async function refresh() {
    if (!token()) {
      status("Connect with an API token before listing plugins.", true);
      return;
    }
    try {
      const response = await fetch("/api/v1/plugins", { headers: headers(false) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      const targets = data.targets || [];
      const toolchain = data.toolchain || {};
      renderInventory(data.plugins || []);

      const select = $("#agent-plugins-target");
      if (select) {
        select.replaceChildren(...targets.map((name) => new Option(name, name)));
      }
      const output = $("#agent-plugins-output");
      if (output && !output.value) output.placeholder = targets.length ? `${targets[0]}.dll` : "plugin.dll";

      const build = $("#agent-plugins-build");
      const missing = [];
      if (!toolchain.go) missing.push("Go toolchain");
      if (!toolchain.cross_compiler) missing.push(`cross compiler ${toolchain.cross_compiler_name || "mingw-w64"}`);
      if (build) build.disabled = !targets.length || missing.length > 0;
      if (!targets.length) {
        status("No checked-in Go plugins found on the server (agent-plugins/).", true);
      } else if (missing.length) {
        status(`Plugin builds unavailable: ${missing.join(" and ")} missing on the server.`, true);
      } else {
        status(`${targets.length} plugin target(s) ready to compile.`);
      }
    } catch (error) {
      status(error.message, true);
    }
  }

  async function build() {
    if (!token()) {
      status("Connect with an API token before compiling.", true);
      return;
    }
    const name = $("#agent-plugins-target")?.value || "";
    if (!name) {
      status("Choose a plugin target first.", true);
      return;
    }
    const output = $("#agent-plugins-output")?.value.trim() || "";
    const button = $("#agent-plugins-build");
    button.disabled = true;
    status(`Compiling ${name} for windows/amd64 on the RMM server… (first builds can take minutes)`);
    try {
      const response = await fetch("/api/v1/plugins/build", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify(output ? { name, output } : { name }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      status(`Built ${data.output} (${formatBytes(data.size)}, sha256 ${data.sha256}). Load it with: pe ${data.output.replace(/\.dll$/i, "")}`);
      await refresh();
    } catch (error) {
      status(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function bind() {
    if (!$("#agent-plugins-panel")) return;
    $("#agent-plugins-refresh")?.addEventListener("click", refresh);
    $("#agent-plugins-build")?.addEventListener("click", build);
    $("#agent-plugins-panel")?.addEventListener("toggle", (event) => {
      if (event.target.open) refresh();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

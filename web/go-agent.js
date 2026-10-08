/** Authenticated Go agent source viewer and fixed-target server build UI. */
(function () {
  const TOKEN_KEY = "rmm_api_token";
  const $ = (selector) => document.querySelector(selector);
  let files = [];

  function token() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function headers(json) {
    const value = { Authorization: `Bearer ${token()}` };
    if (json) value["Content-Type"] = "application/json";
    return value;
  }

  function status(message, error = false) {
    const node = $("#agent-go-status");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("error", error);
  }

  function renderSource() {
    const select = $("#agent-go-file");
    const output = $("#agent-go-source");
    if (!select || !output) return;
    const current = files[Number(select.value)] || files[0];
    output.value = current ? `// ${current.filename}\n\n${current.content}` : "";
  }

  async function loadSource() {
    if (!token()) {
      status("Connect with an API token before loading the Go source.", true);
      return;
    }
    status("Loading source…");
    try {
      const response = await fetch("/api/v1/agent/go", { headers: headers(false) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      files = data.files || [];
      const select = $("#agent-go-file");
      select.replaceChildren(...files.map((file, index) => new Option(file.filename, String(index))));
      renderSource();
      status(`${files.length} source files loaded. Choose a target and compile on the server.`);
    } catch (error) {
      status(error.message, true);
    }
  }

  async function build() {
    if (!token()) {
      status("Connect with an API token before compiling.", true);
      return;
    }
    const goos = $("#agent-goos")?.value || "windows";
    const goarch = $("#agent-goarch")?.value || "amd64";
    const serverUrl =
      $("#agent-go-server-url")?.value.trim() || $("#agent-server-url")?.value.trim() || "";
    const beaconSecret =
      $("#agent-go-beacon-secret")?.value || $("#agent-beacon-secret")?.value || "";
    if (!serverUrl || !beaconSecret) {
      status("Set the Go server URL and beacon secret before compiling.", true);
      return;
    }
    const button = $("#agent-go-build");
    button.disabled = true;
    $("#agent-go-download")?.classList.add("hidden");
    status(`Compiling ${goos}/${goarch} on the RMM server…`);
    try {
      const response = await fetch("/api/v1/agent/go/build", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({
          goos,
          goarch,
          config: {
            base_url: serverUrl.replace(/\/$/, ""),
            beacon_secret: beaconSecret,
            session_id: $("#agent-go-session-id")?.value.trim() || "",
            sleep_seconds: Math.min(3600, Math.max(1, Number($("#agent-go-sleep")?.value) || 60)),
            jitter_percent: Math.min(100, Math.max(0, Number($("#agent-go-jitter")?.value) || 30)),
            http_proxy: $("#agent-go-proxy")?.value.trim() || "",
          },
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
      const link = $("#agent-go-download");
      link.href = data.download_url;
      link.textContent = `Download ZIP ${data.filename} (${data.size} bytes, SHA-256 ${data.sha256})`;
      link.dataset.downloadUrl = data.download_url;
      link.classList.remove("hidden");
      status("Build complete. Download the ZIP and set the required environment variables before starting the agent.");
    } catch (error) {
      status(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function bind() {
    if (!$("#agent-go-panel")) return;
    $("#agent-go-file")?.addEventListener("change", renderSource);
    $("#agent-go-refresh")?.addEventListener("click", loadSource);
    $("#agent-go-build")?.addEventListener("click", build);
    $("#agent-go-use-origin")?.addEventListener("click", () => {
      const input = $("#agent-go-server-url");
      if (input) input.value = window.location.origin.replace(/\/$/, "");
    });
    $("#agent-go-download")?.addEventListener("click", async (event) => {
      event.preventDefault();
      const url = event.currentTarget.dataset.downloadUrl;
      if (!url) return;
      try {
        status("Downloading ZIP…");
        const response = await fetch(url, { headers: headers(false) });
        if (!response.ok) throw new Error(`Download failed (HTTP ${response.status})`);
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = url.split("/").pop() || "minimal-rmm-agent.zip";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(objectUrl);
        status("ZIP downloaded.");
      } catch (error) {
        status(error.message, true);
      }
    });
    $("#agent-go-panel")?.addEventListener("toggle", (event) => {
      if (event.target.open && !files.length) loadSource();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();

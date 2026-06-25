/**
 * Web UI — PowerShell agent deploy generator (full client_rmm.ps1 or config snippet).
 */
(function () {
  const PREFS_KEY = "rmm_agent_gen_prefs";
  const TOKEN_KEY = "rmm_api_token";
  const $ = (sel) => document.querySelector(sel);

  let scriptTemplate = null;
  let scriptTemplatePromise = null;

  function psQuote(value) {
    return `'${String(value ?? "").replace(/'/g, "''")}'`;
  }

  function psBool(value) {
    return value ? "$true" : "$false";
  }

  function defaultServerUrl() {
    try {
      return window.location.origin.replace(/\/$/, "");
    } catch {
      return "";
    }
  }

  function apiToken() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function loadPrefs() {
    try {
      return JSON.parse(sessionStorage.getItem(PREFS_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function savePrefs(prefs) {
    try {
      sessionStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* quota */
    }
  }

  function readForm() {
    const sessionMode = document.querySelector('input[name="agent-session-mode"]:checked')?.value || "new";
    const outputMode = document.querySelector('input[name="agent-output-mode"]:checked')?.value || "script";
    return {
      serverUrl: ($("#agent-server-url")?.value || "").trim().replace(/\/$/, ""),
      beaconSecret: ($("#agent-beacon-secret")?.value || "").trim(),
      sessionMode,
      sessionId: ($("#agent-session-id")?.value || "").trim(),
      sleepSeconds: Math.min(3600, Math.max(1, parseInt($("#agent-sleep")?.value, 10) || 60)),
      jitterPercent: Math.min(100, Math.max(0, parseInt($("#agent-jitter")?.value, 10) || 30)),
      maxRetries: Math.max(1, parseInt($("#agent-max-retries")?.value, 10) || 3),
      persistentHttp: Boolean($("#agent-persistent-http")?.checked),
      httpProxy: ($("#agent-http-proxy")?.value || "").trim(),
      proxyDefaultCreds: Boolean($("#agent-proxy-creds")?.checked),
      verboseHttp: Boolean($("#agent-verbose-http")?.checked),
      outputMode,
    };
  }

  function sessionIdLine(opts) {
    if (opts.sessionMode === "fixed" && opts.sessionId) {
      return `$sessionId = ${psQuote(opts.sessionId)}`;
    }
    return "$sessionId = [System.Guid]::NewGuid().ToString()";
  }

  function buildConfigLines(opts) {
    return [
      `$u = ${psQuote(opts.serverUrl || "https://your-server.example.com")}`,
      `$beaconSecret = ${psQuote(opts.beaconSecret)}`,
      sessionIdLine(opts),
      "",
      `$baseSleepSeconds = ${opts.sleepSeconds}`,
      `$jitterPercent = ${opts.jitterPercent}`,
      `$maxRetries = ${opts.maxRetries}`,
      "",
      `$persistentHttp = ${psBool(opts.persistentHttp)}`,
      `$httpProxy = ${psQuote(opts.httpProxy)}`,
      `$httpProxyUseDefaultCredentials = ${psBool(opts.proxyDefaultCreds)}`,
      "",
      `$verboseHttp = ${psBool(opts.verboseHttp)}`,
    ];
  }

  function buildConfigBlock(opts) {
    return [
      "# Minimal-RMM agent — paste into client_rmm.ps1 configuration block (lab use only)",
      "",
      ...buildConfigLines(opts),
    ].join("\r\n");
  }

  function buildEnvBlock(opts) {
    const lines = [
      "# Minimal-RMM agent — environment overrides (lab use only)",
      "",
      `$env:RMM_BASE_URL = ${psQuote(opts.serverUrl || "https://your-server.example.com")}`,
    ];
    if (opts.beaconSecret) {
      lines.push(`$env:RMM_BEACON_SECRET = ${psQuote(opts.beaconSecret)}`);
    }
    if (opts.persistentHttp) {
      lines.push("$env:RMM_PERSISTENT_HTTP = '1'");
    }
    if (opts.httpProxy) {
      lines.push(`$env:RMM_HTTP_PROXY = ${psQuote(opts.httpProxy)}`);
    }
    if (opts.proxyDefaultCreds) {
      lines.push("$env:RMM_HTTP_PROXY_USE_DEFAULT_CREDENTIALS = '1'");
    }
    if (opts.verboseHttp) {
      lines.push("$env:RMM_VERBOSE = '1'");
    }
    lines.push("");
    lines.push("# Use with an unmodified client_rmm.ps1 from the repo or GET /api/v1/agent/script.");
    if (opts.sessionMode === "fixed" && opts.sessionId) {
      lines.push(`# Fixed session ID: set $sessionId in client_rmm.ps1 to ${psQuote(opts.sessionId)}`);
    }
    return lines.join("\r\n");
  }

  function patchScriptTemplate(template, opts) {
    const lines = template.replace(/\r\n/g, "\n").split("\n");
    let start = -1;
    let end = -1;
    for (let i = 0; i < lines.length; i += 1) {
      if (lines[i].startsWith("$u = ")) start = i;
      if (start >= 0 && lines[i].startsWith("$verboseHttp = ")) {
        end = i;
        break;
      }
    }
    if (start < 0 || end < 0) return null;
    const patched = [
      ...lines.slice(0, start),
      ...buildConfigLines(opts),
      ...lines.slice(end + 1),
    ];
    return patched.join("\r\n");
  }

  async function fetchScriptTemplate() {
    const token = apiToken();
    if (!token) {
      throw new Error("Connect with your API token to load client_rmm.ps1.");
    }
    const res = await fetch("/api/v1/agent/script", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || `Failed to load script (${res.status})`);
    }
    if (!data.content) {
      throw new Error("Script response missing content.");
    }
    return data.content;
  }

  async function ensureScriptTemplate() {
    if (scriptTemplate) return scriptTemplate;
    if (!scriptTemplatePromise) {
      scriptTemplatePromise = fetchScriptTemplate()
        .then((content) => {
          scriptTemplate = content;
          return content;
        })
        .catch((err) => {
          scriptTemplatePromise = null;
          throw err;
        });
    }
    return scriptTemplatePromise;
  }

  function buildRunCommand() {
    return "powershell -ExecutionPolicy Bypass -File .\\client_rmm.ps1";
  }

  function updateSessionIdField() {
    const mode = document.querySelector('input[name="agent-session-mode"]:checked')?.value || "new";
    const input = $("#agent-session-id");
    const regen = $("#agent-session-regen");
    if (!input) return;
    const fixed = mode === "fixed";
    input.disabled = !fixed;
    if (regen) regen.disabled = !fixed;
    if (fixed && !input.value.trim() && typeof crypto !== "undefined" && crypto.randomUUID) {
      input.value = crypto.randomUUID();
    }
  }

  function setOutputHint(opts, errMsg) {
    const hint = $("#agent-gen-hint");
    if (!hint) return;
    if (errMsg) {
      hint.textContent = errMsg;
      return;
    }
    if (opts.outputMode === "env") {
      hint.textContent =
        "Copy the env block, save client_rmm.ps1 on the target, set variables, then run the command below.";
    } else if (opts.outputMode === "config") {
      hint.textContent = "Replace lines 45–57 in client_rmm.ps1, then run the command below on the target host.";
    } else {
      hint.textContent =
        "Save as client_rmm.ps1 on the Windows lab host, then run the command below. Script is loaded from this server.";
    }
  }

  function updateOutputLabels(opts) {
    const label = $("#agent-gen-output-label");
    const copyBtn = $("#agent-gen-copy-script");
    const downloadBtn = $("#agent-gen-download-script");
    const isScript = opts.outputMode === "script";
    if (label) {
      label.textContent = isScript ? "Generated script" : "Generated output";
    }
    if (copyBtn) {
      copyBtn.textContent = isScript ? "Copy script" : "Copy output";
    }
    if (downloadBtn) {
      downloadBtn.classList.toggle("hidden", !isScript);
    }
  }

  async function renderOutput() {
    const opts = readForm();
    savePrefs(opts);
    const out = $("#agent-gen-output");
    const run = $("#agent-gen-run");
    if (!out) return;

    updateOutputLabels(opts);

    if (!opts.serverUrl) {
      out.value = "# Set server URL to generate output.";
      setOutputHint(opts, "Server URL is required (no trailing slash).");
      return;
    }

    if (opts.outputMode === "env") {
      out.value = buildEnvBlock(opts);
      if (run) run.value = buildRunCommand();
      setOutputHint(opts);
      return;
    }

    if (opts.outputMode === "config") {
      out.value = buildConfigBlock(opts);
      if (run) run.value = buildRunCommand();
      setOutputHint(opts);
      return;
    }

    out.value = "Loading client_rmm.ps1…";
    setOutputHint(opts, "Fetching agent script from server…");
    try {
      const template = await ensureScriptTemplate();
      const patched = patchScriptTemplate(template, opts);
      if (!patched) {
        out.value = "# Failed to patch client_rmm.ps1 template (config block not found).";
        setOutputHint(opts, "Script template format changed — report to operator.");
        return;
      }
      out.value = patched;
      if (run) run.value = buildRunCommand();
      setOutputHint(opts);
    } catch (err) {
      out.value = `# ${err.message || String(err)}`;
      setOutputHint(opts, err.message || String(err));
    }
  }

  async function copyText(text, button) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      if (button) {
        const prev = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = prev;
        }, 1500);
      }
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
  }

  function downloadScript(content) {
    if (!content || content.startsWith("# ") || content.startsWith("Loading")) return;
    const blob = new Blob([content], { type: "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "client_rmm.ps1";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function applyPrefs(prefs) {
    if ($("#agent-server-url") && prefs.serverUrl) {
      $("#agent-server-url").value = prefs.serverUrl;
    } else if ($("#agent-server-url") && !$("#agent-server-url").value) {
      $("#agent-server-url").value = defaultServerUrl();
    }
    if ($("#agent-beacon-secret") && prefs.beaconSecret) {
      $("#agent-beacon-secret").value = prefs.beaconSecret;
    }
    if (prefs.sessionMode === "fixed") {
      const fixed = document.querySelector('input[name="agent-session-mode"][value="fixed"]');
      if (fixed) fixed.checked = true;
    }
    if ($("#agent-session-id") && prefs.sessionId) {
      $("#agent-session-id").value = prefs.sessionId;
    }
    if ($("#agent-sleep") && prefs.sleepSeconds) $("#agent-sleep").value = prefs.sleepSeconds;
    if ($("#agent-jitter") && prefs.jitterPercent != null) {
      $("#agent-jitter").value = prefs.jitterPercent;
    }
    if ($("#agent-max-retries") && prefs.maxRetries) {
      $("#agent-max-retries").value = prefs.maxRetries;
    }
    if ($("#agent-persistent-http")) {
      $("#agent-persistent-http").checked = Boolean(prefs.persistentHttp);
    }
    if ($("#agent-http-proxy") && prefs.httpProxy) {
      $("#agent-http-proxy").value = prefs.httpProxy;
    }
    if ($("#agent-proxy-creds")) {
      $("#agent-proxy-creds").checked = Boolean(prefs.proxyDefaultCreds);
    }
    if ($("#agent-verbose-http")) {
      $("#agent-verbose-http").checked = Boolean(prefs.verboseHttp);
    }
    if (prefs.outputMode === "config") {
      const cfg = document.querySelector('input[name="agent-output-mode"][value="config"]');
      if (cfg) cfg.checked = true;
    } else if (prefs.outputMode === "env") {
      const env = document.querySelector('input[name="agent-output-mode"][value="env"]');
      if (env) env.checked = true;
    }
    updateSessionIdField();
    renderOutput();
  }

  function bindAgentGenerator() {
    const panel = $("#agent-gen-panel");
    if (!panel) return;

    applyPrefs(loadPrefs());

    panel.querySelectorAll("input, select, textarea").forEach((el) => {
      el.addEventListener("input", () => {
        renderOutput();
      });
      el.addEventListener("change", () => {
        renderOutput();
      });
    });

    panel.addEventListener("toggle", () => {
      if (panel.open && apiToken()) {
        ensureScriptTemplate()
          .then(() => renderOutput())
          .catch(() => renderOutput());
      }
    });

    document.querySelectorAll('input[name="agent-session-mode"]').forEach((el) => {
      el.addEventListener("change", () => {
        updateSessionIdField();
        renderOutput();
      });
    });

    $("#agent-session-regen")?.addEventListener("click", () => {
      const input = $("#agent-session-id");
      if (input && typeof crypto !== "undefined" && crypto.randomUUID) {
        input.value = crypto.randomUUID();
        renderOutput();
      }
    });

    $("#agent-gen-copy-script")?.addEventListener("click", () => {
      copyText($("#agent-gen-output")?.value, $("#agent-gen-copy-script"));
    });

    $("#agent-gen-download-script")?.addEventListener("click", () => {
      downloadScript($("#agent-gen-output")?.value);
    });

    $("#agent-gen-copy-run")?.addEventListener("click", () => {
      copyText($("#agent-gen-run")?.value, $("#agent-gen-copy-run"));
    });

    $("#agent-use-origin")?.addEventListener("click", () => {
      const url = defaultServerUrl();
      if (url && $("#agent-server-url")) {
        $("#agent-server-url").value = url;
        renderOutput();
      }
    });
  }

  window.initAgentGenerator = bindAgentGenerator;
  window.refreshAgentScriptTemplate = function refreshAgentScriptTemplate() {
    scriptTemplate = null;
    scriptTemplatePromise = null;
    if ($("#agent-gen-panel")?.open) {
      renderOutput();
    }
  };
})();

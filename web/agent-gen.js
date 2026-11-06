/**
 * Web UI — PowerShell agent deploy generator (full client_rmm.ps1 or config snippet).
 */
(function () {
  const PREFS_KEY = "rmm_agent_gen_prefs";
  const TOKEN_KEY = "rmm_api_token";
  const $ = (sel) => document.querySelector(sel);

  let scriptTemplate = null;
  let scriptTemplatePromise = null;
  let renderGeneration = 0;
  let outputContext = null;

  function connectionContext() {
    return { token: apiToken(), auth: window.rmmState?.authGeneration || 0,
      connection: window.rmmState?.connectionGeneration || 0 };
  }

  function sameContext(left, right) {
    return Boolean(left && right && left.token === right.token && left.auth === right.auth && left.connection === right.connection);
  }

  function isCurrent(context) {
    return sameContext(context, connectionContext());
  }

  function psQuote(value) {
    return `'${String(value ?? "").replace(/'/g, "''")}'`;
  }

  function psBool(value) {
    return value ? "$true" : "$false";
  }

  function defaultServerUrl() {
    if (window.rmmBeaconUrl) return window.rmmBeaconUrl.replace(/\/$/, "");
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
      window.persistRmmUiConfig?.();
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
      workspaceId: ($("#agent-workspace-id")?.value || "").trim(),
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
      `$workspaceId = ${psQuote(opts.workspaceId)}`,
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
      `$env:RMM_AGENT_WORKSPACE_ID = ${psQuote(opts.workspaceId)}`,
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

  async function authenticatedData(path, context) {
    if (!context.token && !window.rmmApi) {
      throw new Error("Connect with your API token to load client_rmm.ps1.");
    }
    if (window.rmmApi) {
      const response = await window.rmmApi(path);
      if (!isCurrent(context)) throw new Error("Connection changed; generate output again.");
      if (response.status !== 200) throw new Error(response.data?.detail || response.data?.error || `Request failed (${response.status})`);
      return response.data;
    }
    const res = await fetch(`/api/v1${path}`, {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${context.token}`,
      },
    });
    const data = await res.json().catch(() => ({}));
    if (!isCurrent(context)) throw new Error("Connection changed; generate output again.");
    if (!res.ok) {
      throw new Error(data.detail || data.error || `Failed to load script (${res.status})`);
    }
    return data;
  }

  async function resolveWorkspace(opts, context) {
    const health = await authenticatedData("/health", context);
    const owner = String(health.workspace_id || "").trim();
    const selected = owner || opts.workspaceId;
    if (selected && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(selected)) {
      throw new Error("Workspace ID must contain only letters, numbers, dots, underscores, or hyphens (up to 64 characters).");
    }
    return selected;
  }

  async function fetchScriptTemplate(context) {
    const data = await authenticatedData("/agent/script", context);
    if (!data.content) {
      throw new Error("Script response missing content.");
    }
    return data.content;
  }

  async function ensureScriptTemplate(context) {
    if (scriptTemplate && sameContext(scriptTemplate.context, context)) return scriptTemplate.content;
    if (!scriptTemplatePromise || !sameContext(scriptTemplatePromise.context, context)) {
      const pending = { context, promise: null };
      pending.promise = fetchScriptTemplate(context)
        .then((content) => {
          if (!isCurrent(context) || scriptTemplatePromise !== pending) throw new Error("Connection changed; generate output again.");
          scriptTemplate = { context, content };
          return content;
        })
        .catch((err) => {
          if (scriptTemplatePromise === pending) scriptTemplatePromise = null;
          throw err;
        });
      scriptTemplatePromise = pending;
    }
    return scriptTemplatePromise.promise;
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
      hint.textContent = "Replace the configuration block in client_rmm.ps1, then run the command below on the Windows target host.";
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
    const context = connectionContext();
    const generation = ++renderGeneration;
    outputContext = null;
    const current = () => generation === renderGeneration && isCurrent(context);
    const out = $("#agent-gen-output");
    const run = $("#agent-gen-run");
    if (!out) return;
    out.value = "Loading workspace configuration…";
    if (run) run.value = "";

    updateOutputLabels(opts);

    if (!opts.serverUrl) {
      out.value = "# Set server URL to generate output.";
      setOutputHint(opts, "Server URL is required (no trailing slash).");
      return;
    }

    try {
      opts.workspaceId = await resolveWorkspace(opts, context);
      if (!current()) return;
      if ($("#agent-workspace-id")) $("#agent-workspace-id").value = opts.workspaceId;
      savePrefs(opts);
      if (opts.outputMode === "env" || opts.outputMode === "config") {
        out.value = opts.outputMode === "env" ? buildEnvBlock(opts) : buildConfigBlock(opts);
        outputContext = context;
        if (run) run.value = buildRunCommand();
        setOutputHint(opts);
        return;
      }
      out.value = "Loading client_rmm.ps1…";
      setOutputHint(opts, "Fetching agent script from server…");
      const template = await ensureScriptTemplate(context);
      if (!current()) return;
      const patched = patchScriptTemplate(template, opts);
      if (!patched) {
        out.value = "# Failed to patch client_rmm.ps1 template (config block not found).";
        setOutputHint(opts, "Script template format changed — report to operator.");
        return;
      }
      out.value = patched;
      outputContext = context;
      if (run) run.value = buildRunCommand();
      setOutputHint(opts);
    } catch (err) {
      if (!current()) return;
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
    if (!content || !isCurrent(outputContext)) return;
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
    if ($("#agent-workspace-id") && prefs.workspaceId) $("#agent-workspace-id").value = prefs.workspaceId;
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
        renderOutput();
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
      if (!isCurrent(outputContext)) return renderOutput();
      copyText($("#agent-gen-output")?.value, $("#agent-gen-copy-script"));
    });

    $("#agent-gen-download-script")?.addEventListener("click", () => {
      if (!isCurrent(outputContext)) return renderOutput();
      downloadScript($("#agent-gen-output")?.value);
    });

    $("#agent-gen-copy-run")?.addEventListener("click", () => {
      if (!isCurrent(outputContext)) return renderOutput();
      copyText($("#agent-gen-run")?.value, $("#agent-gen-copy-run"));
    });

    $("#agent-use-origin")?.addEventListener("click", () => {
      const url = window.rmmBeaconUrl || defaultServerUrl();
      if (url && $("#agent-server-url")) {
        $("#agent-server-url").value = url;
        renderOutput();
      }
    });
    window.refreshRmmTransport?.().then(() => {
      const input = $("#agent-server-url");
      if (input && !input.value.trim() && window.rmmBeaconUrl) {
        input.value = window.rmmBeaconUrl;
        renderOutput();
      }
    }).catch(() => {});
  }

  window.initAgentGenerator = bindAgentGenerator;
  window.refreshAgentScriptTemplate = function refreshAgentScriptTemplate() {
    scriptTemplate = null;
    scriptTemplatePromise = null;
    renderGeneration += 1;
    outputContext = null;
    if ($("#agent-gen-output")) $("#agent-gen-output").value = "";
    if ($("#agent-gen-run")) $("#agent-gen-run").value = "";
    if ($("#agent-gen-panel")?.open) {
      renderOutput();
    }
  };
})();

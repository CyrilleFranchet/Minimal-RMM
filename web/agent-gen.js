/**
 * Web UI — PowerShell agent deploy snippet generator (client_rmm.ps1 config).
 */
(function () {
  const PREFS_KEY = "rmm_agent_gen_prefs";
  const $ = (sel) => document.querySelector(sel);

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
    const outputMode = document.querySelector('input[name="agent-output-mode"]:checked')?.value || "config";
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

  function buildConfigBlock(opts) {
    const lines = [
      "# Minimal-RMM agent — paste into client_rmm.ps1 configuration block (lab use only)",
      "",
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
    return lines.join("\r\n");
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
    lines.push("# Timing env vars are not supported — edit client_rmm.ps1 or use server __CONFIG__.");
    if (opts.sessionMode === "fixed" && opts.sessionId) {
      lines.push(`# Fixed session ID: set $sessionId in client_rmm.ps1 to ${psQuote(opts.sessionId)}`);
    }
    return lines.join("\r\n");
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

  function renderOutput() {
    const opts = readForm();
    savePrefs(opts);
    const out = $("#agent-gen-output");
    const run = $("#agent-gen-run");
    const hint = $("#agent-gen-hint");
    if (!out) return;

    if (!opts.serverUrl) {
      out.value = "# Set server URL to generate configuration.";
      if (hint) {
        hint.textContent = "Server URL is required (no trailing slash).";
      }
      return;
    }

    const body = opts.outputMode === "env" ? buildEnvBlock(opts) : buildConfigBlock(opts);
    out.value = body;
    if (run) run.value = buildRunCommand();
    if (hint) {
      hint.textContent =
        opts.outputMode === "env"
          ? "Copy env block + run command. Place client_rmm.ps1 on the target host first."
          : "Replace lines 45–57 in client_rmm.ps1, then run the command below on the target host.";
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
      /* fallback */
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
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
    if (prefs.outputMode === "env") {
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
      el.addEventListener("input", renderOutput);
      el.addEventListener("change", renderOutput);
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

    $("#agent-gen-copy-config")?.addEventListener("click", () => {
      copyText($("#agent-gen-output")?.value, $("#agent-gen-copy-config"));
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
})();

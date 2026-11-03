/* Open and monitor the owning workspace's BloodHound service. */
(function () {
  const button = document.getElementById("bloodhound-open");
  const status = document.getElementById("bloodhound-status");
  const readiness = document.getElementById("bloodhound-readiness");
  const mcpLabel = document.getElementById("mcp-status-label");
  const mcpStatus = document.getElementById("exegol-mcp-status");
  const mcpHelp = document.getElementById("mcp-status-help");
  if (!button || !status) return;
  let healthTimer;
  let healthController;
  let launchController;
  let operationController;
  let healthGeneration = 0;

  // A workspace-level form keeps all actions available without selecting an agent.
  const form = document.getElementById("bloodhound-operation-form");
  if (form) {
    const action = document.getElementById("bloodhound-action");
    const runId = document.getElementById("bloodhound-run-id");
    const options = document.getElementById("bloodhound-options");
    const submit = document.getElementById("bloodhound-submit");
    const response = document.getElementById("bloodhound-response");
    for (const value of window.rmmBloodHoundActions) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      action.appendChild(option);
    }
    action.value = "health";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      let controller;
      let timeout;
      const generation = healthGeneration;
      try {
        const parsed = JSON.parse(options.value);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Options must be a JSON object.");
        submit.disabled = true;
        controller = new AbortController();
        operationController = controller;
        timeout = setTimeout(() => controller.abort(), 120000);
        response.textContent = "Waiting for response…";
        const result = await window.rmmApi("/bloodhound", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: action.value, run_id: runId.value.trim(), options: parsed }),
          signal: controller.signal,
        });
        if (generation === healthGeneration) response.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        if (generation === healthGeneration) response.textContent = controller?.signal.aborted
          ? "Response timed out. The action may still have executed; inspect status before resubmitting."
          : `BloodHound request failed: ${error.message}`;
      } finally {
        clearTimeout(timeout);
        if (operationController === controller) operationController = null;
        if (generation === healthGeneration) submit.disabled = false;
      }
    });
  }

  function message(text) {
    status.textContent = text;
    status.classList.toggle("hidden", !text);
  }

  function showRuntime(runtime) {
    if (!readiness || !runtime) return;
    readiness.textContent = `Exegol: ${runtime.exegol_status || "unknown"} · BloodHound: ${runtime.bloodhound_status || runtime.state || "unknown"}${runtime.error ? ` (${runtime.error})` : ""}`;
    const verified = ["connected", "verified"].includes(runtime.mcp_status);
    const failed = !!runtime.error || runtime.mcp_status === "error";
    if (mcpLabel) {
      mcpLabel.textContent = failed ? "MCP · Check needed" : verified ? "MCP · Verified" : "MCP · Checking";
      mcpLabel.dataset.state = failed ? "error" : verified ? "ready" : "checking";
    }
    if (mcpStatus) mcpStatus.textContent = `Exegol MCP: ${runtime.mcp_status || "not_checked"}${runtime.container ? ` · ${runtime.container}` : ""}`;
    if (mcpHelp) mcpHelp.textContent = runtime.error
      ? "Check the workspace Exegol MCP URL, authentication, and reachability from this container in Config. A recreated container may also need its Python MCP dependencies restored. This status does not identify the exact cause."
      : "Last workspace check; this is not a persistent MCP connection. RMM MCP is checked when an AI request uses it.";
  }

  function resetMcpStatus(label) {
    if (mcpLabel) { mcpLabel.textContent = `MCP · ${label}`; mcpLabel.dataset.state = "unknown"; }
    if (mcpStatus) mcpStatus.textContent = "Exegol MCP: Not checked";
    if (mcpHelp) mcpHelp.textContent = "No current workspace connection verification is available.";
  }

  function pause(signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new Error("Request canceled")); return; }
      const aborted = () => { clearTimeout(timer); reject(new Error("Request canceled")); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", aborted); resolve(); }, 1000);
      signal.addEventListener("abort", aborted, { once: true });
    });
  }

  async function operation(body, controller, budget) {
    for (;;) {
      if (controller.signal.aborted || budget.remaining-- <= 0) {
        throw new Error("BloodHound is still preparing. Check its status before trying again.");
      }
      const result = await window.rmmApi("/bloodhound", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), signal: controller.signal,
      });
      if (result.status !== 202 || !result.data?.workspace_request_id) return result;
      const requestId = result.data.workspace_request_id;
      if (!/^[a-f0-9]{32}$/.test(requestId)) throw new Error("Invalid BloodHound workspace response.");
      body = { action: "result", run_id: requestId };
      await pause(controller.signal);
    }
  }

  function stopHealth() {
    healthGeneration += 1;
    clearTimeout(healthTimer);
    healthController?.abort();
    launchController?.abort();
    operationController?.abort();
    operationController = null;
    if (form) {
      document.getElementById("bloodhound-response").textContent = "";
      document.getElementById("bloodhound-submit").disabled = false;
    }
    if (readiness) readiness.textContent = "";
    resetMcpStatus("Not checked");
  }

  function startHealth() {
    stopHealth();
    const generation = healthGeneration;
    async function refresh() {
      if (generation !== healthGeneration) return;
      const controller = new AbortController();
      healthController = controller;
      const timeout = setTimeout(() => controller.abort(), 30000);
      try {
        const result = await operation({ action: "health" }, controller, { remaining: 30 });
        if (generation !== healthGeneration) return;
        if (result.status === 200 && result.data?.runtime) showRuntime(result.data.runtime);
        else {
          resetMcpStatus("Unavailable");
          if (readiness) readiness.textContent = `BloodHound status unavailable: ${result.data?.error || "workspace did not respond"}`;
        }
      } catch (_error) {
        if (generation === healthGeneration) {
          resetMcpStatus("Unavailable");
          if (readiness) readiness.textContent = "BloodHound status unavailable";
        }
      } finally {
        clearTimeout(timeout);
        if (generation === healthGeneration) healthTimer = setTimeout(refresh, 10000);
      }
    }
    return refresh();
  }

  async function openBloodHound() {
    if (button.disabled) return;
    const tab = window.open("about:blank", "_blank");
    if (!tab) { message("Allow pop-ups for this WebUI to open BloodHound."); return; }
    tab.opener = null;
    tab.document.title = "Opening BloodHound";
    tab.document.body.textContent = "Waiting for the workspace's BloodHound service…";
    button.disabled = true;
    message("");
    const controller = new AbortController();
    launchController = controller;
    const timeout = setTimeout(() => controller.abort(), 120000);
    const budget = { remaining: 120 };
    try {
      let result = await operation({ action: "ui" }, controller, budget);
      while (result.status === 202 && result.data?.runtime) {
        showRuntime(result.data.runtime);
        if (tab.closed) throw new Error("BloodHound tab was closed.");
        const runtime = result.data.runtime;
        if (["blocked", "failed", "uncertain"].includes(runtime.state)) {
          throw new Error(runtime.error || `BloodHound startup is ${runtime.state}; inspect workspace status.`);
        }
        await pause(controller.signal);
        const health = await operation({ action: "health" }, controller, budget);
        if (health.status !== 200 || !health.data?.runtime) {
          throw new Error(health.data?.error || "Cannot check BloodHound startup.");
        }
        showRuntime(health.data.runtime);
        // Only a proven ready state permits obtaining a new browser launch link.
        result = health.data.runtime.state === "ready"
          ? await operation({ action: "ui" }, controller, budget)
          : { status: 202, data: health.data };
      }
      if (result.status !== 200) throw new Error(result.data?.error || "BloodHound is unavailable.");
      const url = new URL(result.data.url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
        throw new Error("Invalid BloodHound proxy address.");
      }
      if (tab.closed) throw new Error("BloodHound tab was closed.");
      tab.location.replace(url.href);
    } catch (error) {
      if (!tab.closed) tab.close();
      message(controller.signal.aborted
        ? "BloodHound opening was canceled or exceeded 120 seconds. Check workspace status before retrying."
        : `Cannot open BloodHound: ${error.message || "connection failed"}`);
    } finally {
      clearTimeout(timeout);
      if (launchController === controller) launchController = null;
      button.disabled = false;
    }
  }

  window.startBloodHoundHealth = startHealth;
  window.stopBloodHoundHealth = stopHealth;
  button.addEventListener("click", openBloodHound);
})();

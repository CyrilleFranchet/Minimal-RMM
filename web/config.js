/** Versioned export/import of supported browser UI configuration. */
(function () {
  const SCHEMA = "minimal-rmm-ui-config";
  const VERSION = 1;
  const SESSION_KEYS = [
    "rmm_sidebar_width",
    "rmm_agent_gen_prefs",
    "rmm_go_agent_prefs",
    "rmm_ai_panel_open",
    "rmm_ai_provider_state",
    "rmm_ai_provider",
    "rmm_exegol_mcp_enabled",
    "rmm_exegol_mcp_url",
    "rmm_ai_skills_enabled",
  ];
  const SECRET_KEYS = [
    "rmm_api_token",
    "rmm_openai_api_key",
    "rmm_anthropic_api_key",
    "rmm_mistral_api_key",
    "rmm_exegol_mcp_token",
  ];
  // These settings are synchronized to the authenticated server or workspace
  // SQLite store. The API token stays tab-local because it authenticates this
  // synchronization request.
  const PERSISTED_SESSION_KEYS = [
    "rmm_sidebar_width",
    "rmm_agent_gen_prefs",
    "rmm_go_agent_prefs",
    "rmm_ai_panel_open",
    "rmm_ai_provider_state",
    "rmm_ai_provider",
    "rmm_exegol_mcp_enabled",
    "rmm_exegol_mcp_url",
    "rmm_ai_skills_enabled",
    "rmm_shell_history",
    "rmm_event_cursors",
    "rmm_openai_api_key",
    "rmm_anthropic_api_key",
    "rmm_mistral_api_key",
    "rmm_exegol_mcp_token",
  ];
  const PERSISTED_LOCAL_KEYS = ["rmm_theme"];
  const $ = (selector) => document.querySelector(selector);
  let persistentStateReady = false;
  let persistTimer = null;
  let persistInFlight = null;

  function setStatus(message, error = false) {
    const status = $("#ui-config-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error-msg", error);
  }

  function readStorage(keys, storage = sessionStorage) {
    const values = {};
    for (const key of keys) {
      const value = storage.getItem(key);
      if (value !== null) values[key] = value;
    }
    return values;
  }

  function readPersistentState() {
    return {
      storage: {
        session: readStorage(PERSISTED_SESSION_KEYS),
        local: readStorage(PERSISTED_LOCAL_KEYS, localStorage),
      },
    };
  }

  function applyPersistentState(config) {
    const storage = config?.storage || config || {};
    const session = storage.session;
    const local = storage.local;
    if (session && typeof session === "object") {
      for (const key of PERSISTED_SESSION_KEYS) {
        if (typeof session[key] === "string") sessionStorage.setItem(key, session[key]);
        else sessionStorage.removeItem(key);
      }
    }
    if (local && typeof local === "object") {
      for (const key of PERSISTED_LOCAL_KEYS) {
        if (typeof local[key] === "string") localStorage.setItem(key, local[key]);
        else localStorage.removeItem(key);
      }
    }
  }

  async function persistNow() {
    if (!persistentStateReady || !window.rmmApi) return;
    if (persistInFlight) {
      window.persistRmmUiConfig();
      return;
    }
    persistInFlight = window.rmmApi("/ui/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readPersistentState()),
    }).finally(() => {
      persistInFlight = null;
    });
    await persistInFlight;
  }

  function schedulePersist() {
    if (!persistentStateReady) return;
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      persistTimer = null;
      persistNow().catch(() => {});
    }, 250);
  }

  async function loadPersistentConfiguration() {
    if (!window.rmmApi || (!window.rmmState?.token && !window.RMM_WORKSPACE_MODE)) return false;
    const result = await window.rmmApi("/ui/config");
    if (result.status !== 200 || !result.data) return false;
    persistentStateReady = true;
    if (result.data.exists && result.data.config) {
      applyPersistentState(result.data.config);
      window.dispatchEvent(new Event("rmm-config-imported"));
    } else {
      await persistNow();
    }
    return true;
  }

  window.loadRmmUiConfig = loadPersistentConfiguration;
  window.persistRmmUiConfig = schedulePersist;

  function readConfiguration(includeSecrets) {
    const session = readStorage(SESSION_KEYS);
    const agentPrefs = session.rmm_agent_gen_prefs;
    const secrets = {};
    if (agentPrefs) {
      try {
        const parsed = JSON.parse(agentPrefs);
        if (parsed && typeof parsed === "object" && parsed.beaconSecret) {
          if (includeSecrets) secrets["rmm_agent_gen_prefs.beaconSecret"] = parsed.beaconSecret;
          delete parsed.beaconSecret;
          session.rmm_agent_gen_prefs = JSON.stringify(parsed);
        }
      } catch {
        /* Keep malformed legacy preferences out of the export. */
        delete session.rmm_agent_gen_prefs;
      }
    }
    const goPrefs = session.rmm_go_agent_prefs;
    if (goPrefs) {
      try {
        const parsed = JSON.parse(goPrefs);
        if (parsed && typeof parsed === "object" && parsed.beaconSecret) {
          if (includeSecrets) secrets["rmm_go_agent_prefs.beaconSecret"] = parsed.beaconSecret;
          delete parsed.beaconSecret;
          session.rmm_go_agent_prefs = JSON.stringify(parsed);
        }
      } catch {
        delete session.rmm_go_agent_prefs;
      }
    }
    if (includeSecrets) Object.assign(secrets, readStorage(SECRET_KEYS));
    return {
      schema: SCHEMA,
      version: VERSION,
      exported_at: new Date().toISOString(),
      storage: {
        session,
        local: { rmm_theme: localStorage.getItem("rmm_theme") || "dark" },
      },
      secrets_included: includeSecrets,
      ...(includeSecrets ? { secrets } : {}),
    };
  }

  function downloadConfiguration() {
    const includeSecrets = Boolean($("#ui-config-include-secrets")?.checked);
    const config = readConfiguration(includeSecrets);
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `minimal-rmm-ui-config-v${VERSION}.json`;
    link.click();
    URL.revokeObjectURL(url);
    setStatus(includeSecrets ? "Configuration exported with secrets." : "Configuration exported without secrets.");
  }

  function importConfiguration(config) {
    if (!config || config.schema !== SCHEMA || config.version !== VERSION) {
      throw new Error("Unsupported configuration schema or version.");
    }
    const session = config.storage?.session;
    const local = config.storage?.local;
    if (!session || typeof session !== "object") throw new Error("Missing session configuration.");
    for (const key of SESSION_KEYS) {
      if (typeof session[key] !== "string") continue;
      let value = session[key];
      if (key === "rmm_ai_provider_state") {
        try {
          const importedState = JSON.parse(value);
          for (const provider of Object.values(importedState || {})) {
            if (provider && typeof provider === "object") provider.valid = false;
          }
          value = JSON.stringify(importedState);
        } catch {
          continue;
        }
      }
      if (key === "rmm_agent_gen_prefs") {
        try {
          const importedPrefs = JSON.parse(value);
          const currentPrefs = JSON.parse(sessionStorage.getItem(key) || "{}");
          if (importedPrefs && currentPrefs?.beaconSecret && !config.secrets?.["rmm_agent_gen_prefs.beaconSecret"]) {
            importedPrefs.beaconSecret = currentPrefs.beaconSecret;
            value = JSON.stringify(importedPrefs);
          }
        } catch {
          /* Preserve the existing preference if the imported value is malformed. */
          continue;
        }
      }
      if (key === "rmm_go_agent_prefs") {
        try {
          const importedPrefs = JSON.parse(value);
          const currentPrefs = JSON.parse(sessionStorage.getItem(key) || "{}");
          if (importedPrefs && currentPrefs?.beaconSecret && !config.secrets?.["rmm_go_agent_prefs.beaconSecret"]) {
            importedPrefs.beaconSecret = currentPrefs.beaconSecret;
            value = JSON.stringify(importedPrefs);
          }
        } catch {
          /* Preserve the existing preference if the imported value is malformed. */
          continue;
        }
      }
      sessionStorage.setItem(key, value);
    }
    if (local && (local.rmm_theme === "dark" || local.rmm_theme === "light")) {
      localStorage.setItem("rmm_theme", local.rmm_theme);
    }
    const secrets = config.secrets;
    if (secrets && typeof secrets === "object") {
      for (const key of SECRET_KEYS) {
        if (typeof secrets[key] === "string") sessionStorage.setItem(key, secrets[key]);
      }
      if (typeof secrets["rmm_agent_gen_prefs.beaconSecret"] === "string") {
        const raw = sessionStorage.getItem("rmm_agent_gen_prefs") || "{}";
        const prefs = JSON.parse(raw);
        prefs.beaconSecret = secrets["rmm_agent_gen_prefs.beaconSecret"];
        sessionStorage.setItem("rmm_agent_gen_prefs", JSON.stringify(prefs));
      }
      if (typeof secrets["rmm_go_agent_prefs.beaconSecret"] === "string") {
        const raw = sessionStorage.getItem("rmm_go_agent_prefs") || "{}";
        const prefs = JSON.parse(raw);
        prefs.beaconSecret = secrets["rmm_go_agent_prefs.beaconSecret"];
        sessionStorage.setItem("rmm_go_agent_prefs", JSON.stringify(prefs));
      }
    }
    window.dispatchEvent(new Event("rmm-config-imported"));
    schedulePersist();
  }

  function chooseConfigurationFile() {
    const input = $("#ui-config-file");
    if (!input) return;
    input.value = "";
    input.click();
  }

  async function handleImport(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      importConfiguration(JSON.parse(await file.text()));
      setStatus("Configuration imported. Current UI state was refreshed.");
    } catch (error) {
      setStatus(`Import failed: ${error.message || error}`, true);
    }
  }

  function initConfiguration() {
    const dialog = $("#ui-config-dialog");
    $("#config-toggle-btn")?.addEventListener("click", () => {
      if (!dialog) return;
      for (const section of dialog.querySelectorAll("details")) section.open = false;
      dialog.showModal();
      window.refreshPluginSources?.();
    });
    $("#ui-config-export")?.addEventListener("click", downloadConfiguration);
    $("#ui-config-import")?.addEventListener("click", chooseConfigurationFile);
    $("#ui-config-file")?.addEventListener("change", handleImport);
  }

  window.addEventListener("DOMContentLoaded", initConfiguration);
})();

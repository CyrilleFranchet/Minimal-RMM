/* Service-owned persistent settings; credentials never enter browser storage. */
(function () {
  const $ = (id) => document.getElementById(`service-settings-${id}`);
  let fields = [];
  let loaded = false;
  let busy = false;

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
  }

  function valueText(value, type) {
    return type === "json" ? JSON.stringify(value, null, 2) : String(value ?? "");
  }

  function render(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.settings)) throw new Error("Invalid settings response.");
    fields = [];
    $("fields").replaceChildren();
    const groups = new Map();
    for (const setting of snapshot.settings) {
      let group = groups.get(setting.group);
      if (!group) {
        group = document.createElement("fieldset");
        const legend = document.createElement("legend"); legend.textContent = setting.group || "General";
        group.append(legend); groups.set(setting.group, group); $("fields").append(group);
      }
      const row = document.createElement("div"); row.className = "service-setting";
      const label = document.createElement("label"); label.textContent = setting.label || setting.key;
      const input = document.createElement(setting.secret ? "input" : setting.choices?.length ? "select" : setting.type === "json" ? "textarea" : "input");
      input.id = `service-setting-${setting.key}`; label.htmlFor = input.id;
      input.autocomplete = "off";
      const initial = setting.saved_value !== null && setting.saved_value !== undefined ? setting.saved_value : setting.value;
      if (setting.secret) {
        input.type = "password"; input.value = "";
        input.placeholder = setting.configured ? "Configured — leave blank to keep" : "Not configured";
        input.autocomplete = "new-password";
      } else if (setting.choices?.length) {
        for (const choice of setting.choices) {
          const option = document.createElement("option"); option.value = choice; option.textContent = choice; input.append(option);
        }
        input.value = initial;
      } else if (setting.type === "boolean") {
        input.type = "checkbox"; input.checked = Boolean(initial);
      } else {
        input.type = setting.type === "integer" || setting.type === "number" ? "number" : "text";
        if (input.type === "number") input.step = setting.type === "integer" ? "1" : "any";
        if (setting.minimum !== null && setting.minimum !== undefined) input.min = setting.minimum;
        if (setting.maximum !== null && setting.maximum !== undefined) input.max = setting.maximum;
        input.value = valueText(initial, setting.type);
        if (setting.type === "json") { input.rows = 4; input.spellcheck = false; }
      }
      const help = document.createElement("p"); help.id = `${input.id}-help`; help.className = "ai-hint";
      help.textContent = `${setting.key} · Source: ${setting.source}${setting.restart_required ? " · Restart pending" : ""}${setting.saved ? " · Saved override present" : ""}. ` +
        (setting.secret ? `Leave blank to keep the current secret. Values are never returned.${setting.type === "json" ? " Enter a JSON object or array to replace it." : ""}` : `Effective value: ${valueText(setting.value, setting.type)}.`);
      input.setAttribute("aria-describedby", help.id);
      const resetLabel = document.createElement("label"); resetLabel.className = "service-setting-reset";
      const reset = document.createElement("input"); reset.type = "checkbox";
      const resetText = document.createElement("span"); resetText.textContent = " Remove saved override (environment or default will apply)";
      resetLabel.append(reset, resetText);
      reset.addEventListener("change", () => { input.disabled = reset.checked; });
      row.append(label, input, help, resetLabel); group.append(row);
      fields.push({ setting, input, reset, initial });
    }
    $("scope").textContent = `Settings owner: ${snapshot.scope}. Changes apply after restarting this process. Environment variables override saved values.`;
    loaded = true;
    status(snapshot.restart_required ? "Saved changes are pending a process restart." : "Settings loaded.");
  }

  async function request(method, values) {
    const response = await window.rmmApi("/settings", { method, headers: { "Content-Type": "application/json" },
      ...(values === undefined ? {} : { body: JSON.stringify({ settings: values }) }) });
    if (response.status !== 200) throw new Error(response.data?.detail || response.data?.error || `HTTP ${response.status}`);
    return response.data;
  }

  function changes() {
    const result = {};
    for (const { setting, input, reset, initial } of fields) {
      if (reset.checked) { result[setting.key] = null; continue; }
      if (setting.secret) {
        if (input.value !== "") {
          if (setting.type === "json") {
            try { result[setting.key] = JSON.parse(input.value); } catch { throw new Error(`${setting.label}: invalid JSON.`); }
          } else result[setting.key] = input.value;
        }
        continue;
      }
      let value = input.value;
      if (setting.type === "boolean") value = input.checked;
      else if (setting.type === "integer" || setting.type === "number") {
        if (!input.value.trim()) throw new Error(`${setting.label}: enter a number or remove the saved override.`);
        value = Number(input.value);
        if (!Number.isFinite(value) || (setting.type === "integer" && !Number.isSafeInteger(value))) throw new Error(`${setting.label}: invalid ${setting.type}.`);
      } else if (setting.type === "json") {
        try { value = JSON.parse(input.value); } catch { throw new Error(`${setting.label}: invalid JSON.`); }
      }
      if (JSON.stringify(value) !== JSON.stringify(initial)) result[setting.key] = value;
    }
    return result;
  }

  async function perform(action) {
    if (busy) return;
    busy = true; $("fields").disabled = true; $("save").disabled = true; $("reload").disabled = true; $("add-credential").disabled = true;
    $("credential-key").disabled = true; $("credential-value").disabled = true;
    try { await action(); } catch (error) { status(error.message, true); }
    finally {
      busy = false; $("fields").disabled = !loaded; $("save").disabled = !loaded; $("reload").disabled = false; $("add-credential").disabled = !loaded;
      $("credential-key").disabled = !loaded; $("credential-value").disabled = !loaded;
    }
  }

  async function load() {
    if (busy) return;
    loaded = false;
    await perform(async () => { status("Loading service settings…"); render(await request("GET")); });
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("reload").addEventListener("click", load);
    $("save").addEventListener("click", () => perform(async () => {
      const values = changes();
      if (!Object.keys(values).length) { status("No changes to save."); return; }
      status("Saving settings…"); render(await request("PATCH", values));
      status("Settings saved. Restart this process to apply changes; environment overrides still take precedence.");
    }));
    $("add-credential").addEventListener("click", () => perform(async () => {
      if (Object.keys(changes()).length) throw new Error("Save or reload your other settings before adding a credential.");
      const key = $("credential-key").value.trim();
      const value = $("credential-value").value;
      if (!/^RMM_PLUGIN_TOKEN_[A-Z0-9_]{1,100}$/.test(key)) throw new Error("Use RMM_PLUGIN_TOKEN_<NAME> with uppercase letters, digits, or underscores.");
      if (!value) throw new Error("Enter a credential value.");
      status("Saving credential…"); render(await request("PATCH", { [key]: value }));
      $("credential-key").value = ""; $("credential-value").value = "";
      status("Credential saved. Restart this process, then select its key in the plugin source editor.");
    }));
    document.getElementById("config-toggle-btn").addEventListener("click", load);
  });
})();

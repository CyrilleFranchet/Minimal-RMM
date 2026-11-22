/* Workspace credential editor. Secrets stay in the current dialog only. */
(function () {
  const $ = (id) => document.getElementById(`credentials-${id}`);
  let generation = 0;
  let busy = false;
  let editing = null;
  let originalPassword = null;
  let listReady = false;
  let listVersion = 0;

  function context() {
    const state = window.rmmState || {};
    return [generation, state.authGeneration || 0, state.connectionGeneration || 0, state.token || ""].join("|");
  }

  function current(owner) { return $("dialog").open && owner === context(); }

  function status(message, error = false) {
    $("status").textContent = message;
    $("status").classList.toggle("error", error);
  }

  function editor(row = null) {
    editing = row?.cred_id || null;
    originalPassword = row?.password ?? null;
    $("form").reset();
    $("password").disabled = false;
    $("password").type = editing ? "text" : "password";
    $("password").value = row?.password ?? "";
    $("identity").value = row?.label || "";
    $("editor-title").textContent = editing ? `Edit ${editing}` : "Add credential";
    $("password-help").textContent = editing
      ? "The stored password is shown. Edit it to replace it, leave unchanged or blank to keep it, or select Set empty password."
      : "An empty password is allowed.";
    $("empty-label").hidden = !editing;
    $("save").textContent = editing ? "Save changes" : "Add credential";
  }

  function invalidateList() {
    listReady = false;
    listVersion++;
    $("list").replaceChildren();
    $("purge").disabled = true;
    $("save").disabled = true;
  }

  function reset() {
    generation++;
    if ($("dialog").open) $("dialog").close();
    invalidateList();
    editor();
    status("");
    busy = false;
    $("controls").disabled = false;
  }

  async function request(path, method = "GET", body) {
    const result = await window.rmmApi(path, {
      method, ...(body === undefined ? {} : {
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }),
    });
    if (result.status !== 200 || result.data?.ok === false) {
      throw new Error(result.status === 0 ? "Cannot reach the credential store. Refresh to check its current state."
        : `Credential request failed (HTTP ${result.status}).`);
    }
    return result.data;
  }

  function render(rows, owner) {
    const version = listVersion;
    $("list").replaceChildren();
    $("purge").disabled = rows.length === 0;
    if (!rows.length) {
      const empty = document.createElement("p"); empty.textContent = "No credentials stored.";
      $("list").append(empty);
    }
    for (const row of rows) {
      const item = document.createElement("div"); item.className = "credentials-row";
      const label = document.createElement("span"); label.textContent = `${row.label} · ${row.cred_id}`;
      item.append(label);
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "secondary";
      edit.textContent = "Edit"; edit.setAttribute("aria-label", `Edit ${row.label}`);
      edit.addEventListener("click", () => {
        if (!current(owner) || busy || !listReady || version !== listVersion) return;
        perform(async (active) => {
          status("Loading credential for editing…");
          const data = await request("/creds?include_passwords=1");
          if (!current(active) || !listReady || version !== listVersion) return;
          const entry = data?.creds?.find((credential) => credential.cred_id === row.cred_id);
          if (!entry || typeof entry.password !== "string") throw new Error("Credential unavailable.");
          editor(entry); $("identity").focus();
          status("Credential loaded for editing.");
        });
      });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary";
      remove.textContent = "Delete"; remove.setAttribute("aria-label", `Delete ${row.label}`);
      remove.addEventListener("click", () => {
        if (!current(owner) || busy || !listReady || version !== listVersion || !window.confirm(`Delete credential ${row.label} (${row.cred_id})?`)) return;
        perform(async (active) => {
          await request(`/creds/${encodeURIComponent(row.cred_id)}`, "DELETE");
          if (!current(active)) return;
          if (editing === row.cred_id) editor();
          await refreshAfterMutation(active, "Credential deleted.");
        });
      });
      item.append(edit, remove); $("list").append(item);
    }
  }

  async function load(owner) {
    invalidateList();
    try {
      const data = await request("/creds");
      if (!current(owner)) return;
      if (!Array.isArray(data?.creds)) throw new Error("Invalid credential list.");
      render(data.creds, owner);
      listReady = true;
      $("save").disabled = false;
      status("Credentials loaded.");
    } catch (error) {
      if (current(owner)) invalidateList();
      throw error;
    }
  }

  async function refreshAfterMutation(owner, message) {
    try {
      await load(owner);
      if (current(owner)) status(message);
    } catch {
      if (current(owner)) status(message + " List refresh failed. Refresh the list before making more changes; do not repeat the completed operation.", true);
    }
  }

  async function perform(action) {
    if (busy || !$("dialog").open) return;
    const owner = context();
    busy = true; $("controls").disabled = true;
    try { await action(owner); }
    catch {
      if (current(owner)) {
        invalidateList();
        status("Credential request failed or its result could not be confirmed. Refresh to check the store before making more changes.", true);
      }
    }
    finally { if (current(owner)) { busy = false; $("controls").disabled = false; } }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("open").addEventListener("click", () => {
      reset(); $("dialog").showModal();
      status("Loading credentials…"); perform(load);
    });
    $("close").addEventListener("click", reset);
    $("dialog").addEventListener("cancel", (event) => { event.preventDefault(); reset(); });
    $("dialog").addEventListener("close", () => {
      // A queued close event from an earlier opening must not reset a reopened dialog.
      if (!$("dialog").open) reset();
    });
    $("refresh").addEventListener("click", () => perform(load));
    $("cancel-edit").addEventListener("click", () => { editor(); $("identity").focus(); });
    $("empty").addEventListener("change", () => {
      $("password").disabled = $("empty").checked;
      if ($("empty").checked) $("password").value = "";
    });
    $("form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (busy || !listReady) return;
      const identity = $("identity").value.trim();
      const separator = identity.indexOf("\\");
      const body = { domain: separator < 0 ? "" : identity.slice(0, separator),
        user: separator < 0 ? identity : identity.slice(separator + 1) };
      if (!body.user.trim()) { status("Enter a username.", true); return; }
      if (!editing || $("empty").checked || ($("password").value !== "" && $("password").value !== originalPassword)) {
        body.password = $("password").value;
      }
      const id = editing;
      perform(async (owner) => {
        await request(id ? `/creds/${encodeURIComponent(id)}` : "/creds", id ? "PATCH" : "POST", body);
        if (!current(owner)) return;
        editor();
        await refreshAfterMutation(owner, id ? "Credential updated." : "Credential added.");
      });
    });
    $("purge").addEventListener("click", () => {
      if (busy || !listReady || !window.confirm("Delete ALL credentials in this workspace? This cannot be undone.")) return;
      perform(async (owner) => {
        const data = await request("/creds", "DELETE");
        if (!current(owner)) return;
        editor();
        await refreshAfterMutation(owner, `Deleted ${data.deleted} credentials.`);
      });
    });
  });
  window.rmmCredentials = { reset };
})();

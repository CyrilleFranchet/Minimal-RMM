const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const tick = () => new Promise(setImmediate);

function fixture() {
  class Element {
    constructor() { this.value = ''; this.checked = false; this.disabled = false; this.open = false; this.children = []; this.handlers = {}; this.classList = { toggle() {} }; }
    addEventListener(event, handler) { this.handlers[event] = handler; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren() { this.children = []; }
    setAttribute() {}
    focus() {}
    reset() { for (const id of ['identity', 'password', 'empty']) { get(id).value = ''; get(id).checked = false; } }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const calls = []; let rows = []; let accepted = true; let pending; let respond;
  const window = { rmmState: { authGeneration: 1, connectionGeneration: 1, token: 'fixture' }, confirm: () => accepted,
    rmmApi: async (url, options) => {
      calls.push({ url, ...options });
      if (respond) return respond(url, options);
      if (pending) return pending;
      return { status: 200, data: { ok: true, creds: rows, deleted: rows.length } };
    } };
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../web/credentials.js'), 'utf8'), {
    window, document: { getElementById: (id) => get(id.replace('credentials-', '')), createElement: () => new Element(), addEventListener: (_, fn) => fn() },
  });
  return { get, calls, window, respond: (value) => { respond = value; }, rows: (value) => { rows = value; }, confirm: (value) => { accepted = value; }, pending: (value) => { pending = value; },
    click: async (id) => { get(id).handlers.click(); await tick(); },
    submit: async () => { get('form').handlers.submit({ preventDefault() {} }); await tick(); } };
}

test('dialog loads redacted list, adds and edits while preserving ID and exact password', async () => {
  const ui = fixture(); ui.rows([{ cred_id: 'cred-12345678', label: 'LAB\\alice' }]);
  await ui.click('open');
  assert.equal(ui.calls[0].url, '/creds');
  assert.equal(ui.get('dialog').open, true);
  ui.get('identity').value = 'LAB\\bob'; ui.get('password').value = "  quoted ' secret  ";
  await ui.submit();
  assert.deepEqual(JSON.parse(ui.calls[1].body), { domain: 'LAB', user: 'bob', password: "  quoted ' secret  " });
  ui.get('list').children[0].children[1].handlers.click();
  assert.equal(ui.get('identity').value, 'LAB\\alice');
  ui.get('identity').value = 'LAB\\new'; await ui.submit();
  const edit = ui.calls.find((call) => call.method === 'PATCH');
  assert.equal(edit.url, '/creds/cred-12345678');
  assert.deepEqual(JSON.parse(edit.body), { domain: 'LAB', user: 'new' });
  ui.get('list').children[0].children[1].handlers.click();
  ui.get('empty').checked = true; ui.get('empty').handlers.change(); await ui.submit();
  assert.equal(JSON.parse(ui.calls.filter((call) => call.method === 'PATCH')[1].body).password, '');
  assert.equal(ui.get('password').disabled, false);
});

test('single and bulk deletion require confirmation', async () => {
  const ui = fixture(); ui.rows([{ cred_id: 'cred-12345678', label: '<script>literal</script>' }]); await ui.click('open');
  assert.equal(ui.get('list').children[0].children[0].textContent, '<script>literal</script> · cred-12345678');
  ui.confirm(false); ui.get('list').children[0].children[2].handlers.click(); await ui.click('purge');
  assert.equal(ui.calls.filter((call) => call.method === 'DELETE').length, 0);
  ui.confirm(true); ui.get('list').children[0].children[2].handlers.click(); await tick(); await ui.click('purge');
  assert.deepEqual(ui.calls.filter((call) => call.method === 'DELETE').map((call) => call.url), ['/creds/cred-12345678', '/creds']);
});

test('close and reset clear secrets and reject a late response', async () => {
  const ui = fixture(); let release;
  ui.pending(new Promise((resolve) => { release = resolve; }));
  await ui.click('open'); ui.get('password').value = 'secret'; await ui.click('close');
  release({ status: 200, data: { creds: [{ cred_id: 'old', label: 'old workspace' }] } }); await tick();
  assert.equal(ui.get('password').value, ''); assert.equal(ui.get('list').children.length, 0);
  ui.pending(null); await ui.click('open'); ui.get('password').value = 'secret'; ui.window.rmmCredentials.reset();
  assert.equal(ui.get('dialog').open, false); assert.equal(ui.get('password').value, '');
});

test('failures retain form for retry and prevent duplicate requests while busy', async () => {
  const ui = fixture(); await ui.click('open'); let release;
  ui.pending(new Promise((resolve) => { release = resolve; }));
  ui.get('identity').value = 'alice'; ui.get('password').value = 'retry';
  await ui.submit(); await ui.submit(); assert.equal(ui.calls.filter((call) => call.method === 'POST').length, 1);
  release({ status: 503, data: { error: 'secret must not be displayed' } }); await tick();
  assert.equal(ui.get('controls').disabled, false); assert.equal(ui.get('password').value, 'retry');
  assert.match(ui.get('status').textContent, /failed/); assert.doesNotMatch(ui.get('status').textContent, /secret/);
});

test('authentication generation changes reject pending lists and stale row actions', async () => {
  const ui = fixture(); ui.rows([{ cred_id: 'old', label: 'old' }]); await ui.click('open');
  const staleEdit = ui.get('list').children[0].children[1].handlers.click;
  const staleDelete = ui.get('list').children[0].children[2].handlers.click;
  let release; ui.pending(new Promise((resolve) => { release = resolve; })); await ui.click('refresh');
  ui.window.rmmState.authGeneration++;
  ui.get('identity').value = 'new workspace';
  release({ status: 200, data: { creds: [{ cred_id: 'leaked', label: 'leaked' }] } }); await tick();
  staleEdit(); staleDelete(); await tick();
  assert.equal(ui.get('identity').value, 'new workspace');
  assert.equal(ui.get('list').children.length, 0);
  assert.equal(ui.calls.filter((call) => call.method === 'DELETE').length, 0);
});


test('completed mutations remain successful when the list refresh fails and block stale actions', async () => {
  for (const action of ['add', 'edit', 'delete', 'purge']) {
    const ui = fixture(); ui.rows([{ cred_id: 'cred-12345678', label: 'LAB\\alice' }]); await ui.click('open');
    const row = ui.get('list').children[0];
    const staleEdit = row.children[1].handlers.click;
    const staleDelete = row.children[2].handlers.click;
    ui.respond((url, options) => options.method === 'GET'
      ? { status: 503, data: {} }
      : { status: 200, data: { ok: true, deleted: 1 } });
    if (action === 'add' || action === 'edit') {
      if (action === 'edit') staleEdit();
      ui.get('identity').value = 'updated'; ui.get('password').value = 'secret'; await ui.submit();
    } else if (action === 'delete') { staleDelete(); await tick(); }
    else await ui.click('purge');
    assert.match(ui.get('status').textContent, /(?:added|updated|deleted|Deleted)/);
    assert.match(ui.get('status').textContent, /List refresh failed/);
    assert.doesNotMatch(ui.get('status').textContent, /operation failed/);
    assert.equal(ui.get('list').children.length, 0);
    assert.equal(ui.get('purge').disabled, true);
    assert.equal(ui.get('save').disabled, true);
    assert.equal(ui.get('password').value, '');
    const count = ui.calls.length;
    staleEdit(); staleDelete(); await ui.click('purge'); await ui.submit();
    assert.equal(ui.calls.length, count, 'stale controls must not issue another mutation');
    ui.respond(null); await ui.click('refresh');
    assert.equal(ui.get('save').disabled, false);
    assert.equal(ui.get('purge').disabled, false);
    staleDelete(); await tick();
    assert.equal(ui.calls.length, count + 1, 'old rows remain invalid after list recovery');
  }
});

test('failed manual list refresh disables edits and deletes until a successful refresh', async () => {
  const ui = fixture(); ui.rows([{ cred_id: 'stored', label: 'alice' }]); await ui.click('open');
  const row = ui.get('list').children[0]; row.children[1].handlers.click();
  ui.respond(() => ({ status: 503, data: {} })); await ui.click('refresh');
  assert.equal(ui.get('save').disabled, true); assert.equal(ui.get('purge').disabled, true);
  assert.equal(ui.get('list').children.length, 0);
  const count = ui.calls.length;
  row.children[1].handlers.click(); row.children[2].handlers.click(); await ui.submit(); await ui.click('purge');
  assert.equal(ui.calls.length, count);
  ui.respond(null); await ui.click('refresh'); await ui.submit();
  assert.equal(ui.calls.filter((call) => call.method === 'PATCH').length, 1);
});


test('uncertain mutation results require a successful refresh before any further writes', async () => {
  for (const failure of ['http-zero', 'rejected-request']) {
    const ui = fixture(); ui.rows([{ cred_id: 'stored', label: 'alice' }]); await ui.click('open');
    const row = ui.get('list').children[0];
    ui.get('identity').value = 'draft'; ui.get('password').value = 'preserved';
    ui.respond(() => {
      if (failure === 'rejected-request') throw new Error('fetch rejected');
      return { status: 0, data: { error: 'network_error' } };
    });
    await ui.submit();
    assert.equal(ui.get('identity').value, 'draft'); assert.equal(ui.get('password').value, 'preserved');
    assert.equal(ui.get('save').disabled, true); assert.equal(ui.get('purge').disabled, true);
    assert.equal(ui.get('list').children.length, 0);
    assert.match(ui.get('status').textContent, /could not be confirmed/);
    const count = ui.calls.length;
    await ui.submit(); await ui.click('purge'); row.children[1].handlers.click(); row.children[2].handlers.click();
    assert.equal(ui.calls.length, count);
    await ui.click('refresh');
    assert.equal(ui.get('save').disabled, true, 'failed refresh cannot reenable writes');
    ui.respond(null); await ui.click('refresh');
    assert.equal(ui.get('save').disabled, false); assert.equal(ui.get('password').value, 'preserved');
    await ui.submit();
    assert.equal(ui.calls.filter((call) => call.method === 'POST').length, 2);
    assert.equal(ui.get('password').value, '');
  }
});

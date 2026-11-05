/* Profile failure visibility and asset ownership across reconnects. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function fixture(respond) {
  class Element {
    constructor() {
      this.value = ''; this.textContent = ''; this.handlers = {}; this.children = [];
      this.classList = { toggle() {} }; this.files = [];
    }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
  }
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const calls = [];
  const window = { rmmState: { authGeneration: 1, token: 'alice' },
    rmmApi: async (url, args = {}) => { calls.push({ url, args }); return respond(url, args); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/assets.js'), 'utf8'), {
    window, document: { readyState: 'complete', querySelector: get, createElement: () => new Element() },
    btoa: (value) => Buffer.from(value, 'binary').toString('base64'), Uint8Array, console,
  });
  return { window, calls, get, click: (id) => get(id).handlers.click() };
}

test('profile start and stop failures remain visible after transport refresh', async () => {
  for (const action of ['start', 'stop']) {
    const ui = fixture((url) => url === '/profile'
      ? { status: 503, data: { detail: 'Connector failed to start' } }
      : { status: 200, data: { beacon_url: '', profile: { active: null } } });
    await ui.click(`#asset-profile-${action}`);
    assert.equal(ui.get('#asset-profile-status').textContent, 'Connector failed to start');
    assert.equal(ui.calls[1].url, '/transport');
  }
});

test('old transport response cannot restore previous login beacon or status', async () => {
  let release;
  const ui = fixture(() => new Promise((resolve) => { release = resolve; }));
  const pending = ui.window.refreshRmmTransport();
  ui.window.rmmState.authGeneration++;
  ui.get('#asset-profile-status').textContent = 'New connection';
  release({ status: 200, data: { beacon_url: 'https://old.example', profile: { active: { running: true, name: 'old' } } } });
  await pending;
  assert.equal(ui.window.rmmBeaconUrl, undefined);
  assert.equal(ui.get('#asset-profile-status').textContent, 'New connection');
});

test('switching owners during file decoding does not upload into the new workspace', async () => {
  let release;
  const ui = fixture(() => { throw new Error('Must not submit'); });
  ui.get('#asset-kind').value = 'plugins';
  ui.get('#asset-file').files = [{ name: 'example.dll', arrayBuffer: () => new Promise((resolve) => { release = resolve; }) }];
  const pending = ui.get('#asset-file').handlers.change();
  ui.window.rmmState.authGeneration++;
  ui.window.rmmState.token = 'bob';
  ui.get('#asset-status').textContent = 'Bob workspace';
  release(new Uint8Array([1, 2, 3]).buffer);
  await pending;
  assert.equal(ui.calls.length, 0);
  assert.equal(ui.get('#asset-status').textContent, 'Bob workspace');
});

test('rendered asset buttons cannot act under a later connection', async () => {
  const ui = fixture(() => ({ status: 200, data: { assets: [{ kind: 'plugins', name: 'same.dll', enabled: true }] } }));
  await ui.window.refreshAssets();
  const row = ui.get('#asset-list').children[0];
  ui.window.rmmState.authGeneration++;
  await row.children[1].handlers.click();
  await row.children[2].handlers.click();
  assert.equal(ui.calls.length, 1);
});

test('legacy skills have no visible asset selection or enable controls', async () => {
  const ui = fixture(() => ({ status: 200, data: { assets: [
    { kind: 'skills', name: 'legacy.md', enabled: true },
    { kind: 'plugins', name: 'example.dll', enabled: true },
  ] } }));
  await ui.window.refreshAssets();
  const rows = ui.get('#asset-list').children;
  assert.equal(rows.length, 1);
  assert.match(rows[0].children[0].textContent, /plugins\/example.dll/);
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.ok(!html.includes('<option value="skills">'));
  assert.ok(!html.includes('id="ai-skills-list"'));
  assert.ok(html.includes('id="ai-playbook"'));
});

/* Verify explicit release selection with a minimal DOM; no live GitHub access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.join(__dirname, '..');
const tag = `plugins-${'a'.repeat(40)}`;
const digest = 'b'.repeat(64);

function fixture(installResponse = { status: 200, data: { cached: true } }, browseResponse) {
  class Element {
    constructor() { this.value = ''; this.children = []; this.handlers = {}; this.classList = { toggle() {} }; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    replaceChildren() { this.children = []; this.value = ''; }
    appendChild(child) { this.children.push(child); if (!this.value) this.value = child.value; }
  }
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="(plugin-release-[^"]+)"/g)].map((m) => [m[1], new Element()]));
  const calls = [];
  let refreshed = 0;
  const get = (id) => elements.get(`plugin-release-${id}`);
  get('tag').value = 'latest';
  const document = {
    readyState: 'complete',
    getElementById(id) { assert.ok(elements.has(id), id); return elements.get(id); },
    createElement() { return new Element(); },
  };
  const window = {
    async rmmApi(url, options = {}) {
      calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
      if (url.startsWith('/plugin-releases?')) return browseResponse || {
        status: 200, data: { repository: 'owner/plugins', tag, plugins: [{ name: 'example.dll', size: 5, sha256: digest }] },
      };
      assert.equal(url, '/plugin-releases/install');
      return installResponse;
    },
    async refreshAssets() { refreshed++; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/plugin-releases.js'), 'utf8'), { document, window });
  return { get, calls, click: async (id) => get(id).handlers.click(), refreshed: () => refreshed };
}

test('latest is resolved before install, and the selected checksum is sent', async () => {
  const ui = fixture();
  await ui.click('browse');
  assert.equal(ui.get('install').disabled, false);
  await ui.click('install');
  assert.deepEqual(ui.calls[1].body, { name: 'example.dll', tag, sha256: digest });
  assert.equal(ui.refreshed(), 1);
  assert.match(ui.get('status').textContent, /SHA-256 verified/);
  assert.equal(ui.get('install').disabled, false);
});

test('editing the release clears stale selections before installation', async () => {
  const ui = fixture();
  await ui.click('browse');
  ui.get('tag').value = `plugins-${'c'.repeat(40)}`;
  ui.get('tag').handlers.input();
  assert.equal(ui.get('install').disabled, true);
  await ui.click('install');
  assert.equal(ui.calls.length, 1);
});

test('workspace sync failure is distinguished from a failed local install', async () => {
  const ui = fixture({ status: 502, data: { local_installed: true, error: 'upstream unavailable' } });
  await ui.click('browse');
  await ui.click('install');
  assert.match(ui.get('status').textContent, /Installed locally, but server synchronization failed/);
  assert.equal(ui.refreshed(), 1);
  assert.equal(ui.get('install').disabled, false);
});

test('failed browsing cannot leave a release available for install', async () => {
  const ui = fixture(undefined, { status: 503, data: { error: 'GitHub credentials unavailable' } });
  await ui.click('browse');
  assert.equal(ui.get('install').disabled, true);
  assert.equal(ui.get('browse').disabled, false);
  assert.match(ui.get('status').textContent, /credentials unavailable/);
});

/* Verify persisted sources and pinned releases through a minimal DOM adapter. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.join(__dirname, '..');
const tag = `plugins-${'a'.repeat(12)}-v1.2`;
const digest = 'b'.repeat(64);
const revision = 'c'.repeat(64);

function fixture(installResponse = { status: 200, data: { cached: true } }, browseResponse, sourcesResponse) {
  class Element {
    constructor() { this.value = ''; this.children = []; this.handlers = {}; this.classList = { toggle() {} }; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    replaceChildren() { this.children = []; this.value = ''; }
    appendChild(child) { this.children.push(child); if (!this.value) this.value = child.value; }
  }
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="(plugin-release-[^"]+)"/g)].map((m) => [m[1], new Element()]));
  const calls = [];
  const sources = [{ id: 'default', repository: 'owner/plugins', branch: 'main', label: 'Stable', credential_env: 'RMM_PLUGIN_GITHUB_TOKEN', credential_configured: true },
    { id: 'dev', repository: 'other/plugins', branch: 'feature/test', credential_env: 'RMM_PLUGIN_TOKEN_DEV', credential_configured: false }];
  let refreshed = 0;
  let failRefresh = false;
  let failSave = false;
  const get = (id) => elements.get(`plugin-release-${id}`);
  get('tag').value = 'latest';
  const document = {
    readyState: 'complete',
    getElementById(id) { assert.ok(elements.has(id), id); return elements.get(id); },
    createElement() { return new Element(); },
  };
  const window = {
    async rmmApi(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, method: options.method || 'GET', body });
      if (url === '/plugin-sources' && sourcesResponse) return sourcesResponse;
      if (url === '/plugin-sources') return failRefresh ? { status: 503, data: { error: 'offline' } } : { status: 200, data: { sources: sources.map((row) => ({ ...row })) } };
      if (url.startsWith('/plugin-sources/')) {
        if (failSave && options.method === 'POST') return { status: 500, data: { error: 'plugin source storage failed' } };
        const id = decodeURIComponent(url.split('/').pop());
        const at = sources.findIndex((source) => source.id === id);
        if (at !== -1) sources.splice(at, 1);
        if (options.method === 'POST') sources.push({ id, ...body });
        return { status: 200, data: options.method === 'POST' ? { source: sources.find((row) => row.id === id) } : { ok: true } };
      }
      if (url.startsWith('/plugin-releases?')) return browseResponse || {
        status: 200, data: { repository: 'owner/plugins', branch: 'main', source: get('source').value,
          source_revision: revision, version: '1.2', tag, plugins: [{ name: 'example.dll', size: 5, sha256: digest }] },
      };
      assert.equal(url, '/plugin-releases/install');
      return typeof installResponse === "function" ? installResponse(body) : installResponse;
    },
    async refreshAssets() { refreshed++; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/plugin-releases.js'), 'utf8'), { document, window });
  return { get, calls, sources, load: window.refreshPluginSources,
    click: async (id) => id === 'source-save' ? get('source-form').handlers.submit({ preventDefault() {} }) : get(id).handlers.click(), refreshed: () => refreshed,
    failRefresh: () => { failRefresh = true; }, failSave: () => { failSave = true; } };
}

test('latest resolves the selected source and pins tag, digest, and source revision', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  assert.equal(ui.get('install').disabled, false);
  assert.match(ui.get('info').textContent, /version 1.2/);
  await ui.click('install');
  assert.deepEqual(ui.calls.at(-1).body, { name: 'example.dll', tag, sha256: digest, source: 'default', source_revision: revision });
  assert.equal(ui.refreshed(), 1);
  assert.match(ui.get('status').textContent, /SHA-256 verified/);
});

test('changing the source or editing its configuration clears a browsed release', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  ui.get('source').value = 'dev';
  ui.get('source').handlers.change();
  assert.equal(ui.get('install').disabled, true);
  assert.equal(ui.get('source-repository').value, 'other/plugins');
  assert.equal(ui.get('source-branch').value, 'feature/test');
  await ui.click('browse');
  await ui.click('select-all');
  assert.match(ui.calls.at(-1).url, /source=dev/);
  ui.get('source-branch').value = 'other';
  ui.get('source-branch').handlers.input();
  assert.equal(ui.get('install').disabled, true);
  assert.equal(ui.get('browse').disabled, true);
  await ui.click('install');
  assert.ok(ui.calls.every((call) => call.url !== '/plugin-releases/install'));
});

test('sources can be added, reloaded, edited, and removed with credential names only', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('source-new');
  assert.equal(ui.get('source-editor').open, true);
  for (const [id, value] of Object.entries({ 'source-id': 'lab', 'source-label': '',
    'source-repository': 'team/plugins', 'source-branch': 'lab/builds', 'source-credential': 'RMM_PLUGIN_TOKEN_LAB' })) {
    ui.get(id).value = value;
    ui.get(id).handlers.input();
  }
  await ui.click('source-save');
  assert.equal(ui.get('source').value, 'lab');
  assert.equal(ui.sources.find((source) => source.id === 'lab').label, 'lab');
  await ui.load();
  assert.equal(ui.get('source-branch').value, 'lab/builds');
  ui.get('source-branch').value = 'lab/revised';
  ui.get('source-branch').handlers.input();
  await ui.click('source-save');
  assert.equal(ui.sources.find((source) => source.id === 'lab').branch, 'lab/revised');
  await ui.click('source-remove');
  assert.ok(!ui.sources.some((source) => source.id === 'lab'));
  assert.equal(ui.get('source').value, 'default');
});

test('literal secret values are rejected before source save', async () => {
  const ui = fixture();
  await ui.load();
  ui.get('source-credential').value = 'github_pat_do_not_send';
  ui.get('source-credential').handlers.input();
  await ui.click('source-save');
  assert.equal(ui.calls.length, 1);
  assert.match(ui.get('status').textContent, /never a secret value/);
});

test('editing the tag clears stale selections before installation', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  ui.get('tag').value = `plugins-${'c'.repeat(40)}`;
  ui.get('tag').handlers.input();
  assert.equal(ui.get('install').disabled, true);
  await ui.click('install');
  assert.equal(ui.calls.length, 2);
});

test('workspace synchronization failure reports the successful local installation', async () => {
  const ui = fixture({ status: 502, data: { local_installed: true, error: 'upstream unavailable' } });
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  await ui.click('install');
  assert.match(ui.get('status').textContent, /Installed locally, but server synchronization failed/);
  assert.equal(ui.refreshed(), 1);
});

test('failed browsing leaves no installable release', async () => {
  const ui = fixture(undefined, { status: 503, data: { error: 'GitHub credentials unavailable' } });
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  assert.equal(ui.get('install').disabled, true);
  assert.equal(ui.get('browse').disabled, false);
  assert.match(ui.get('status').textContent, /credentials unavailable/);
});

test('a release without a configuration revision cannot be installed', async () => {
  const ui = fixture(undefined, { status: 200, data: { tag, source: 'default', plugins: [{ name: 'example.dll', size: 5, sha256: digest }] } });
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  assert.equal(ui.get('install').disabled, true);
  assert.match(ui.get('status').textContent, /invalid plugin release/);
});


test('plugins start unchecked and selection can be cleared', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('browse');
  assert.equal(ui.get('install').disabled, true);
  await ui.click('select-all');
  assert.equal(ui.get('install').disabled, false);
  await ui.click('select-none');
  assert.equal(ui.get('install').disabled, true);
  await ui.click('install');
  assert.equal(ui.calls.length, 2);
});

test('batch install pins each plugin and keeps only failures selected for retry', async () => {
  let fail = true;
  const ui = fixture((body) => ({ status: body.name === 'second.dll' && fail ? 503 : 200,
    data: { error: 'download failed' } }), { status: 200, data: {
      tag, source: 'default', source_revision: revision, version: '1.2',
      plugins: [{ name: 'first.dll', size: 5, sha256: digest }, { name: 'second.dll', size: 7, sha256: 'd'.repeat(64) }],
    } });
  await ui.load();
  await ui.click('browse');
  await ui.click('select-all');
  await ui.click('install');
  const installs = ui.calls.filter((call) => call.url === '/plugin-releases/install');
  assert.equal(installs.length, 2);
  assert.equal(installs[1].body.sha256, 'd'.repeat(64));
  assert.equal(installs[1].body.source_revision, revision);
  assert.equal(ui.get('name').children[0].children[0].checked, false);
  assert.equal(ui.get('name').children[1].children[0].checked, true);
  assert.match(ui.get('status').textContent, /1 of 2 installed/);
  fail = false;
  await ui.click('install');
  assert.equal(ui.calls.at(-1).body.name, 'second.dll');
  assert.equal(ui.get('install').disabled, true);
});


test('saving confirms and selects the persisted source without a second GET', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('source-new');
  assert.equal(ui.get('source-repository').value, 'CyrilleFranchet/Minimal-RMM-plugins');
  ui.get('source-id').value = 'stable';
  ui.failRefresh();
  await ui.click('source-save');
  assert.equal(ui.get('source-editor').open, false);
  assert.equal(ui.get('source').value, 'stable');
  assert.match(ui.get('status').textContent, /Saved source/);
  assert.equal(ui.calls.filter((call) => call.url === '/plugin-sources').length, 1);
  await ui.load();
  assert.equal(ui.get('source').value, 'stable');
  assert.match(ui.get('status').textContent, /Could not refresh/);
});

test('source dialog keeps storage errors visible and cancel restores selection', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('source-new');
  ui.get('source-id').value = 'new';
  ui.failSave();
  await ui.click('source-save');
  assert.equal(ui.get('source-editor').open, true);
  assert.match(ui.get('source-status').textContent, /storage failed/);
  assert.equal(ui.get('source-id').value, 'new');
  await ui.click('source-cancel');
  assert.equal(ui.get('source').value, 'default');
  assert.equal(ui.get('source-editor').open, false);
});

test('new source refuses duplicate IDs and editing locks the saved ID', async () => {
  const ui = fixture();
  await ui.load();
  await ui.click('source-new');
  ui.get('source-id').value = 'default';
  await ui.click('source-save');
  assert.match(ui.get('source-status').textContent, /already exists/);
  assert.equal(ui.calls.length, 1);
  await ui.click('source-cancel');
  await ui.click('source-edit');
  assert.equal(ui.get('source-editor').open, true);
  assert.equal(ui.get('source-id').readOnly, true);
  assert.equal(ui.get('source-repository').value, 'owner/plugins');
});


test('an empty store and a failed fetch have different visible source states', async () => {
  const empty = fixture(undefined, undefined, { status: 200, data: { sources: [] } });
  await empty.load();
  assert.match(empty.get('source-list-status').textContent, /No sources are saved/);
  assert.match(empty.get('source').children[0].textContent, /No saved sources/);
  assert.equal(empty.get('source-edit').disabled, true);
  const failed = fixture(undefined, undefined, { status: 409, data: { detail: 'Use the local workspace API URL.' } });
  await failed.load();
  assert.match(failed.get('source-list-status').textContent, /Use the local workspace API URL/);
  assert.match(failed.get('source').children[0].textContent, /Sources unavailable/);
});

test('malformed source responses are errors rather than empty stores', async () => {
  const ui = fixture(undefined, undefined, { status: 200, data: {} });
  await ui.load();
  assert.match(ui.get('source-list-status').textContent, /invalid source list/);
  assert.equal(ui.get('browse').disabled, true);
});

test('opening Config reloads sources independently and collapses sections', () => {
  const handlers = {};
  const sections = [{ open: true }, { open: true }];
  let refreshes = 0;
  const dialog = { showModal() {}, querySelectorAll() { return sections; } };
  const button = { addEventListener(name, handler) { handlers[name] = handler; } };
  const document = { querySelector(selector) {
    return selector === '#ui-config-dialog' ? dialog : selector === '#config-toggle-btn' ? button : null;
  } };
  const window = { refreshPluginSources() { refreshes++; }, addEventListener(name, callback) { callback(); } };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/config.js'), 'utf8'), { window, document });
  handlers.click();
  assert.equal(refreshes, 1);
  assert.ok(sections.every((section) => !section.open));
  handlers.click();
  assert.equal(refreshes, 2);
});

test('deployment badge distinguishes standalone, central, workspace, and unknown services', () => {
  const badge = {};
  const hint = {};
  const window = { addEventListener() {} };
  const document = { querySelector(selector) {
    return selector === '#deployment-mode' ? badge : selector === '#deployment-mode-description' ? hint : null;
  } };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/config.js'), 'utf8'), { window, document });
  window.renderRmmDeploymentMode({ storage_mode: 'server' });
  assert.equal(badge.textContent, 'Standalone');
  window.renderRmmDeploymentMode({ storage_mode: 'workspace' });
  assert.equal(badge.textContent, 'Central · workspace mode');
  assert.match(hint.textContent, /Open your operator workspace UI/);
  window.renderRmmDeploymentMode({ workspace_id: 'alice' });
  assert.equal(badge.textContent, 'Workspace: alice');
  window.renderRmmDeploymentMode();
  assert.equal(badge.textContent, 'Mode unknown');
});

/* Service settings controller tests: owner scope, typed patches, and secret handling. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.join(__dirname, '..');

function fixture(options = {}) {
  class Element {
    constructor(tag = '') { this.tag = tag; this.children = []; this.handlers = {}; this.value = ''; this.checked = false; this.classList = { toggle() {} }; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    setAttribute(name, value) { this[name] = value; }
  }
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="(service-settings-[^"]+|config-toggle-btn)"/g)].map((match) => [match[1], new Element()]));
  const calls = []; let ready;
  let snapshot = structuredClone(options.snapshot || { scope: 'workspace', restart_required: false, settings: [
    { key: 'RMM_ASSET_MAX_BYTES', label: 'Asset limit', group: 'Limits', type: 'integer', secret: false, value: 1024, saved_value: null, source: 'default', restart_required: false },
    { key: 'RMM_AI_USE_MCP', label: 'MCP', group: 'AI', type: 'boolean', secret: false, value: true, saved_value: null, source: 'default', restart_required: false },
    { key: 'RMM_PLUGIN_GITHUB_TOKEN', label: 'GitHub token', group: 'Credentials', type: 'string', secret: true, configured: true, value: null, saved_value: null, source: 'environment', restart_required: false },
    { key: 'RMM_WORKSPACES', label: 'Workspace tokens', group: 'Credentials', type: 'json', secret: true, configured: false, value: null, saved_value: null, source: 'default', restart_required: false },
  ] });
  const request = async (url, args) => {
    const body = args.body ? JSON.parse(args.body) : null; calls.push({ url, method: args.method, body });
    if (options.fail) return { status: 503, data: { error: 'settings_unavailable' } };
    if (args.method === 'PATCH') snapshot.restart_required = true;
    return { status: 200, data: structuredClone(snapshot) };
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/settings.js'), 'utf8'), {
    document: { getElementById: (id) => { assert.ok(elements.has(id), id); return elements.get(id); }, createElement: (tag) => new Element(tag),
      addEventListener: (_, callback) => { ready = callback; } }, window: { rmmApi: request }, console,
  });
  ready();
  const get = (id) => elements.get(`service-settings-${id}`);
  const click = async (id) => { const element = id === 'open' ? elements.get('config-toggle-btn') : get(id); await element.handlers.click(); };
  const row = (key) => {
    for (const group of get('fields').children) for (const child of group.children) {
      if (child.children?.[1]?.id === `service-setting-${key}`) return { input: child.children[1], help: child.children[2], reset: child.children[3].children[0] };
    }
    throw new Error(`Missing setting ${key}`);
  };
  return { get, click, row, calls, options };
}

test('loads owner scope and sends only typed changes with blank secrets untouched', async () => {
  const ui = fixture(); await ui.click('open');
  assert.match(ui.get('scope').textContent, /workspace/);
  assert.equal(ui.row('RMM_PLUGIN_GITHUB_TOKEN').input.type, 'password');
  assert.equal(ui.row('RMM_PLUGIN_GITHUB_TOKEN').input.value, '');
  await ui.click('save'); assert.equal(ui.calls.length, 1);
  ui.row('RMM_ASSET_MAX_BYTES').input.value = '2048';
  ui.row('RMM_AI_USE_MCP').input.checked = false;
  await ui.click('save');
  assert.deepEqual(ui.calls.at(-1).body, { settings: { RMM_ASSET_MAX_BYTES: 2048, RMM_AI_USE_MCP: false } });
  assert.match(ui.get('status').textContent, /Restart/);
});

test('secret replacement and JSON credentials are write-only; reset sends null', async () => {
  const ui = fixture(); await ui.click('open');
  ui.row('RMM_PLUGIN_GITHUB_TOKEN').input.value = 'new-secret';
  ui.row('RMM_WORKSPACES').input.value = '{"alice":"token"}';
  await ui.click('save');
  assert.deepEqual(ui.calls.at(-1).body, { settings: { RMM_PLUGIN_GITHUB_TOKEN: 'new-secret', RMM_WORKSPACES: { alice: 'token' } } });
  assert.equal(ui.row('RMM_PLUGIN_GITHUB_TOKEN').input.value, '');
  assert.equal(ui.row('RMM_WORKSPACES').input.value, '');
  ui.row('RMM_PLUGIN_GITHUB_TOKEN').reset.checked = true;
  ui.row('RMM_PLUGIN_GITHUB_TOKEN').reset.handlers.change();
  assert.equal(ui.row('RMM_PLUGIN_GITHUB_TOKEN').input.disabled, true);
  await ui.click('save'); assert.deepEqual(ui.calls.at(-1).body, { settings: { RMM_PLUGIN_GITHUB_TOKEN: null } });
});

test('failed load disables saving, retry works, and invalid input is never sent', async () => {
  const ui = fixture({ fail: true }); await ui.click('open');
  assert.equal(ui.get('save').disabled, true); assert.match(ui.get('status').textContent, /settings_unavailable/);
  ui.options.fail = false; await ui.click('reload'); assert.equal(ui.get('save').disabled, false);
  ui.row('RMM_WORKSPACES').input.value = '{secret'; await ui.click('save');
  assert.match(ui.get('status').textContent, /invalid JSON/);
  assert.equal(ui.calls.filter((call) => call.method === 'PATCH').length, 0);
  assert.equal(ui.get('save').disabled, false);
});

test('failed save retains edits; named credential save clears the password', async () => {
  const ui = fixture(); await ui.click('open');
  ui.row('RMM_ASSET_MAX_BYTES').input.value = '2048'; ui.options.fail = true; await ui.click('save');
  assert.equal(ui.row('RMM_ASSET_MAX_BYTES').input.value, '2048'); assert.equal(ui.get('save').disabled, false);
  ui.options.fail = false;
  ui.get('credential-key').value = 'RMM_PLUGIN_TOKEN_TEAM'; ui.get('credential-value').value = 'team-secret';
  await ui.click('add-credential');
  assert.match(ui.get('status').textContent, /Save or reload/);
  assert.equal(ui.row('RMM_ASSET_MAX_BYTES').input.value, '2048');
  await ui.click('reload'); await ui.click('add-credential');
  assert.deepEqual(ui.calls.at(-1).body, { settings: { RMM_PLUGIN_TOKEN_TEAM: 'team-secret' } });
  assert.equal(ui.get('credential-value').value, '');
});

test('saved pending values are editable while effective environment values stay visible', async () => {
  const ui = fixture({ snapshot: { scope: 'server', restart_required: true, settings: [
    { key: 'MODE', label: 'Tunnel mode', group: 'Transport', type: 'string', secret: false, value: 'quick', saved_value: 'named', source: 'environment', saved: true, restart_required: true, choices: ['quick', 'named'] },
  ] } });
  await ui.click('open');
  assert.equal(ui.row('MODE').input.tag, 'select'); assert.equal(ui.row('MODE').input.value, 'named');
  assert.match(ui.row('MODE').help.textContent, /Source: environment.*Effective value: quick/);
  await ui.click('save'); assert.equal(ui.calls.filter((call) => call.method === 'PATCH').length, 0);
});

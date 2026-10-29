/* Go generator transport freshness and credential failure regression tests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function fixture(options = {}) {
  class Element {
    constructor() {
      this.value = ''; this.textContent = ''; this.handlers = {}; this.dataset = {};
      this.classes = new Set(); this.disabled = false;
      this.classList = { add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name),
        toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name) };
    }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  }
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [`#${match[1]}`, new Element()]));
  const get = (id) => elements.get(`#agent-go-${id}`);
  get('server-url').value = options.selected || 'https://current.trycloudflare.com';
  get('beacon-secret').value = 'private-beacon-secret';
  const storage = new Map([['rmm_api_token', 'operator-token']]);
  if (options.prefs) storage.set('rmm_go_agent_prefs', JSON.stringify(options.prefs));
  const calls = [];
  const transport = () => ({ beacon_url: options.current || 'https://current.trycloudflare.com' });
  const api = async (url) => {
    calls.push({ url });
    if (options.transportFailure) return { status: 503, data: { error: 'unavailable' } };
    return { status: 200, data: transport() };
  };
  const fetch = async (url, args) => {
    calls.push({ url, body: args.body ? JSON.parse(args.body) : undefined });
    if (url === '/api/v1/transport') {
      return { ok: !options.transportFailure, json: async () => transport() };
    }
    assert.equal(url, '/api/v1/agent/go/build');
    return { ok: !options.buildFailure, status: options.buildFailure ? 400 : 200,
      json: async () => options.buildFailure || { download_url: '/api/v1/agent/go/download/test.zip', filename: 'test.zip', size: 100, sha256: 'digest' } };
  };
  const window = { location: { origin: 'http://admin.example:8080' }, rmmBeaconUrl: 'https://stale-cache.trycloudflare.com',
    addEventListener() {}, persistRmmUiConfig() {} };
  if (!options.fetchFallback) window.rmmApi = api;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/go-agent.js'), 'utf8'), {
    document: { readyState: 'complete', querySelector: (selector) => elements.get(selector) },
    window, fetch, URL, console,
    sessionStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
  });
  return { get, options, calls, storage, window, click: async (id) => get(id).handlers.click() };
}

test('stale saved Quick Tunnel URL blocks compilation without replacing operator input', async () => {
  const ui = fixture({ prefs: { serverUrl: 'https://old.trycloudflare.com', beaconSecret: 'private-beacon-secret' } });
  await ui.click('build');
  assert.match(ui.get('status').textContent, /Quick Tunnel URL is no longer current/);
  assert.equal(ui.get('server-url').value, 'https://old.trycloudflare.com');
  assert.equal(ui.calls.filter((call) => call.url.endsWith('/build')).length, 0);
  assert.equal(ui.get('build').disabled, false);
});

test('current Quick Tunnel and intentional custom or named routes remain buildable', async () => {
  for (const selected of ['https://current.trycloudflare.com/', 'https://named.example/beacon', 'http://10.0.0.1:8081']) {
    const ui = fixture({ selected });
    await ui.click('build');
    const builds = ui.calls.filter((call) => call.url.endsWith('/build'));
    assert.equal(builds.length, 1, selected);
    assert.equal(builds[0].body.config.base_url, selected.replace(/\/$/, ''));
    assert.match(ui.get('status').textContent, /URL and beacon secret are embedded/);
    assert.match(ui.get('status').textContent, /launcher to override stale environment/);
    assert.doesNotMatch(ui.get('status').textContent, /private-beacon-secret/);
  }
});

test('current beacon button refreshes transport instead of using cached URL or administration origin', async () => {
  const ui = fixture({ selected: 'https://old.trycloudflare.com', current: 'https://new.trycloudflare.com', fetchFallback: true });
  await ui.click('use-origin');
  assert.equal(ui.get('server-url').value, 'https://new.trycloudflare.com');
  assert.equal(ui.calls[0].url, '/api/v1/transport');
  assert.equal(JSON.parse(ui.storage.get('rmm_go_agent_prefs')).serverUrl, 'https://new.trycloudflare.com');
  assert.equal(ui.get('use-origin').disabled, false);
});

test('transport failure preserves edits and gives an actionable error before build or URL selection', async () => {
  const ui = fixture({ selected: 'https://chosen.example', transportFailure: true });
  await ui.click('build');
  assert.match(ui.get('status').textContent, /Unable to verify.*retry/);
  assert.equal(ui.calls.filter((call) => call.url.endsWith('/build')).length, 0);
  await ui.click('use-origin');
  assert.equal(ui.get('server-url').value, 'https://chosen.example');
  assert.equal(ui.get('beacon-secret').value, 'private-beacon-secret');
  assert.equal(ui.get('use-origin').disabled, false);
});

test('server beacon-secret mismatch is surfaced without echoing the entered credential', async () => {
  const ui = fixture({ buildFailure: { error: 'beacon_secret_mismatch', detail: 'The beacon secret does not match the active server configuration.' } });
  await ui.click('build');
  assert.match(ui.get('status').textContent, /does not match the active server configuration/);
  assert.doesNotMatch(ui.get('status').textContent, /private-beacon-secret/);
  assert.equal(ui.get('beacon-secret').value, 'private-beacon-secret');
  assert.equal(ui.get('build').disabled, false);
});

/* Go generator transport freshness and credential failure regression tests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

test('Go persistence defaults off and opt-in survives preferences and build submission', async () => {
  const defaults = fixture();
  await defaults.click('build');
  assert.equal(defaults.calls.find(call => call.url.endsWith('/build')).body.config.persistent_http, false);
  const ui = fixture({prefs: {persistentHttp: true}});
  assert.equal(ui.get('persistent-http').checked, true);
  await ui.click('build');
  assert.equal(ui.calls.find(call => call.url.endsWith('/build')).body.config.persistent_http, true);
  ui.get('persistent-http').checked = false;
  ui.get('persistent-http').handlers.change();
  assert.equal(JSON.parse(ui.storage.get('rmm_go_agent_prefs')).persistentHttp, false);
});

test('TLS selection persists and reaches build request', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<select id="agent-go-tls-version" aria-describedby="agent-go-tls-hint">/);
  assert.match(html, /id="agent-go-tls-hint">The staged launcher uses its own TLS settings/);
  const defaults = fixture();
  await defaults.click('build');
  assert.equal(defaults.calls.find(call => call.url.endsWith('/build')).body.config.tls_version, 'default');
  const ui = fixture({prefs: {tlsVersion: '1.2'}});
  assert.equal(ui.get('tls-version').value, '1.2');
  await ui.click('build');
  assert.equal(ui.calls.find(call => call.url.endsWith('/build')).body.config.tls_version, '1.2');
  ui.get('tls-version').value = '1.3';
  ui.get('tls-version').handlers.change();
  assert.equal(JSON.parse(ui.storage.get('rmm_go_agent_prefs')).tlsVersion, '1.3');
  const tls13 = fixture({prefs: {tlsVersion: '1.3'}});
  await tls13.click('build');
  assert.equal(tls13.calls.find(call => call.url.endsWith('/build')).body.config.tls_version, '1.3');
});

test('Go generator exposes the debug build checkbox and inherited tracing label', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /<label for="agent-go-debug"[^>]*>[\s\S]*?<input type="checkbox" id="agent-go-debug"> Debug build \(child agents inherit\)/);
});

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
  const downloads = [];
  const cleanupTimers = [];
  const objectUrls = new Set();
  class TestURL extends URL {
    static createObjectURL() { const url = `blob:test-${objectUrls.size}`; objectUrls.add(url); return url; }
    static revokeObjectURL(url) { objectUrls.delete(url); }
  }
  const transport = () => ({ beacon_url: options.current || 'https://current.trycloudflare.com' });
  const api = async (url) => {
    calls.push({ url });
    if (url === '/health') {
      if (options.healthGate) await options.healthGate;
      return {status: 200, data: {workspace_id: options.workspaceId || null}};
    }
    if (options.transportGate) await options.transportGate;
    if (options.transportFailure) return { status: 503, data: { error: 'unavailable' } };
    return { status: 200, data: transport() };
  };
  const fetch = async (url, args) => {
    calls.push({ url, headers: args.headers, body: args.body ? JSON.parse(args.body) : undefined });
    if (url === '/api/v1/agent/go/download/test.zip') {
      if (options.networkFailure) throw new Error('Network unavailable');
      return { ok: !options.downloadFailure, status: options.downloadFailure ? 503 : 200, blob: async () => {
        if (options.downloadGate) await options.downloadGate;
        if (options.blobFailure) throw new Error('ZIP response interrupted');
        return new Blob(['zip']);
      } };
    }
    if (url === '/api/v1/health') {
      return {ok: true, json: async () => ({workspace_id: options.workspaceId || null})};
    }
    if (url === '/api/v1/transport') {
      return { ok: !options.transportFailure, json: async () => transport() };
    }
    assert.equal(url, '/api/v1/agent/go/build');
    return { ok: !options.buildFailure, status: options.buildFailure ? 400 : 200,
      json: async () => {
        if (options.buildGate) await options.buildGate;
        return options.buildFailure || { download_url: '/api/v1/agent/go/download/test.zip', filename: 'test.zip', size: 100, sha256: 'digest' };
      } };
  };
  const window = { location: { origin: 'http://admin.example:8080' }, rmmBeaconUrl: 'https://stale-cache.trycloudflare.com',
    rmmState: { authGeneration: 1 }, addEventListener() {}, persistRmmUiConfig() {} };
  if (!options.fetchFallback) window.rmmApi = api;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/go-agent.js'), 'utf8'), {
    document: { readyState: 'complete', querySelector: (selector) => elements.get(selector),
      body: { appendChild() {} },
      createElement: () => { const anchor = { click() {
        assert.ok(objectUrls.has(anchor.href));
        if (options.clickFailure) throw new Error('Browser download failed');
        downloads.push(anchor.download);
      }, remove() {} }; return anchor; } },
    window, fetch, URL: TestURL, Blob, console,
    setTimeout: (callback, delay) => cleanupTimers.push({callback, delay}),
    sessionStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
  });
  return { get, options, calls, downloads, cleanupTimers, objectUrls, storage, window, click: async (id) => get(id).handlers.click() };
}

test('debug generation is explicit and requests a full-agent debug build', async () => {
  for (const debug of [false, true]) {
    const ui = fixture();
    ui.get('debug').checked = debug;
    await ui.click('build');
    const request = ui.calls.find(call => call.url.endsWith('/build'));
    assert.equal(request.body.debug, debug);
    assert.equal(request.body.stageless, debug);
  }
});

test('saved debug build preference survives reload and can be disabled for the next build', async () => {
  const ui = fixture({prefs: {debug: true}});
  assert.equal(ui.get('debug').checked, true);
  await ui.click('build');
  let builds = ui.calls.filter(call => call.url.endsWith('/build'));
  assert.equal(builds[0].body.debug, true);
  assert.equal(builds[0].body.stageless, true);
  ui.get('debug').checked = false;
  ui.get('debug').handlers.change();
  assert.equal(JSON.parse(ui.storage.get('rmm_go_agent_prefs')).debug, false);
  await ui.click('build');
  builds = ui.calls.filter(call => call.url.endsWith('/build'));
  assert.equal(builds[1].body.debug, false);
  assert.equal(builds[1].body.stageless, false);
});

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

test('zero jitter survives preferences and the actual build payload', async () => {
  const ui = fixture();
  ui.get('jitter').value = '0';
  await ui.click('build');
  assert.equal(JSON.parse(ui.storage.get('rmm_go_agent_prefs')).jitterPercent, 0);
  assert.equal(ui.calls.find((call) => call.url.endsWith('/build')).body.config.jitter_percent, 0);
});

test('connection change during transport lookup prevents old configuration submission', async () => {
  let release;
  const ui = fixture({ transportGate: new Promise((resolve) => { release = resolve; }) });
  const pending = ui.click('build');
  ui.window.rmmState.authGeneration++;
  ui.storage.set('rmm_api_token', 'new-operator-token');
  ui.get('status').textContent = 'New connection';
  release();
  await pending;
  assert.equal(ui.calls.filter((call) => call.url.endsWith('/build')).length, 0);
  assert.equal(ui.get('status').textContent, 'New connection');
  assert.equal(ui.window.rmmBeaconUrl, 'https://stale-cache.trycloudflare.com');
});

test('same-token reconnect suppresses an old build response during JSON consumption', async () => {
  let release;
  const ui = fixture({ buildGate: new Promise((resolve) => { release = resolve; }) });
  const pending = ui.click('build');
  while (!ui.calls.some((call) => call.url.endsWith('/build'))) await Promise.resolve();
  ui.window.rmmState.authGeneration++;
  ui.get('status').textContent = 'New connection';
  release();
  await pending;
  assert.equal(ui.get('download').dataset.downloadUrl, undefined);
  assert.equal(ui.get('status').textContent, 'New connection');
});

test('a download link from an earlier connection cannot use the new token', async () => {
  const ui = fixture();
  await ui.click('build');
  const before = ui.calls.length;
  ui.window.rmmState.authGeneration++;
  ui.storage.set('rmm_api_token', 'new-operator-token');
  await ui.get('download').handlers.click({ preventDefault() {}, currentTarget: ui.get('download') });
  assert.equal(ui.calls.length, before);
});


test('current workspace overrides saved or manually entered owner before building', async () => {
  const ui = fixture({workspaceId: 'bob', prefs: {workspaceId: 'alice'}});
  ui.get('workspace-id').value = 'alice';
  await ui.click('build');
  const build = ui.calls.find(call => call.url.endsWith('/build'));
  assert.equal(build.body.config.workspace_id, 'bob');
  assert.equal(ui.get('workspace-id').value, 'bob');
});

test('administrator-selected workspace is included with a Windows-only build', async () => {
  const ui = fixture();
  ui.get('workspace-id').value = 'alice';
  await ui.click('build');
  const build = ui.calls.find(call => call.url.endsWith('/build'));
  assert.equal(build.body.config.workspace_id, 'alice');
  assert.equal(build.body.goos, 'windows');
});


test('Download agent compiles and downloads the ZIP with authentication in one click', async () => {
  const ui = fixture();
  await ui.click('build');
  assert.deepEqual(ui.downloads, ['test.zip']);
  const requests = ui.calls.filter(call => call.url.includes('/agent/go/'));
  assert.deepEqual(requests.map(call => call.url), ['/api/v1/agent/go/build', '/api/v1/agent/go/download/test.zip']);
  assert.equal(requests[1].headers.Authorization, 'Bearer operator-token');
  assert.match(ui.get('status').textContent, /Download started/);
  assert.equal(ui.get('build').disabled, false);
  assert.match(fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8'), /id="agent-go-build">Download agent/);
});

test('failed download can be retried without compiling again', async () => {
  const ui = fixture({downloadFailure: true});
  await ui.click('build');
  assert.equal(ui.downloads.length, 0);
  assert.match(ui.get('status').textContent, /Download failed.*retry without compiling again/);
  assert.equal(ui.get('download').classes.has('hidden'), false);
  assert.equal(ui.get('build').disabled, false);
  ui.options.downloadFailure = false;
  await ui.get('download').handlers.click({preventDefault() {}, currentTarget: ui.get('download')});
  assert.deepEqual(ui.downloads, ['test.zip']);
  assert.match(ui.get('status').textContent, /launcher to override stale environment/);
  assert.equal(ui.calls.filter(call => call.url.endsWith('/build')).length, 1);
});

test('compile failure never starts a download', async () => {
  const ui = fixture({buildFailure: {error: 'Compiler unavailable'}});
  await ui.click('build');
  assert.equal(ui.calls.filter(call => call.url.includes('/download/')).length, 0);
  assert.equal(ui.downloads.length, 0);
});

test('connection change during ZIP fetch suppresses the old download and status', async () => {
  let release;
  const ui = fixture({downloadGate: new Promise(resolve => { release = resolve; })});
  const pending = ui.click('build');
  while (!ui.calls.some(call => call.url.includes('/download/'))) await Promise.resolve();
  assert.equal(ui.get('build').disabled, true);
  await ui.click('build');
  assert.equal(ui.calls.filter(call => call.url.endsWith('/build')).length, 1);
  ui.window.rmmState.authGeneration++;
  ui.get('status').textContent = 'New connection';
  release();
  await pending;
  assert.equal(ui.downloads.length, 0);
  assert.equal(ui.get('status').textContent, 'New connection');
  assert.equal(ui.get('build').disabled, false);
});


test('manual retries cannot overlap downloads or start another compilation', async () => {
  const ui = fixture();
  await ui.click('build');
  let release;
  ui.options.downloadGate = new Promise(resolve => { release = resolve; });
  const retry = () => ui.get('download').handlers.click({preventDefault() {}});
  const pending = retry();
  assert.equal(ui.get('build').disabled, true);
  await retry();
  await ui.click('build');
  assert.equal(ui.calls.filter(call => call.url.endsWith('/build')).length, 1);
  assert.equal(ui.calls.filter(call => call.url.includes('/download/')).length, 2);
  release();
  await pending;
  assert.equal(ui.get('build').disabled, false);
  assert.deepEqual(ui.downloads, ['test.zip', 'test.zip']);
});

test('blob URLs are released after the browser has time to start the download', async () => {
  for (const clickFailure of [false, true]) {
    const ui = fixture({clickFailure});
    await ui.click('build');
    assert.equal(ui.objectUrls.size, 1);
    assert.equal(ui.cleanupTimers.length, 1);
    assert.equal(ui.cleanupTimers[0].delay, 60000);
    ui.cleanupTimers[0].callback();
    assert.equal(ui.objectUrls.size, 0);
    assert.equal(ui.get('build').disabled, false);
    if (clickFailure) assert.match(ui.get('status').textContent, /Browser download failed.*retry/);
  }
});


test('network and interrupted ZIP failures release the busy state and allow retry', async () => {
  for (const failure of ['networkFailure', 'blobFailure']) {
    const ui = fixture({[failure]: true});
    await ui.click('build');
    assert.equal(ui.get('build').disabled, false);
    assert.match(ui.get('status').textContent, /retry without compiling again/);
    assert.equal(ui.downloads.length, 0);
    ui.options[failure] = false;
    await ui.get('download').handlers.click({preventDefault() {}});
    assert.deepEqual(ui.downloads, ['test.zip']);
    assert.equal(ui.calls.filter(call => call.url.endsWith('/build')).length, 1);
  }
});

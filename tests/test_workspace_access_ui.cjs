const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('central administration never polls local BloodHound and workspace access restores controls', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = source.indexOf('function configureWorkspaceAccess(');
  const end = source.indexOf('\nasync function connect()', start);
  assert.ok(start > 0 && end > start);
  const hidden = new Map();
  let starts = 0;
  let stops = 0;
  const context = { document: { getElementById: id => ({ classList: {
    toggle: (_name, value) => hidden.set(id, value),
  } }) }, window: {
    startBloodHoundHealth: () => starts++, stopBloodHoundHealth: () => stops++,
  } };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  context.configureWorkspaceAccess({ storage_mode: 'workspace' });
  assert.equal(starts, 0);
  assert.equal(stops, 1);
  assert.equal(hidden.get('bloodhound-open'), true);
  assert.equal(hidden.get('ai-toggle-btn'), true);
  assert.equal(hidden.get('workspace-access-notice'), false);
  context.configureWorkspaceAccess({ workspace_id: 'default' });
  assert.equal(starts, 1);
  assert.equal(hidden.get('bloodhound-open'), false);
  assert.equal(hidden.get('workspace-access-notice'), true);
});

test('workspace dot reflects HTTP reachability without claiming a WebSocket connection', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = source.indexOf('function setWsStatus(');
  const end = source.indexOf('\nfunction wsUrl()', start);
  const dot = { classList: { toggle: (_name, value) => { dot.connected = value; } } };
  const context = { WORKSPACE_MODE: true, state: { workspaceReachable: true }, $: () => dot };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  context.setWsStatus(false);
  assert.equal(dot.connected, true);
  assert.equal(context.state.wsConnected, false);
  assert.match(dot.title, /HTTP polling/);
  context.state.workspaceReachable = false;
  context.setWsStatus(false);
  assert.equal(dot.connected, false);
  context.WORKSPACE_MODE = false;
  context.setWsStatus(true);
  assert.equal(dot.connected, true);
  assert.equal(dot.title, 'WebSocket connected');
});

test('canceled and stale HTTP requests do not change workspace connectivity', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = source.indexOf('async function api(');
  const end = source.indexOf('\nasync function probeServerHealth()', start);
  let finish;
  let unreachable = 0;
  let reachable = 0;
  const context = {
    WORKSPACE_MODE: true, state: { workspaceReachable: true, connectionGeneration: 0 },
    fetch: () => new Promise(resolve => { finish = resolve; }),
    noteServerReachable: () => reachable++, noteServerUnreachable: () => unreachable++,
    setWsStatus: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  const pending = context.api('/health');
  context.state.connectionGeneration++;
  context.state.workspaceReachable = false;
  finish({ ok: true, status: 200, text: async () => '{}' });
  await pending;
  assert.equal(context.state.workspaceReachable, false);
  assert.equal(reachable, 0);
  context.state.workspaceReachable = true;
  context.fetch = async () => { throw Object.assign(new Error('canceled'), { name: 'AbortError' }); };
  await context.api('/health', { signal: { aborted: true } });
  assert.equal(context.state.workspaceReachable, true);
  assert.equal(unreachable, 0);
  context.fetch = async () => { throw new Error('offline'); };
  await context.api('/health');
  assert.equal(context.state.workspaceReachable, false);
  assert.equal(unreachable, 1);
  context.fetch = async () => ({ ok: true, status: 200, text: async () => '{}' });
  await context.api('/health');
  assert.equal(context.state.workspaceReachable, true);
  assert.equal(reachable, 1);
});

test('responses from a previous login never expose old payloads or disconnect a new login', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = source.indexOf('async function api(');
  const end = source.indexOf('\nasync function probeServerHealth()', start);
  let finish;
  let reachable = 0;
  const context = {
    WORKSPACE_MODE: true, state: { token: 'old-token', authGeneration: 1 },
    fetch: () => new Promise(resolve => { finish = resolve; }),
    noteServerReachable: () => reachable++, noteServerUnreachable: () => {}, setWsStatus: () => {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  for (const status of [200, 401]) {
    const pending = context.api('/sessions');
    context.state.authGeneration++;
    context.state.token = 'new-token';
    finish({ ok: status === 200, status, text: async () => '{"sessions":["old-owner"]}' });
    const result = await pending;
    assert.equal(result.status, 0);
    assert.equal(result.data.error, 'stale_connection_response');
    assert.equal(result.data.sessions, undefined);
  }
  assert.equal(reachable, 0);
  let finishBody;
  let bodyStarted;
  const readingBody = new Promise(resolve => { bodyStarted = resolve; });
  context.fetch = async () => ({ ok: true, status: 200,
    text: () => new Promise(resolve => { finishBody = resolve; bodyStarted(); }) });
  const pendingBody = context.api('/history');
  await readingBody;
  context.state.authGeneration++;
  finishBody('{"sessions":["old-owner"]}');
  const result = await pendingBody;
  assert.equal(result.status, 0);
  assert.equal(result.data.sessions, undefined);
});

test('agent workspace tabs follow selection and do not restore an archive after reconnect', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  function element(dataset = {}) {
    const classes = new Set();
    return { dataset, textContent: '', classes, classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
    }, setAttribute() {} };
  }
  const tabs = element();
  const buttons = ['shell', 'files', 'pivots'].map(name => element({ workspaceTab: name }));
  const panels = ['shell', 'files', 'pivots'].map(name => element({ workspacePanel: name }));
  const nodes = new Map(['#empty-state', '#files-selected-session', '#console-header', '#login', '#app'].map(id => [id, element()]));
  const refreshes = [];
  const noop = () => {};
  const context = {
    state: { workspaceTab: 'pivots', selectedId: null, selectedHistoryId: null, sessions: [],
      selectionGeneration: 5, authGeneration: 1, token: 'old-token', echoedCommands: new Set() },
    document: { querySelector: () => tabs, querySelectorAll: selector => selector === '.workspace-tab' ? buttons : panels },
    $: id => nodes.get(id), show: node => node.classList.remove('hidden'), hide: node => node.classList.add('hidden'),
    refreshExfilStatus: async () => refreshes.push('files'), refreshSocksRelays: async () => refreshes.push('socks'),
    refreshTunRelays: async () => refreshes.push('tun'), prepareTunControls: noop, updateSocksUi: noop, updateTunUi: noop,
    window: {}, sessionStorage: { removeItem: noop }, STORAGE_KEY: 'token', hideServerUnreachableDialog: noop,
    disconnectWebSocket: noop, stopEventPolling: noop, stopSessionPolling: noop, stopStatusTick: noop,
    stopConnectivityProbe: noop, showLogin: noop,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function setWorkspaceTab('), source.indexOf('\nfunction selectedSocksRelay(')) +
    source.slice(source.indexOf('function showApp('), source.indexOf('\nfunction configureWorkspaceAccess(')) +
    source.slice(source.indexOf('function disconnect()'), source.indexOf('\nasync function refreshSessions()', source.indexOf('function disconnect()'))), context);
  context.showApp();
  assert(tabs.classes.has('hidden'));
  assert(panels.every(panel => panel.classes.has('hidden')));
  assert.deepEqual(refreshes, [], 'hidden pivot panel must not refresh');
  context.setWorkspaceTab('files');
  assert.deepEqual(refreshes, [], 'hidden file panel must not refresh');
  context.state.selectedId = 'agent-a';
  context.state.sessions = [{ id: 'agent-a', username: 'user', hostname: 'host' }];
  context.setWorkspaceTab('shell');
  assert(!tabs.classes.has('hidden'));
  assert(!panels[0].classes.has('hidden'));
  context.state.selectedId = null;
  context.state.selectedHistoryId = 'archive-a';
  context.state.viewMode = 'history';
  context.setWorkspaceTab('shell');
  assert(!tabs.classes.has('hidden'), 'selected archived transcript remains accessible');
  context.disconnect();
  assert.equal(context.state.selectedId, null);
  assert.equal(context.state.selectedHistoryId, null);
  assert.equal(context.state.selectionGeneration, 6, 'pending transcript loads must be invalidated');
  context.state.token = 'new-token';
  context.showApp();
  assert(tabs.classes.has('hidden'));
  assert(panels.every(panel => panel.classes.has('hidden')));
  assert(nodes.get('#console-header').classes.has('hidden'));
  assert(!nodes.get('#empty-state').classes.has('hidden'));
  assert.deepEqual(refreshes, []);
});

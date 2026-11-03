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

const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
function setup() {
  const alerts = [];
  const state = { token: 'token', authGeneration: 1, sessionsRequest: 0, historyRequest: 0,
    sessions: [{ id: 'agent' }, { id: 'other' }], historySessions: [],
    killingSessionIds: new Set(), killedSessionIds: new Set(), viewMode: 'live', selectedId: 'other' };
  const context = vm.createContext({ state, window: {}, WORKSPACE_MODE: true,
    confirm: () => true, alert: message => alerts.push(message),
    renderSessionList() {}, renderHistoryList() {}, updateSessionSidebarMeta() {},
    updateShellPrompt() {}, sessionLiveDetail: () => '', $: () => ({}),
    showEmptyConsole() { state.selectedId = null; }, disconnect() {},
    api: async () => ({ status: 200, data: { sessions: [] } }) });
  for (const [start, end] of [
    ['function applySessionsUpdate(', 'function updateSessionSidebarMeta('],
    ['async function refreshSessions(', 'function renderSessionList('],
    ['async function fetchSessionHistory(', 'function renderHistoryList('],
    ['async function killSession(', 'window.rmmApi = api;'],
  ]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);
  return { context, state, alerts };
}
test('kill prevents duplicate submission and rejects stale live snapshots after success', async () => {
  const { context, state } = setup();
  const pending = deferred();
  let deletes = 0;
  context.api = async (url, options) => {
    if (options?.method === 'DELETE') { deletes++; return pending.promise; }
    return { status: 200, data: { sessions: [{ id: 'agent' }, { id: 'other' }] } };
  };
  const first = context.killSession('agent');
  assert.equal(state.killingSessionIds.has('agent'), true);
  await context.killSession('agent');
  assert.equal(deletes, 1);
  pending.resolve({ status: 200, data: {} });
  await first;
  context.applySessionsUpdate([{ id: 'agent' }, { id: 'other' }]);
  assert.deepEqual(Array.from(state.sessions, s => s.id), ['other']);
  assert.equal(state.selectedId, 'other');
  assert.equal(state.killingSessionIds.size, 0);
});
test('failed kill keeps agent visible, reports error, and permits retry', async () => {
  const { context, state, alerts } = setup();
  context.api = async () => ({ status: 503, data: { error: 'unavailable' } });
  await context.killSession('agent');
  assert.equal(state.sessions.length, 2);
  assert.equal(state.killedSessionIds.size, 0);
  assert.equal(state.killingSessionIds.size, 0);
  assert.deepEqual(alerts, ['unavailable']);
});
test('transport failure releases pending state', async () => {
  const { context, state, alerts } = setup();
  context.api = async () => { throw new Error('network failed'); };
  await context.killSession('agent');
  assert.equal(state.killingSessionIds.size, 0);
  assert.equal(state.killedSessionIds.size, 0);
  assert.deepEqual(alerts, ['network failed']);
});
test('refresh failure after acceptance preserves success and never offers a duplicate kill', async () => {
  const { context, state, alerts } = setup();
  let deletes = 0;
  context.api = async (_url, options) => {
    if (options?.method === 'DELETE') {
      deletes++;
      return { status: 200, data: {} };
    }
    throw new Error('network failed');
  };
  await context.killSession('agent');
  assert.equal(state.killedSessionIds.has('agent'), true);
  assert.deepEqual(Array.from(state.sessions, s => s.id), ['other']);
  assert.match(alerts[0], /^Session killed\./);
  await context.killSession('agent');
  assert.equal(deletes, 1);
});
test('canceling confirmation leaves state and network untouched', async () => {
  const { context, state } = setup();
  context.confirm = () => false;
  context.api = () => { throw new Error('must not send'); };
  await context.killSession('agent');
  assert.equal(state.killingSessionIds.size, 0);
  assert.equal(state.killedSessionIds.size, 0);
  assert.equal(state.sessions.length, 2);
});
test('HTTP refresh failures report accepted kill while still fetching the archive', async () => {
  const { context, state, alerts } = setup();
  let historyReads = 0;
  context.api = async (url, options) => {
    if (options?.method === 'DELETE') return { status: 200, data: {} };
    if (url === '/history') {
      historyReads++;
      return { status: 200, data: { sessions: [{ session_id: 'agent', active: false }] } };
    }
    return { status: 503, data: {} };
  };
  await context.killSession('agent');
  assert.equal(historyReads, 1);
  assert.equal(state.historySessions[0].session_id, 'agent');
  assert.equal(state.killedSessionIds.has('agent'), true);
  assert.match(alerts[0], /^Session killed\./);
});
test('session and history reads ignore older responses and connection changes', async () => {
  for (const method of ['refreshSessions', 'fetchSessionHistory']) {
    const { context, state } = setup();
    const older = deferred(), newer = deferred();
    let reads = 0;
    context.api = () => (++reads === 1 ? older.promise : newer.promise);
    const first = context[method](), second = context[method]();
    newer.resolve({ status: 200, data: { sessions: [{ id: 'new' }] } });
    await second;
    older.resolve({ status: 200, data: { sessions: [{ id: 'old' }] } });
    await first;
    const key = method === 'refreshSessions' ? 'sessions' : 'historySessions';
    assert.equal(state[key][0].id, 'new');
    const late = deferred();
    context.api = () => late.promise;
    const request = context[method]();
    state.authGeneration++;
    late.resolve({ status: 200, data: { sessions: [{ id: 'other-account' }] } });
    await request;
    assert.equal(state[key][0].id, 'new');
  }
});
test('kill completion from an earlier connection cannot alter the new connection', async () => {
  const { context, state, alerts } = setup();
  const pending = deferred();
  context.api = () => pending.promise;
  const request = context.killSession('agent');
  state.authGeneration++;
  state.killingSessionIds.clear();
  pending.resolve({ status: 200, data: {} });
  await request;
  assert.equal(state.killedSessionIds.size, 0);
  assert.equal(state.sessions.length, 2);
  assert.equal(alerts.length, 0);
});

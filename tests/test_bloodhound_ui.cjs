// Exercise the production BloodHound launch flow without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/bloodhound.js'), 'utf8');

function classList() {
  const classes = new Set();
  return {
    classes,
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
    contains(name) { return classes.has(name); },
    toggle(name, force) {
      const wanted = force === undefined ? !classes.has(name) : force;
      if (wanted) classes.add(name); else classes.delete(name);
    },
  };
}

function setup(api, blocked = false) {
  const button = { disabled: false, addEventListener(name, callback) { this[name] = callback; } };
  const resetButton = { disabled: false, classList: classList(), addEventListener(name, callback) { this[name] = callback; } };
  const status = { textContent: '', classList: classList() };
  const readiness = { textContent: '' };
  const mcpLabel = { textContent: '', dataset: {} }, mcpStatus = {}, mcpHelp = {};
  const nodes = { 'bloodhound-open': button, 'bloodhound-reset': resetButton, 'bloodhound-status': status, 'bloodhound-readiness': readiness,
    'mcp-status-label': mcpLabel, 'exegol-mcp-status': mcpStatus, 'mcp-status-help': mcpHelp };
  const events = [];
  let tab;
  function makeTab() {
    return { opener: {}, closed: false, document: { title: '', body: {} },
      location: { replace(url) { events.push(['navigate', url]); } },
      close() { this.closed = true; } };
  }
  const window = { open(url) { events.push(['open', url]); tab = blocked ? null : makeTab(); return tab; },
    rmmBloodHoundActions: ['health', 'query', 'result', 'library_search'],
    rmmApi: async (...args) => { events.push(['request', ...args]); return api(...args); } };
  vm.runInNewContext(source, { window, URL, AbortController,
    document: { getElementById: id => nodes[id] || null, createElement: () => ({}) },
    setTimeout(callback, delay) { if (delay === 1000) queueMicrotask(callback); return 1; }, clearTimeout() {} });
  return { button, resetButton, status, readiness, mcpLabel, mcpStatus, mcpHelp, events, window,
    get tab() { return tab; } };
}

(async () => {
  let finishOldLaunch;
  const staleLaunch = setup(() => new Promise(resolve => { finishOldLaunch = resolve; }));
  const oldLaunch = staleLaunch.button.click();
  staleLaunch.window.stopBloodHoundHealth();
  assert.equal(staleLaunch.button.disabled, false);
  assert.equal(staleLaunch.events[1][2].signal.aborted, true);
  staleLaunch.status.textContent = 'New connection status';
  finishOldLaunch({ status: 200, data: { url: 'http://localhost:1031/launch?ticket=old-owner' } });
  await oldLaunch;
  assert.ok(!staleLaunch.events.some(event => event[0] === 'navigate'));
  assert.equal(staleLaunch.tab.closed, true);
  assert.equal(staleLaunch.status.textContent, 'New connection status');

  const ui = setup(async () => ({ status: 200, data: { url: 'http://localhost:1031/launch?ticket=one-use' } }));
  await ui.button.click();
  assert.deepEqual(ui.events.map(e => e[0]), ['open', 'request', 'navigate']);
  assert.equal(ui.tab.opener, null);
  assert.equal(ui.events[1][1], '/bloodhound');
  assert.deepEqual(JSON.parse(ui.events[1][2].body), { action: 'ui' });
  assert.equal(ui.button.disabled, false);

  let calls = 0;
  const relay = setup(async (route, options) => {
    const body = JSON.parse(options.body);
    if (++calls === 1) return { status: 202, data: { workspace_request_id: 'a'.repeat(32) } };
    assert.deepEqual(body, { action: 'result', run_id: 'a'.repeat(32) });
    return { status: 200, data: { url: 'https://bloodhound.lab/launch?ticket=secret' } };
  });
  await relay.button.click();
  assert.equal(calls, 2);
  assert.equal(relay.events.at(-1)[0], 'navigate');

  const blocked = setup(() => { throw new Error('must not request'); }, true);
  await blocked.button.click();
  assert.match(blocked.status.textContent, /Allow pop-ups/);
  assert.equal(blocked.events.length, 1);

  for (const url of ['javascript:alert(1)', 'https://user:secret@lab/', '/relative']) {
    const invalid = setup(async () => ({ status: 200, data: { url } }));
    await invalid.button.click();
    assert.equal(invalid.tab.closed, true);
    assert.ok(!invalid.events.some(e => e[0] === 'navigate'));
    assert.match(invalid.status.textContent, /Cannot open BloodHound/);
  }

  const failure = setup(async () => ({ status: 503, data: { error: '<img src=x onerror=alert(1)>' } }));
  await failure.button.click();
  assert.match(failure.status.textContent, /<img/);
  assert.equal(failure.status.innerHTML, undefined);
  assert.equal(failure.button.disabled, false);
  assert.equal(failure.tab.closed, true);

  const pending = setup(async () => ({ status: 202, data: { workspace_request_id: 'b'.repeat(32) } }));
  await pending.button.click();
  assert.equal(pending.events.filter(e => e[0] === 'request').length, 120);
  assert.equal(pending.tab.closed, true);
  assert.match(pending.status.textContent, /still preparing/);

  const actions = [];
  const starting = setup(async (route, options) => {
    const action = JSON.parse(options.body).action;
    actions.push(action);
    if (action === 'health') return { status: 200, data: { runtime: { state: 'ready', exegol_status: 'ready', bloodhound_status: 'ready' } } };
    return actions.length === 1
      ? { status: 202, data: { runtime: { state: 'starting' } } }
      : { status: 200, data: { url: 'http://localhost:1031/launch?ticket=fresh' } };
  });
  await starting.button.click();
  assert.deepEqual(actions, ['ui', 'health', 'ui']);
  assert.match(starting.readiness.textContent, /Exegol: ready/);

  const uncertainActions = [];
  const uncertain = setup(async (route, options) => {
    const action = JSON.parse(options.body).action;
    uncertainActions.push(action);
    return action === 'ui'
      ? { status: 202, data: { runtime: { state: 'starting' } } }
      : { status: 200, data: { runtime: { state: 'uncertain', error: 'inspect_previous_launch' } } };
  });
  await uncertain.button.click();
  assert.deepEqual(uncertainActions, ['ui', 'health']);
  assert.match(uncertain.status.textContent, /Retry BloodHound startup/);
  assert.equal(uncertain.tab.closed, true);
  assert.equal(uncertain.resetButton.classList.contains('hidden'), false);

  // Resolved states (not uncertain) keep the retry control hidden.
  const resolved = setup(async () => ({ status: 200, data: { runtime: { state: 'failed', error: 'bloodhound_unavailable' } } }));
  await resolved.window.startBloodHoundHealth();
  assert.equal(resolved.resetButton.classList.contains('hidden'), true);

  // Clicking retry clears the stuck dispatch and immediately re-attempts the open flow.
  const retryActions = [];
  const retry = setup(async (route, options) => {
    const body = JSON.parse(options.body);
    retryActions.push(body.action);
    if (body.action === 'ui' && retryActions.filter(a => a === 'ui').length === 1) {
      return { status: 202, data: { runtime: { state: 'starting' } } };
    }
    if (body.action === 'health') {
      return { status: 200, data: { runtime: { state: 'uncertain', error: 'inspect_previous_launch' } } };
    }
    if (body.action === 'reset') return { status: 200, data: { runtime: { state: 'blocked' } } };
    return { status: 200, data: { url: 'http://localhost:1031/launch?ticket=retried' } };
  });
  await retry.button.click();
  assert.equal(retry.resetButton.classList.contains('hidden'), false);
  await retry.resetButton.click();
  assert.deepEqual(retryActions, ['ui', 'health', 'reset', 'ui']);
  assert.equal(retry.events.at(-1)[0], 'navigate');
  assert.equal(retry.events.at(-1)[1], 'http://localhost:1031/launch?ticket=retried');
  assert.equal(retry.resetButton.classList.contains('hidden'), true);
  assert.equal(retry.button.disabled, false);
  assert.equal(retry.resetButton.disabled, false);

  // A reset response that arrives after the workspace connection changed must
  // not resurrect the retry control, clobber the status left by the switch,
  // or re-attempt the open flow.
  let finishStaleReset;
  const staleReset = setup(async (route, options) => {
    const action = JSON.parse(options.body).action;
    if (action === 'ui') return { status: 202, data: { runtime: { state: 'starting' } } };
    if (action === 'health') return { status: 200, data: { runtime: { state: 'uncertain', error: 'inspect_previous_launch' } } };
    return new Promise(resolve => { finishStaleReset = resolve; }); // action === 'reset'
  });
  await staleReset.button.click();
  assert.equal(staleReset.resetButton.classList.contains('hidden'), false);
  const staleResetCall = staleReset.resetButton.click();
  staleReset.window.stopBloodHoundHealth();
  assert.equal(staleReset.resetButton.disabled, false);
  assert.equal(staleReset.button.disabled, false);
  staleReset.status.textContent = 'New connection status';
  finishStaleReset({ status: 200, data: { runtime: { state: 'blocked' } } });
  await staleResetCall;
  assert.equal(staleReset.status.textContent, 'New connection status');
  assert.equal(staleReset.resetButton.classList.contains('hidden'), true);
  assert.ok(!staleReset.events.some(event => event[0] === 'navigate'));

  const healthActions = [];
  const monitor = setup(async (route, options) => {
    healthActions.push(JSON.parse(options.body).action);
    return { status: 200, data: { runtime: { state: 'ready', mcp_status: 'verified', exegol_status: 'connected', bloodhound_status: 'ready' } } };
  });
  await monitor.window.startBloodHoundHealth();
  monitor.window.stopBloodHoundHealth();
  await monitor.window.startBloodHoundHealth();
  assert.deepEqual(healthActions, ['health', 'health']);
  assert.match(monitor.readiness.textContent, /BloodHound: ready/);
  assert.equal(monitor.mcpLabel.dataset.state, 'ready');
  assert.match(monitor.mcpHelp.textContent, /not a persistent/);
  monitor.window.stopBloodHoundHealth();
  assert.equal(monitor.mcpLabel.textContent, 'MCP · Not checked');
  const failedMonitor = setup(async () => ({ status: 200, data: { runtime: {
    mcp_status: 'error', error: 'exegol_runtime_failed', exegol_status: 'unavailable'
  } } }));
  await failedMonitor.window.startBloodHoundHealth();
  assert.equal(failedMonitor.mcpLabel.textContent, 'MCP · Check needed');
  assert.equal(failedMonitor.mcpLabel.dataset.state, 'error');
  assert.match(failedMonitor.readiness.textContent, /exegol_runtime_failed/);
  const unavailableMonitor = setup(async () => { throw new Error('offline'); });
  await unavailableMonitor.window.startBloodHoundHealth();
  assert.equal(unavailableMonitor.mcpLabel.textContent, 'MCP · Unavailable');
  console.log('BloodHound UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

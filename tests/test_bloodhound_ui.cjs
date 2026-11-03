// Exercise the production BloodHound launch flow without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/bloodhound.js'), 'utf8');

function setup(api, blocked = false) {
  const button = { disabled: false, addEventListener(name, callback) { this[name] = callback; } };
  const status = { textContent: '', classList: { toggle() {} } };
  const readiness = { textContent: '' };
  const mcpLabel = { textContent: '', dataset: {} }, mcpStatus = {}, mcpHelp = {};
  const form = { addEventListener(name, callback) { this[name] = callback; } };
  const action = { value: '', children: [], appendChild(child) { this.children.push(child); } };
  const runId = { value: '' }, options = { value: '{}' }, submit = { disabled: false }, response = { textContent: '' };
  const nodes = { 'bloodhound-open': button, 'bloodhound-status': status, 'bloodhound-readiness': readiness,
    'mcp-status-label': mcpLabel, 'exegol-mcp-status': mcpStatus, 'mcp-status-help': mcpHelp,
    'bloodhound-operation-form': form, 'bloodhound-action': action, 'bloodhound-run-id': runId,
    'bloodhound-options': options, 'bloodhound-submit': submit, 'bloodhound-response': response };
  const events = [];
  const tab = { opener: {}, closed: false, document: { title: '', body: {} },
    location: { replace(url) { events.push(['navigate', url]); } },
    close() { this.closed = true; } };
  const window = { open(url) { events.push(['open', url]); return blocked ? null : tab; },
    rmmBloodHoundActions: ['health', 'query', 'result', 'library_search'],
    rmmApi: async (...args) => { events.push(['request', ...args]); return api(...args); } };
  vm.runInNewContext(source, { window, URL, AbortController,
    document: { getElementById: id => nodes[id] || null, createElement: () => ({}) },
    setTimeout(callback, delay) { if (delay === 1000) queueMicrotask(callback); return 1; }, clearTimeout() {} });
  return { button, status, readiness, mcpLabel, mcpStatus, mcpHelp, events, tab, window, form, action, runId, options, submit, response };
}

(async () => {
  const operation = setup(async () => ({ status: 202, data: { workspace_request_id: 'a'.repeat(32) } }));
  // No agent/session state is present: the form operates directly at the owner.
  operation.action.value = 'library_search';
  operation.options.value = '{"text":"Tier Zero"}';
  await operation.form.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(operation.events[0][2].body), { action: 'library_search', run_id: '', options: { text: 'Tier Zero' } });
  assert.match(operation.response.textContent, /workspace_request_id/);
  assert.equal(operation.submit.disabled, false);
  for (const invalid of ['[]', 'null', 'bad json']) {
    operation.options.value = invalid;
    await operation.form.submit({ preventDefault() {} });
    assert.equal(operation.events.length, 1);
    assert.match(operation.response.textContent, /request failed/);
  }
  let finish;
  const stale = setup(() => new Promise(resolve => { finish = resolve; }));
  const running = stale.form.submit({ preventDefault() {} });
  stale.window.stopBloodHoundHealth();
  assert.equal(stale.events[0][2].signal.aborted, true);
  finish({ status: 200, data: { previous_owner: true } });
  await running;
  assert.equal(stale.response.textContent, '');
  assert.equal(stale.submit.disabled, false);

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
  assert.match(uncertain.status.textContent, /inspect_previous_launch/);
  assert.equal(uncertain.tab.closed, true);

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

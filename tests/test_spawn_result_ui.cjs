const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({ escapeHtml: text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;') });
vm.runInContext(source.slice(source.indexOf('function renderSpawnResult('), source.indexOf('async function dispatchShellMetaCommand(')), context);

test('spawn summary reports registration without execution probes and retains diagnostics', () => {
  const html = context.renderSpawnResult({ registered: true, verified: false,
    spawned_session_id: 'go-spawn-123', parent_session_id: 'go-parent', spawn_pid: 42,
    parent_responsive: true, spawned_responsive: false, registered_hostname: '<host>' });
  assert.match(html, /Spawn registered/);
  assert.match(html, /Session: go-spawn-123/);
  assert.match(html, /PID: 42/);
  assert.match(html, /Host: &lt;host&gt;/);
  assert.doesNotMatch(html, /<host>/);
  assert.match(html, /Registration: confirmed/);
  assert.doesNotMatch(html, /Parent response:|Spawned agent response:|Verification:/);
  assert.match(html, /<details><summary>Response details \(JSON\)<\/summary>/);
  assert.doesNotMatch(html, /<details open/);
});

test('pending and missing registration never claim success', () => {
  assert.match(context.renderSpawnResult({ registered: null }), /Spawn pending/);
  assert.match(context.renderSpawnResult({ registered: null }), /Registration: not checked/);
  assert.match(context.renderSpawnResult({ registered: false }), /Spawn unconfirmed/);
  assert.match(context.renderSpawnResult({ registered: true, verified: true }), /Registration: confirmed/);
});

test('spawn summary exposes identity downgrade separately from registration', () => {
  const html = context.renderSpawnResult({registered:true, verified:true, spawn_method:'CreateProcessA', identity_degraded:true});
  assert.match(html, /Method: CreateProcessA/);
  assert.match(html, /WARNING: spawned with the original process identity/);
  assert.match(html, /Registration: confirmed/);
  assert.doesNotMatch(context.renderSpawnResult({identity_degraded:false}), /WARNING:/);
});

test('spawn summary includes failures before successful fallback with escaped errors', () => {
  const html = context.renderSpawnResult({spawn_diagnostics:{attempts:[
    {method:'CreateProcessAsUser',success:false,error_code:1314,error:'<privilege error>'},
    {method:'CreateProcessA',success:true}
  ]}});
  assert.match(html, /CreateProcessAsUser: failed \(Windows 1314\): &lt;privilege error&gt;/);
  assert.match(html, /CreateProcessA: started/);
  assert.doesNotMatch(html, /<privilege error>/);
});

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
  assert.equal(hidden.get('bloodhound-operations'), false);
  assert.equal(hidden.get('workspace-access-notice'), true);
});

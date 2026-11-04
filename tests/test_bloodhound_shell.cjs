const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function fixture(response = { status: 202, data: { workspace_request_id: 'a'.repeat(32) } }) {
  const calls = [], outputs = [], errors = [];
  const context = { state: { selectedId: 'session', viewMode: 'live', selectionGeneration: 1 }, window: {},
    api: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return response; },
    appendShellEcho() {}, appendShellError: (message) => errors.push(message),
    appendShellLine: (...args) => outputs.push(args), escapeHtml: value => value.replaceAll('<', '&lt;'),
  };
  vm.createContext(context);
  vm.runInContext(source.match(/const SHELL_META_COMMANDS = \[.*?\];/s)[0] + source.match(/const BLOODHOUND_ACTIONS = \[.*?\];/s)[0] +
    source.slice(source.indexOf('function captureShellTarget('), source.indexOf('async function postDownloadQueue(')) +
    source.slice(source.indexOf('function parseShellWords('), source.indexOf('function defaultExfilProfile(')) +
    source.slice(source.indexOf('async function dispatchShellMetaCommand('), source.indexOf('async function runCommand(')), context);
  return { context, calls, outputs, errors, run: cmd => context.dispatchShellMetaCommand(cmd) };
}
test('all BloodHound actions dispatch locally with preserved options and acceptance', async () => {
  const actions = JSON.parse(source.match(/const BLOODHOUND_ACTIONS = (\[.*?\]);/s)[1]);
  for (const action of actions) {
    const f = fixture();
    assert.equal(await f.run(`/bloodhound ${action} --run-id abc --options-json '{"text":"Tier Zero","path":"C:\\\\Lab"}'`), true);
    assert.deepEqual(f.calls, [{ url: '/bloodhound', body: { action, run_id: 'abc', options: { text: 'Tier Zero', path: 'C:\\Lab' } } }]);
    assert.match(f.outputs[0][1], /workspace_request_id/);
    assert.equal(f.errors.length, 0);
  }
});
test('invalid commands never fall through to remote dispatch', async () => {
  for (const command of ['/bloodhound', '/bloodhound unknown', '/bloodhound query --options-json []', '/bloodhound query --options-json null', '/bloodhound query --options-json "unfinished', '/bloodhound query --wat x', '/bloodhound query --run-id']) {
    const f = fixture();
    assert.equal(await f.run(command), true);
    assert.equal(f.calls.length, 0);
    assert.match(f.errors[0], /Usage:/);
  }
});
test('responses are escaped and delayed replies never cross session views', async () => {
  const f = fixture({ status: 400, data: { error: '<img>' } });
  await f.run('/bloodhound health');
  assert.match(f.outputs[0][1], /&lt;img>/);
  let resolve;
  const pending = fixture(new Promise(done => { resolve = done; }));
  const operation = pending.run('/bloodhound health');
  pending.context.state.selectedId = 'other';
  pending.context.state.selectionGeneration++;
  resolve({ status: 200, data: {} });
  await operation;
  assert.equal(pending.outputs.length, 0);
});

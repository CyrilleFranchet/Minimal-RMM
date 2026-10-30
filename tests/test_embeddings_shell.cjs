const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function fixture(response = { status: 200, data: { queued: '__PE_LOAD__ fixture Run {}' } }) {
  const calls = [], echoes = [], errors = [];
  const context = { state: { selectedId: 'go-session', viewMode: 'live', selectionGeneration: 1 },
    api: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); if (response instanceof Error) throw response; return response; },
    appendShellEcho: (...args) => echoes.push(args), appendShellError: (message) => errors.push(message),
    pollSessionEvents: async () => {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function captureShellTarget('), source.indexOf('async function postDownloadQueue(')) + source.slice(source.indexOf('function parseShellWords('), source.indexOf('function defaultExfilProfile(')) +
    source.slice(source.indexOf('async function dispatchShellMetaCommand('), source.indexOf('async function runCommand(')), context);
  return { context, calls, echoes, errors, run: (cmd) => context.dispatchShellMetaCommand(cmd) };
}
test('embeddings queues a saved profile for the selected session and matches plugin output', async () => {
  const f = fixture();
  const cmd = String.raw`embeddings run strategy --document "C:\Lab Docs\notes.txt" --document "\\server\share\Docs"`;
  assert.equal(await f.run(cmd), true);
  assert.deepEqual(f.calls, [{ url: '/embeddings/run', body: { session_ref: 'go-session', profile: 'strategy', documents: [String.raw`C:\Lab Docs\notes.txt`, String.raw`\\server\share\Docs`] } }]);
  assert.equal(f.echoes[0][0], cmd);
  assert.ok(f.echoes[0][1].matchKeys.includes('__PE_LOAD__ fixture Run {}'));
  assert.equal(f.errors.length, 0);
});
test('invalid syntax stays local without queuing or claiming success', async () => {
  for (const cmd of ['embeddings', 'embeddings run', 'embeddings run strategy', 'embeddings run strategy --document', 'embeddings run strategy --wat x', 'embeddings run strategy --document "unfinished', 'embeddings run strategy ' + '--document x '.repeat(33)]) {
    const f = fixture();
    assert.equal(await f.run(cmd), true);
    assert.equal(f.calls.length, 0, cmd);
    assert.equal(f.echoes.length, 0);
    assert.match(f.errors[0], /Usage:/);
  }
});
test('missing session and API failures do not echo a queued command', async () => {
  const cmd = 'embeddings run strategy --document C:\\Lab';
  const missing = fixture(); missing.context.state.selectedId = '';
  await missing.run(cmd); assert.equal(missing.calls.length, 0); assert.match(missing.errors[0], /Select a session/);
  for (const response of [{ status: 404, data: { error: 'profile_not_found' } }, new Error('offline')]) {
    const f = fixture(response); await f.run(cmd);
    assert.equal(f.echoes.length, 0); assert.equal(f.errors.length, 1);
  }
});
test('delayed embedding acknowledgement stays out of the newly selected shell', async () => {
  let resolve;
  const response = new Promise(done => { resolve = done; });
  const f = fixture(response);
  const operation = f.run('embeddings run strategy --document C:\\Lab');
  f.context.state.selectedId = 'other-session';
  f.context.state.selectionGeneration++;
  resolve({ status: 200, data: { queued: '__PE_LOAD__ fixture Run {}' } });
  await operation;
  assert.equal(f.calls[0].body.session_ref, 'go-session');
  assert.equal(f.echoes.length, 0);
  assert.equal(f.errors.length, 0);
});

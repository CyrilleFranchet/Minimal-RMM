const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');

function fixture() {
  const calls = [], errors = [], polls = [];
  let current = true, resolve;
  const pending = new Promise(r => { resolve = r; });
  const context = vm.createContext({
    SHELL_META_COMMANDS: ['whoami'], parseShellWords: text => text.trim().split(/\s+/),
    appendShellEcho() {}, appendShellError: text => errors.push(text),
    api: async (path, options) => {
      calls.push({ path, body: JSON.parse(options.body) });
      return pending;
    },
    pollSessionEvents: async () => polls.push('poll'),
  });
  vm.runInContext(source.slice(source.indexOf('async function dispatchShellMetaCommand('),
    source.indexOf('async function runCommand(')), context);
  return { calls, errors, polls, resolve,
    run: cmd => context.dispatchShellMetaCommand(cmd, {sessionId: 'a/b', isCurrent: () => current}),
    switchSession: () => { current = false; } };
}

test('whoami forwards optional ticket metadata and uses captured session', async () => {
  for (const tickets of [false, true]) {
    const f = fixture();
    const operation = f.run(tickets ? '/whoami --tickets' : '/whoami');
    assert.equal(f.calls[0].path, '/sessions/a%2Fb/whoami');
    assert.equal(f.calls[0].body.tickets, tickets);
    f.resolve({ status: 200, data: { output: '<img src=x>' } });
    await operation;
    assert.deepEqual(f.errors, []);
    assert.deepEqual(f.polls, ['poll']);
  }
});

test('whoami rejects unknown and repeated options without issuing requests', async () => {
  for (const cmd of ['/whoami --bad', '/whoami --tickets --tickets', '/whoami extra']) {
    const f = fixture();
    assert.equal(await f.run(cmd), true);
    assert.equal(f.calls.length, 0);
    assert.deepEqual(f.errors, ['Usage: /whoami [--tickets]']);
  }
});

test('whoami stale replies cannot render errors or poll another session', async () => {
  const f = fixture();
  const operation = f.run('/whoami');
  f.switchSession();
  f.resolve({status: 409, data: {error: 'old error'}});
  await operation;
  assert.deepEqual(f.errors, []);
  assert.deepEqual(f.polls, []);
});

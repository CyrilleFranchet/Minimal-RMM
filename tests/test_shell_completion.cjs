const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function element() {
  const attrs = {}, classes = new Set(['hidden']), listeners = {};
  return { value: '', disabled: false, dataset: {}, children: [], attrs, listeners,
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: k => { delete attrs[k]; },
    replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
    addEventListener: (name, fn) => { listeners[name] = fn; }, setSelectionRange() {}, focus() {},
  };
}
function fixture() {
  const input = element(), menu = element(), hint = element(), errors = [], calls = [], history = [];
  const nodes = { '#shell-input': input, '#shell-command-menu': menu, '#shell-completion-hint': hint, '#shell-prompt': element() };
  let lastShellLine = null;
  const context = { document: { createElement: element }, $: key => nodes[key],
    state: { echoedCommands: new Set(), viewMode: 'live', selectedId: 'agent', selectionGeneration: 1, shellTabCycle: { anchor: null, candidates: [], index: -1 } },
    getShellHistoryList: () => ['whoami', '/screenshot'], resetShellHistoryNav() {}, resetShellTabCycle() {},
    navigateShellHistory: () => { throw new Error('History must not handle menu navigation'); },
    api: async (...args) => { calls.push(args); return { status: 200, data: {} }; },
    appendShellError: text => errors.push(text), rememberShellCommand: cmd => history.push(cmd),
    appendShellLine: () => { lastShellLine = element(); return lastShellLine; }, escapeHtml: text => text,
    createCommandBlock() {}, setTimeout() {}, pollSessionEvents: async () => {},
  };
  vm.createContext(context);
  vm.runInContext(source.match(/const SHELL_META_COMMANDS = \[.*?\];/s)[0] +
    source.match(/const SHELL_DISPATCH_PREFIXES = \[.*?\];/s)[0] +
    source.slice(source.indexOf('function appendShellEcho('), source.indexOf('// --- Shell history')) +
    source.slice(source.indexOf('function longestCommonPrefix('), source.indexOf('function appendShellOutput(')) +
    source.slice(source.indexOf('function parseShellWords('), source.indexOf('function defaultExfilProfile(')) +
    source.slice(source.indexOf('function defaultExfilProfile('), source.indexOf('/** @returns {boolean} true if cmd was a meta command')) +
    source.slice(source.indexOf('async function dispatchShellMetaCommand('), source.indexOf('async function queueDownload(')) +
    source.slice(source.indexOf('async function queueDownload('), source.indexOf('function formatExfilProfileOption(')) +
    source.slice(source.indexOf('async function queueExfil('), source.indexOf('async function queueUpload(')), context);
  context.clearShellInput = () => { input.value = ''; context.updateShellCompletionHint(); };
  context.bindShellInputCompletion();
  const type = value => { input.value = value; input.listeners.input(); };
  const key = value => { let prevented = false; input.listeners.keydown({ key: value, preventDefault() { prevented = true; } }); return prevented; };
  return { context, input, menu, hint, errors, calls, history, nodes, type, key,
    get lastShellLine() { return lastShellLine; } };
}
test('slash immediately displays commands; typing and backspacing filter live', () => {
  const f = fixture(); f.type('/');
  assert.equal(f.menu.children.length, 13); assert.equal(f.input.attrs['aria-expanded'], 'true');
  f.type('/s'); assert.deepEqual(f.menu.children.map(x => x.textContent), ['/screenshot', '/spawn']);
  f.type('/sc'); assert.equal(f.menu.children.length, 1);
  f.type('/'); assert.equal(f.menu.children.length, 13);
  f.type('/unknown'); assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('who'); assert.equal(f.input.attrs['aria-expanded'], 'false');
});
test('arrows select, Tab inserts, Escape dismisses until edited, click inserts', () => {
  const f = fixture(); f.type('/'); f.key('ArrowDown');
  assert.equal(f.input.attrs['aria-activedescendant'], 'shell-command-option-1');
  assert.equal(f.key('Tab'), true); assert.equal(f.input.value, '/? ');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('/s'); f.key('Escape'); assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.context.updateShellCompletionHint(); assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('/sc'); assert.equal(f.input.attrs['aria-expanded'], 'true');
  f.menu.children[0].listeners.mousedown({ preventDefault() {} });
  assert.equal(f.input.value, '/screenshot ');
});
test('unknown slash commands stay local and bare commands reach shell unchanged', async () => {
  for (const command of ['/', '/unknown', '//screenshot']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 0); assert.match(f.errors[0], /Unknown internal command/);
  }
  for (const command of ['screenshot', 'help', 'cmd: /screenshot', 'PS: Get-Process']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls[0][0], '/sessions/agent/commands');
    assert.equal(JSON.parse(f.calls[0][1].body).command, command);
  }
  const f = fixture(); f.type('/screenshot'); await f.context.runCommand(false);
  assert.equal(f.calls[0][0], '/sessions/agent/screenshot');
});

test('slash action history can be replayed without becoming a raw shell command', async () => {
  const f = fixture(); f.type('/screenshot'); await f.context.runCommand(false);
  assert.deepEqual(f.history, ['/screenshot']);
  f.type(f.history[0]); await f.context.runCommand(false);
  assert.ok(f.calls.every(call => call[0] === '/sessions/agent/screenshot'));
});
test('disabled and historical shells close the command menu', () => {
  const f = fixture(); f.type('/'); f.input.disabled = true;
  f.context.updateShellCompletionHint(); assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.input.disabled = false; f.type('/'); f.context.state.viewMode = 'history';
  f.context.updateShellCompletionHint(); assert.equal(f.input.attrs['aria-expanded'], 'false');
});

test('download, exfil and plugin actions preserve slash history and literal paths', async () => {
  for (const command of [String.raw`/download "C:\Lab Docs\file.txt"`, String.raw`/exfil "C:\Lab Docs" profile`, '/pe plugin Run data']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.deepEqual(f.history, [command]); assert.equal(f.calls.length, 1);
    if (!command.startsWith('/pe')) assert.match(JSON.parse(f.calls[0][1].body).remote_path, /^C:\\Lab Docs/);
  }
});

test('make_token posts identity and password as parameters', async () => {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const f = fixture(); f.type('/make_token LAB\\alice s3cret-pw'); await f.context.runCommand(false); await settle();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0][0], '/sessions/agent/make_token');
  const body = JSON.parse(f.calls[0][1].body);
  assert.equal(body.user, 'LAB\\alice');
  assert.equal(body.password, 's3cret-pw');
  assert.deepEqual(f.history, ['/make_token LAB\\alice s3cret-pw']);
});

test('make_token rejects missing or extra parameters', async () => {
  const f1 = fixture(); f1.type('/make_token'); await f1.context.runCommand(false);
  assert.equal(f1.calls.length, 0);
  assert.match(f1.errors[0], /Usage: \/make_token/);
  const f2 = fixture(); f2.type('/make_token LAB\\alice'); await f2.context.runCommand(false);
  assert.equal(f2.calls.length, 0);
  assert.match(f2.errors[0], /Usage: \/make_token/);
});

test('rev2self posts directly', async () => {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const f = fixture(); f.type('/rev2self'); await f.context.runCommand(false); await settle();
  assert.equal(f.calls[0][0], '/sessions/agent/rev2self');
  assert.deepEqual(f.history, ['/rev2self']);
});

test('lateral validates method and posts method and host', async () => {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const rejected = fixture(); rejected.type('/lateral ssh ws01'); await rejected.context.runCommand(false);
  assert.equal(rejected.calls.length, 0);
  assert.match(rejected.errors[0], /Method must be psexec, wmiexec, or winrm/);
  const arity = fixture(); arity.type('/lateral psexec'); await arity.context.runCommand(false);
  assert.equal(arity.calls.length, 0);
  assert.match(arity.errors[0], /Usage: \/lateral/);
  const f = fixture(); f.type('/lateral psexec ws01.lab.local'); await f.context.runCommand(false); await settle();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0][0], '/sessions/agent/lateral');
  const body = JSON.parse(f.calls[0][1].body);
  assert.equal(body.method, 'psexec');
  assert.equal(body.host, 'ws01.lab.local');
  assert.deepEqual(f.history, ['/lateral psexec ws01.lab.local']);
});

test('uid posts directly without arguments', async () => {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const f = fixture(); f.type('/uid'); await f.context.runCommand(false); await settle();
  assert.equal(f.calls[0][0], '/sessions/agent/uid');
  assert.deepEqual(f.history, ['/uid']);
  const rejected = fixture(); rejected.type('/uid extra'); await rejected.context.runCommand(false);
  assert.equal(rejected.calls.length, 0);
  assert.match(rejected.errors[0], /Usage: \/uid/);
});
test('panel screenshot/download history is replayable; exfil mode labels are excluded', async () => {
  const f = fixture();
  f.nodes['#download-remote'] = element(); f.nodes['#download-remote'].value = 'C:\\Lab Docs\\file.txt';
  await f.context.queueDownload(); await f.context.queueScreenshot();
  assert.deepEqual(f.history, ['/download C:\\Lab Docs\\file.txt', '/screenshot']);
  f.nodes['#exfil-remote'] = element(); f.nodes['#exfil-remote'].value = 'C:\\Lab';
  f.nodes['#exfil-profile'] = element(); f.nodes['#exfil-profile'].value = 'profile';
  await f.context.queueExfil(); assert.equal(f.history.length, 2);
});

function historyFixture(stored = {}) {
  const context = { SHELL_HISTORY_MAX: 100, state: { shellHistoryBySession: {} },
    loadShellHistoryStore: () => stored, persistShellHistory() {}, resetShellHistoryNav() {},
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function extractShellCommandFromEvent('), source.indexOf('function resetShellHistoryNav(')) +
    source.slice(source.indexOf('function operatorCommandFromBody('), source.indexOf('function renderEventBodyHtml(')), context);
  return context;
}
test('event history excludes tool audit labels without rewriting real shell commands', () => {
  const c = historyFixture();
  const events = [
    { type: 'operator', body: 'exfil: exfil C:\\Lab [mode=plugin]', command: 'exfil C:\\Lab' },
    { type: 'operator', body: 'socks: socks 1080', command: 'socks 1080' },
    { type: 'operator', body: 'tun: tun start rmm0', command: 'tun start rmm0' },
    { type: 'operator', body: 'config: sleep 10', command: 'sleep 10' },
    { type: 'operator', body: 'persist: whoami', command: 'whoami' },
    { type: 'operator', body: 'queued: screenshot', command: 'screenshot' },
    { type: 'operator', body: 'exec: display label [detail]', command: 'cmd: type C:\\Lab\\notes.txt' },
    { type: 'operator', body: 'queued: PS: Get-Content "C:\\Lab Docs\\notes.txt"' },
    { type: 'output', command: 'whoami' },
    { type: 'output', command: '__PE_LOAD__ plugin Run data' },
    { type: 'operator', body: 'queued: __SCREENSHOT__', command: '__SCREENSHOT__' },
  ];
  assert.deepEqual(Array.from(c.rebuildShellHistoryFromEvents(events)), [
    'screenshot', 'cmd: type C:\\Lab\\notes.txt', 'PS: Get-Content "C:\\Lab Docs\\notes.txt"', 'whoami',
  ]);
});
test('history seeding retains original slash action instead of appending incomplete tool audit', () => {
  const stored = { agent: ['/exfil "C:\\Lab Docs" profile', '/screenshot'] };
  const c = historyFixture(stored);
  c.seedShellHistory('agent', [
    { type: 'operator', body: 'exfil: exfil C:\\Lab Docs [mode=plugin]', command: 'exfil C:\\Lab Docs' },
    { type: 'operator', body: 'screenshot: __SCREENSHOT__', command: '__SCREENSHOT__' },
    { type: 'output', command: '__SCREENSHOT__' },
  ]);
  assert.deepEqual(Array.from(c.state.shellHistoryBySession.agent), stored.agent);
});

test('quoted passwords preserve spaces, backslashes and empty values in requests and history', async () => {
  for (const [command, password] of [
    [String.raw`/make_token LAB\alice 'two words\path#1'`, String.raw`two words\path#1`],
    [String.raw`/make_token LAB\alice ""`, ''],
    [String.raw`/make_token LAB\alice "it's a password"`, "it's a password"],
  ]) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 1);
    assert.equal(JSON.parse(f.calls[0][1].body).password, password);
    assert.deepEqual(f.history, [command]);
  }
  const f = fixture(); f.type('/make_token alice "unfinished'); await f.context.runCommand(false);
  assert.equal(f.calls.length, 0);
});

test('lateral method completion filters without completing the target host', () => {
  const f = fixture();
  assert.deepEqual(Array.from(f.context.getShellCompletionCandidates('/lateral w')), ['/lateral wmiexec ', '/lateral winrm ']);
  assert.equal(f.context.getShellCompletionCandidates('/lateral ').length, 3);
  assert.equal(f.context.getShellCompletionCandidates('/lateral winrm ').length, 0);
});

test('removed lateral method is rejected locally and excluded from completion', async () => {
  const f = fixture();
  f.type('/lateral smbexec ws01'); await f.context.runCommand(false);
  assert.equal(f.calls.length, 0);
  assert.match(f.errors[0], /Method must be psexec, wmiexec, or winrm/);
  assert.equal(f.context.getShellCompletionCandidates('/lateral s').length, 0);
});

test('rev2self failure is shown as an error', async () => {
  const f = fixture();
  f.context.api = async () => ({status: 200, data: {ok: false, output: 'Rev2self failed: requires Windows'}});
  f.type('/rev2self'); await f.context.runCommand(false);
  assert.match(f.errors[0], /rev2self failed/);
});

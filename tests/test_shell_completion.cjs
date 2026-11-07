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
  const input = element(), menu = element(), hint = element(), errors = [], calls = [], history = [], lines = [];
  const nodes = { '#shell-input': input, '#shell-command-menu': menu, '#shell-completion-hint': hint, '#shell-prompt': element() };
  let lastShellLine = null;
  const context = { document: { createElement: element }, $: key => nodes[key],
    state: { echoedCommands: new Set(), viewMode: 'live', selectedId: 'agent', selectionGeneration: 1, shellTabCycle: { anchor: null, candidates: [], index: -1 } },
    getShellHistoryList: () => ['whoami', '/screenshot'], resetShellHistoryNav() {}, resetShellTabCycle() {},
    navigateShellHistory: () => { throw new Error('History must not handle menu navigation'); },
    api: async (...args) => { calls.push(args); return { status: 200, data: {} }; },
    appendShellError: text => { errors.push(text); lines.push({kind: 'error', text}); }, rememberShellCommand: cmd => {
      if (history.at(-1) !== cmd) history.push(cmd);
    },
    appendShellLine: (kind, html) => { lines.push({kind, html}); lastShellLine = element(); return lastShellLine; }, escapeHtml: text => text,
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
  return { context, input, menu, hint, errors, calls, history, lines, nodes, type, key,
    get lastShellLine() { return lastShellLine; } };
}
test('slash immediately displays commands; typing and backspacing filter live', () => {
  const f = fixture(); f.type('/');
  assert.equal(f.menu.children.length, 16); assert.equal(f.input.attrs['aria-expanded'], 'true');
  f.type('/s'); assert.deepEqual(f.menu.children.map(x => x.textContent), ['/screenshot', '/spawn', '/spawnas']);
  f.type('/sc'); assert.equal(f.menu.children.length, 1);
  f.type('/'); assert.equal(f.menu.children.length, 16);
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

test('WinRM HTTPS flag forwards a boolean and rejects invalid combinations', async () => {
  for (const command of ['/lateral winrm -s ws01', '/lateral winrm ws01 -s', '/lateral winrm --https ws01']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 1);
    assert.deepEqual(JSON.parse(f.calls[0][1].body), { method: 'winrm', host: 'ws01', https: true });
  }
  for (const command of ['/lateral psexec ws01 -s', '/lateral wmiexec -s ws01', '/lateral winrm -s', '/lateral winrm ws01 -s -s', '/lateral winrm --https ws01 -s']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 0);
    assert.match(f.errors[0], /Usage: \/lateral/);
  }
});

test('WinRM certificate bypass requires HTTPS and forwards only when selected', async () => {
  for (const command of ['/lateral winrm target -s -k', '/lateral winrm --insecure --https target']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.deepEqual(JSON.parse(f.calls[0][1].body), {method:'winrm',host:'target',https:true,insecure:true});
  }
  for (const command of ['/lateral winrm target -k','/lateral psexec target -s -k','/lateral winrm target -s -k -k']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length,0); assert.match(f.errors[0],/Usage:/);
  }
});

test('lateral renders results only through events and preserves registration warnings', async () => {
  for (const method of ['wmiexec', 'psexec', 'winrm']) {
    for (const registered of [true, false]) {
      for (const earlyEvent of [true, false]) {
        const f = fixture();
        const result = registered ? `lateral started: method=${method} host=ws01 target session=child`
          : 'Lateral failed: provider failure';
        let polls = 0, emitted = false;
        const emit = () => {
          if (emitted) return;
          emitted = true;
          f.context.appendShellLine('output', `<pre>${result}</pre>`);
        };
        f.context.api = async () => {
          if (earlyEvent) emit();
          return {status: 200, data: {registered, output: result}};
        };
        f.context.pollSessionEvents = async () => { polls++; emit(); };
        f.type(`/lateral ${method} ws01`);
        await f.context.runCommand(false);
        assert.equal(polls, 1);
        assert.equal(f.lines.filter(line => line.html?.includes(result)).length, 1);
        assert.equal(f.lines.filter(line => line.html?.includes('waiting for the next beacon')).length, 1);
        assert.equal(f.errors.length, registered ? 0 : 1);
        if (!registered) assert.match(f.errors[0], /Deployment unconfirmed/);
      }
    }
  }
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
test('identity results render only through session events, including failed operations', async () => {
  for (const command of ['/uid', '/make_token LAB\\alice test-password', '/rev2self']) {
    for (const ok of [true, false]) {
      const f = fixture();
      const outputs = [];
      let polls = 0;
      f.context.api = async () => ({status: 200, data: {ok, output: 'agent result'}});
      f.context.appendShellLine = (type, html) => { if (type === 'output') outputs.push(html); };
      f.context.pollSessionEvents = async () => {
        polls++;
        f.context.appendShellLine('output', 'agent result');
      };
      f.type(command);
      await f.context.runCommand(false);
      assert.equal(polls, 1);
      assert.deepEqual(outputs, ['agent result']);
    }
  }
});

test('spawnas renders its launch result once through events and preserves registration status', async () => {
  for (const registered of [true, false]) {
    const f = fixture(), outputs = [];
    const result = 'spawned session=child-session pid=42 identity=LAB\\alice';
    f.context.api = async (...args) => {
      f.calls.push(args);
      return {status: 200, data: {spawned_session_id: 'child-session', spawn_pid: 42,
        identity: 'LAB\\alice', verify: true, registered}};
    };
    f.context.appendShellOutput = text => outputs.push(text);
    f.context.pollSessionEvents = async () => outputs.push(result);
    f.type('/spawnas -i cred-example');
    await f.context.runCommand(false);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], '/sessions/agent/spawnas');
    assert.deepEqual(outputs, ['Spawning agent under new identity…',
      `registered: ${registered ? 'yes' : 'no'}`, result]);
    assert.deepEqual(f.errors, registered ? [] : ['spawnas registration was not confirmed.']);
  }
});

test('failed validation and HTTP requests remain recallable as original input', async () => {
  for (const command of ['/unknown', '/uid extra', '/make_token', '/lateral psexec', '/embeddings run']) {
    const f = fixture();
    f.type(command);
    await f.context.runCommand(false);
    assert.deepEqual(f.history, [command]);
    assert.equal(f.calls.length, 0);
    assert.ok(f.errors.length);
  }
  for (const command of ['/uid', 'whoami']) {
    const f = fixture();
    f.context.api = async () => ({status: 403, data: {error: 'denied'}});
    f.type(command);
    await f.context.runCommand(false);
    assert.deepEqual(f.history, [command]);
    assert.deepEqual(f.errors, ['denied']);
  }
});

test('invalid commands appear once before their error without being queued', async () => {
  for (const command of ['/unknown', '/spawn -i cred-fixture', '/lateral psexec',
    '/make_token', '/spawnas "unterminated', '/embeddings run']) {
    const f = fixture();
    f.type(command);
    await f.context.runCommand(false);
    assert.equal(f.calls.length, 0, command);
    assert.deepEqual(f.lines.map(line => line.kind), ['echo', 'error'], command);
    assert.ok(f.lines[0].html.endsWith(command), command);
    assert.deepEqual(f.history, [command]);
  }
  const f = fixture();
  for (let attempt = 0; attempt < 2; attempt++) {
    f.type('/unknown');
    await f.context.runCommand(false);
  }
  assert.deepEqual(f.lines.map(line => line.kind), ['echo', 'error', 'echo', 'error']);
});

test('HTTP rejection keeps a single command echo before the error', async () => {
  const f = fixture();
  f.context.api = async () => ({status: 403, data: {error: 'denied'}});
  f.type('/uid');
  await f.context.runCommand(false);
  assert.deepEqual(f.lines.map(line => line.kind), ['echo', 'error']);
  assert.deepEqual(f.errors, ['denied']);
});

test('invalid command text uses the existing escaped echo renderer', async () => {
  const f = fixture();
  f.context.escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  f.type('/unknown <img src=x onerror=alert(1)>');
  await f.context.runCommand(false);
  assert.match(f.lines[0].html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.ok(!f.lines[0].html.includes('<img'));
  assert.equal(f.calls.length, 0);
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
  assert.deepEqual(Array.from(f.context.getShellCompletionCandidates('/lateral w')), ['/lateral wmiexec', '/lateral winrm']);
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

test('creds dispatches list, add and delete with literal credential arguments', async () => {
  for (const [command, path, body] of [
    ['/creds list', '/creds', null],
    [String.raw`/creds add LAB\alice 'two words\path'`, '/creds', {domain:'LAB', user:'alice', password:String.raw`two words\path`}],
    ["/creds add alice ''", '/creds', {domain:'', user:'alice', password:''}],
    ['/creds del -i credential-123', '/creds/credential-123', null],
  ]) {
    const f = fixture();
    f.context.appendShellOutput = () => {};
    await f.context.dispatchShellMetaCommand(command);
    assert.equal(f.errors.length, 0);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], path);
    if (body) assert.deepEqual(JSON.parse(f.calls[0][1].body), body);
  }
});

test('creds rejects missing and extra arguments without requests', async () => {
  for (const command of ['/creds list extra', '/creds add alice', '/creds add alice pass extra', '/creds del', '/creds del id extra']) {
    const f = fixture();
    await f.context.dispatchShellMetaCommand(command);
    assert.equal(f.calls.length, 0, command);
    assert.match(f.errors[0], /Usage:/);
  }
});

test('creds completes subcommands and displays argument hints without credential history', () => {
  const f = fixture();
  f.type('/creds ');
  assert.deepEqual(f.menu.children.map(x => x.textContent), ['/creds list', '/creds add', '/creds del']);
  f.type('/creds a');
  f.key('Tab');
  assert.equal(f.input.value, '/creds add ');
  assert.match(f.hint.textContent, /<password>/);
  f.type('/creds del ');
  assert.match(f.hint.textContent, /<cred_id>/);
  assert.equal(f.context.getShellCompletionCandidates('/creds add alice ').length, 0);
});

test('identity commands accept stored IDs or direct credentials', async () => {
  for (const verb of ['make_token', 'spawnas', 'runas']) {
    for (const direct of [false, true]) {
      const f = fixture(); f.context.appendShellOutput = () => {};
      f.context.api = async (...args) => { f.calls.push(args); return {status:200,data:{ok:true}}; };
      const identity = direct ? String.raw`LAB\alice 'two words\path'` : '-i cred-123';
      await f.context.dispatchShellMetaCommand(`/${verb} ${identity}${verb === 'runas' ? ' whoami /all' : ''}`);
      assert.equal(f.errors.length, 0);
      assert.equal(f.calls[0][0], `/sessions/agent/${verb}`);
      const body = JSON.parse(f.calls[0][1].body);
      if (direct) { assert.equal(body.user, String.raw`LAB\alice`); assert.equal(body.password, String.raw`two words\path`); assert.equal(body.cred_id, undefined); }
      else { assert.equal(body.cred_id, 'cred-123'); assert.equal(body.password, undefined); }
      if (verb === 'runas') assert.equal(body.command, 'whoami /all');
      f.type(`/${verb} `); assert.match(f.hint.textContent, /<cred_id>/); assert.match(f.hint.textContent, /<password>/);
    }
  }
});

test('credential commands reject missing IDs, positional IDs and mixed identity arguments locally', async () => {
  const commands = ['/creds del cred-123', '/creds del -i', '/creds del -i ""', '/creds del -i id extra'];
  for (const verb of ['make_token', 'spawnas', 'runas']) {
    commands.push(`/${verb} cred-123`, `/${verb} -i`, `/${verb} -i ""`, `/${verb} -i -i`);
    if (verb === 'runas') commands.push('/runas cred-123 whoami', '/runas -i id ""');
    else commands.push(`/${verb} -i id alice password`);
  }
  for (const command of commands) {
    const f = fixture(); await f.context.dispatchShellMetaCommand(command);
    assert.equal(f.calls.length, 0, command);
    assert.match(f.errors[0], /Usage:/, command);
  }
});

test('deployment requests inherit source build mode and preserve flag-like passwords', async () => {
  const cases = [
    ['/spawn', '/sessions/agent/spawn', {}],
    ['/spawnas -i cred-123', '/sessions/agent/spawnas', {cred_id: 'cred-123', verify: true}],
    ['/spawnas alice -d', '/sessions/agent/spawnas', {user: 'alice', password: '-d', verify: true}],
    ['/spawnas alice --debug', '/sessions/agent/spawnas', {user: 'alice', password: '--debug', verify: true}],
    ['/lateral wmiexec ws01', '/sessions/agent/lateral', {method: 'wmiexec', host: 'ws01'}],
  ];
  for (const [command, path, expected] of cases) {
    const f = fixture(); f.context.appendShellOutput = () => {};
    f.context.api = async (...args) => { f.calls.push(args); return {status: 200, data: {ok: true, registered: true}}; };
    await f.context.dispatchShellMetaCommand(command);
    assert.deepEqual(f.errors, [], command);
    assert.equal(f.calls[0][0], path, command);
    assert.deepEqual(JSON.parse(f.calls[0][1].body), expected, command);
  }
});

test('removed deployment debug flags stay local and echo before their error', async () => {
  for (const command of ['/spawn -d', '/spawn --debug', '/spawn extra',
    '/spawnas -i cred-123 -d', '/spawnas --debug -i cred-123', '/spawnas alice pw --debug',
    '/lateral psexec -d', '/lateral psexec ws01 -d', '/lateral --debug winrm ws01',
    '/lateral psexec ws01 extra']) {
    const f = fixture();
    await f.context.dispatchShellMetaCommand(command);
    assert.equal(f.calls.length, 0, command);
    assert.match(f.errors[0], /Usage:/, command);
    assert.deepEqual(f.lines.map(line => line.kind), ['echo', 'error'], command);
  }
});

test('direct identity syntax accepts domainless names and preserves empty or flag-like passwords', async () => {
  for (const verb of ['make_token', 'spawnas', 'runas']) {
    for (const password of ['', '-i', 'two words\\path']) {
      const f = fixture(); f.context.appendShellOutput = () => {};
      f.context.api = async (...args) => { f.calls.push(args); return {status: 200, data: {ok: true}}; };
      await f.context.dispatchShellMetaCommand(`/${verb} alice '${password}'${verb === 'runas' ? ' whoami /all' : ''}`);
      assert.equal(f.errors.length, 0);
      const body = JSON.parse(f.calls[0][1].body);
      assert.equal(body.user, 'alice'); assert.equal(body.password, password);
      assert.equal(body.cred_id, undefined);
      if (verb === 'runas') assert.equal(body.command, 'whoami /all');
    }
  }
});

test('lateral opens its method menu after command completion and accepts keyboard selection', () => {
  const f = fixture();
  f.type('/lat'); f.key('Tab');
  assert.equal(f.input.value, '/lateral ');
  assert.deepEqual(f.menu.children.map(x => x.textContent), ['/lateral psexec', '/lateral wmiexec', '/lateral winrm']);
  assert.equal(f.input.attrs['aria-expanded'], 'true');
  assert.equal(f.input.attrs['aria-activedescendant'], 'shell-command-option-0');
  f.key('ArrowDown'); f.key('Tab');
  assert.equal(f.input.value, '/lateral wmiexec ');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
  assert.equal(f.hint.textContent, '/lateral wmiexec <host>');
});

test('lateral filters methods, supports clicks, and leaves the host untouched', () => {
  const f = fixture();
  f.type('/lateral w');
  assert.deepEqual(f.menu.children.map(x => x.textContent), ['/lateral wmiexec', '/lateral winrm']);
  f.menu.children[1].listeners.mousedown({preventDefault() {}});
  assert.equal(f.input.value, '/lateral winrm ');
  assert.equal(f.hint.textContent, '/lateral winrm <host> [-s|--https] [-k|--insecure]');
  f.context.getShellHistoryList = () => ['/lateral winrm ws01.old.example'];
  f.type('/lateral winrm ws'); f.key('Tab');
  assert.equal(f.input.value, '/lateral winrm ws');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('/lateral smbexec');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
});

test('creds list renders API credential IDs and labels', async () => {
  const f = fixture(), output = [];
  f.context.appendShellOutput = text => output.push(text);
  f.context.api = async () => ({status:200,data:{ok:true,creds:[{cred_id:'cred-123',label:String.raw`LAB\alice`}]}});
  await f.context.dispatchShellMetaCommand('/creds list');
  assert.equal(f.errors.length, 0);
  assert.deepEqual(output, [String.raw`  cred-123  LAB\alice`]);
});

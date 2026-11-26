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
function fixture({anchorBlocks = false} = {}) {
  const input = element(), menu = element(), hint = element(), errors = [], calls = [], history = [], lines = [];
  const nodes = { '#shell-input': input, '#shell-command-menu': menu, '#shell-completion-hint': hint, '#shell-prompt': element() };
  let lastShellLine = null;
  const context = { document: { createElement: element }, $: key => nodes[key],
    state: { echoedCommands: new Set(), pendingCommandBlocks: [], viewMode: 'live', selectedId: 'agent', selectionGeneration: 1, shellTabCycle: { anchor: null, candidates: [], index: -1 } },
    getShellHistoryList: () => ['whoami', '/screenshot'], resetShellHistoryNav() {}, resetShellTabCycle() {},
    navigateShellHistory: () => { throw new Error('History must not handle menu navigation'); },
    api: async (...args) => { if (!(args[0] === '/creds' && args.length === 1)) calls.push(args); return { status: 200, data: {} }; },
    appendShellError: text => { errors.push(text); lines.push({kind: 'error', text}); }, rememberShellCommand: cmd => {
      if (history.at(-1) !== cmd) history.push(cmd);
    },
    appendShellLine: (kind, html) => { lines.push({kind, html}); lastShellLine = element(); return lastShellLine; }, escapeHtml: text => text,
    createCommandBlock: (cmd, options = {}) => {
      if (!anchorBlocks) { lines.push({kind: 'echo', html: cmd}); return null; }
      const line = {kind: 'block', html: cmd, children: []};
      const block = {filled: false, matchKeys: options.matchKeys || [cmd],
        resultEl: {appendChild: child => line.children.push(child)}};
      lines.push(line);
      context.state.pendingCommandBlocks.push(block);
      return block;
    }, scrollShellIfNearBottom() {}, setTimeout() {}, pollSessionEvents: async () => {},
  };
  vm.createContext(context);
  vm.runInContext(source.match(/const SHELL_META_COMMANDS = \[.*?\];/s)[0] +
    source.slice(source.indexOf('function shellTimestampHtml('), source.indexOf('function renderEventBodyHtml(')) +
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
  assert.equal(f.menu.children.length, 18); assert.equal(f.input.attrs['aria-expanded'], 'true');
  assert.ok(f.menu.children.some(x => x.textContent === '/whoami'));
  f.type('/s'); assert.deepEqual(f.menu.children.map(x => x.textContent), ['/screenshot', '/spawn', '/spawnas']);
  f.type('/sc'); assert.equal(f.menu.children.length, 1);
  f.type('/'); assert.equal(f.menu.children.length, 18);
  f.type('/unknown'); assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('who'); assert.equal(f.input.attrs['aria-expanded'], 'false');
});
test('history crosses slash commands without opening a menu and restores the draft', () => {
  const f = fixture();
  vm.runInContext(source.slice(source.indexOf('function resetShellHistoryNav('),
    source.indexOf('function longestCommonPrefix(')), f.context);
  f.context.getShellHistoryList = () => ['older command', '/screenshot', '/whoami', 'newer command'];
  f.context.resetShellHistoryNav();
  f.type('unfinished draft');
  for (const expected of ['newer command', '/whoami', '/screenshot', 'older command', 'older command']) {
    assert.equal(f.key('ArrowUp'), true);
    assert.equal(f.input.value, expected);
    assert.equal(f.input.attrs['aria-expanded'], 'false');
    // A focus or asynchronous credential refresh must not reopen the menu.
    f.context.updateShellCompletionHint();
    assert.equal(f.input.attrs['aria-expanded'], 'false');
  }
  for (const expected of ['/screenshot', '/whoami', 'newer command', 'unfinished draft']) {
    f.key('ArrowDown');
    assert.equal(f.input.value, expected);
    assert.equal(f.input.attrs['aria-expanded'], 'false');
  }
  f.key('ArrowUp');
  f.key('ArrowUp');
  f.type('/s');
  assert.equal(f.input.attrs['aria-expanded'], 'true');
  f.key('ArrowDown');
  assert.equal(f.input.value, '/s');
  assert.equal(f.input.attrs['aria-activedescendant'], 'shell-command-option-1');
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
    assert.deepEqual(JSON.parse(f.calls[0][1].body), { method: 'winrm', host: 'ws01', https: true, independent: true });
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
    assert.deepEqual(JSON.parse(f.calls[0][1].body), {method:'winrm',host:'target',https:true,insecure:true,independent:true});
  }
  for (const command of ['/lateral winrm target -k','/lateral psexec target -s -k','/lateral winrm target -s -k -k']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length,0); assert.match(f.errors[0],/Usage:/);
  }
});

test('loader flag forwards an explicit selection and rejects invalid values', async () => {
  const csharp = fixture(); csharp.type('/lateral winrm target -l csharp'); await csharp.context.runCommand(false);
  assert.deepEqual(JSON.parse(csharp.calls[0][1].body), {method:'winrm',host:'target',loader:'csharp',independent:true});
  const powershell = fixture(); powershell.type('/lateral wmiexec target --loader powershell'); await powershell.context.runCommand(false);
  assert.deepEqual(JSON.parse(powershell.calls[0][1].body), {method:'wmiexec',host:'target',loader:'powershell'});
  for (const command of ['/lateral winrm target -l', '/lateral winrm target -l csharp -l powershell']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 0);
    assert.match(f.errors[0], /Usage:/);
  }
  const unknown = fixture(); unknown.type('/lateral winrm target -l rustloader'); await unknown.context.runCommand(false);
  assert.equal(unknown.calls.length, 0);
  assert.match(unknown.errors[0], /Loader must be csharp or powershell/);
  const reserved = fixture();
  reserved.context.api = async (...args) => { reserved.calls.push(args); return { status: 400, data: { error: 'loader_reserved' } }; };
  reserved.type('/lateral winrm target -l powershell'); await reserved.context.runCommand(false);
  assert.equal(reserved.calls.length, 1);
  assert.match(reserved.errors.at(-1), /powershell loader is reserved/);
});

test('identity flags force the deployment identity for wmiexec and winrm only', async () => {
  const cred = fixture(); cred.type('/lateral wmiexec target -i cred-3f0cfe46'); await cred.context.runCommand(false);
  assert.deepEqual(JSON.parse(cred.calls[0][1].body), {method:'wmiexec',host:'target',identity_cred_id:'cred-3f0cfe46'});
  const direct = fixture(); direct.type('/lateral winrm target -u LAB\\alice -p pw'); await direct.context.runCommand(false);
  assert.deepEqual(JSON.parse(direct.calls[0][1].body),
    {method:'winrm',host:'target',independent:true,identity_domain:'LAB',identity_user:'alice',identity_password:'pw'});
  const userOnly = fixture(); userOnly.type('/lateral wmiexec target -u alice'); await userOnly.context.runCommand(false);
  assert.deepEqual(JSON.parse(userOnly.calls[0][1].body),
    {method:'wmiexec',host:'target',identity_domain:'',identity_user:'alice',identity_password:''});
  for (const command of [
    '/lateral psexec target -i cred-abc',
    '/lateral psexec target -u alice',
    '/lateral wmiexec target -i cred-abc -u alice',
    '/lateral winrm target -p pw',
    '/lateral winrm target -i',
    '/lateral winrm target -u',
    '/lateral wmiexec target -i cred-abc -i cred-def',
  ]) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 0, command);
    assert.match(f.errors[0], /Usage: \/lateral/);
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
test('uidx posts directly without arguments', async () => {
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const f = fixture(); f.type('/uidx'); await f.context.runCommand(false); await settle();
  assert.equal(f.calls[0][0], '/sessions/agent/uidx');
  assert.deepEqual(f.history, ['/uidx']);
  const rejected = fixture(); rejected.type('/uidx extra'); await rejected.context.runCommand(false);
  assert.equal(rejected.calls.length, 0);
  assert.match(rejected.errors[0], /Usage: \/uidx/);
});
test('identity results render only through session events, including failed operations', async () => {
  for (const command of ['/uid', '/uidx', '/make_token LAB\\alice test-password', '/rev2self']) {
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
      if (args[0] === '/creds') return {status: 200, data: {creds: []}};
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

test('a delayed valid result stays before a later invalid command', async () => {
  const f = fixture({anchorBlocks: true});
  let finish;
  f.context.api = async () => new Promise(resolve => { finish = resolve; });
  f.type('/creds purge');
  const valid = f.context.runCommand(false);
  assert.deepEqual(f.lines.map(line => line.kind), ['block']);
  f.type('/unknown');
  await f.context.runCommand(false);
  assert.deepEqual(f.lines.map(line => line.kind), ['block', 'echo', 'error']);
  finish({status: 200, data: {deleted: 2}});
  await valid;
  assert.equal(f.lines[0].children[0].innerHTML, 'deleted 2 credentials');
  assert.equal(f.lines[2].text, 'Unknown internal command. Type / for commands or /help for usage.');
});

test('a rejected queued slash command keeps its error in its original block', async () => {
  const f = fixture({anchorBlocks: true});
  let finish;
  f.context.api = async () => new Promise(resolve => { finish = resolve; });
  f.type('/download C:\\Lab\\file.txt');
  const queued = f.context.runCommand(false);
  f.type('/unknown');
  await f.context.runCommand(false);
  finish({status: 403, data: {error: 'denied'}});
  await queued;
  assert.deepEqual(f.lines.map(line => line.kind), ['block', 'echo', 'error']);
  assert.equal(f.lines[0].children[0].innerHTML, 'denied');
  assert.equal(f.context.state.pendingCommandBlocks[0].filled, true);

  f.context.api = async () => ({status: 200, data: {}});
  f.type('/download C:\\Lab\\file.txt');
  await f.context.runCommand(false);
  vm.runInContext(source.slice(source.indexOf('function normalizeCmdKey('), source.indexOf('function normalizeRemotePath(')), f.context);
  const resultBlock = f.context.findPendingCommandBlock({type: 'output', command: 'download C:\\Lab\\file.txt'});
  assert.equal(resultBlock, f.context.state.pendingCommandBlocks[1]);
});

test('a failed queue request closes its block without appending after an invalid command', async () => {
  const f = fixture({anchorBlocks: true});
  let fail;
  f.context.api = async () => new Promise((resolve, reject) => { fail = reject; });
  f.type('/screenshot');
  const queued = f.context.runCommand(false);
  f.type('/unknown');
  await f.context.runCommand(false);
  fail(new Error('offline'));
  await queued;
  assert.equal(f.lines[0].children[0].innerHTML, 'offline');
  assert.equal(f.context.state.pendingCommandBlocks[0].filled, true);
  assert.deepEqual(f.lines.map(line => line.kind), ['block', 'echo', 'error']);
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
    { type: 'output', body: 'RMM TUN relay enabled on rmm0', command: 'tun start rmm0' },
    { type: 'operator', body: 'tun: tun stop', command: 'tun stop' },
    { type: 'output', body: 'RMM TUN relay stopped', command: 'tun stop' },
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
test('tool result filtering preserves explicit shell submissions with the same text', () => {
  const c = historyFixture();
  assert.deepEqual(Array.from(c.rebuildShellHistoryFromEvents([
    { type: 'operator', body: 'tun: tun stop', command: 'tun stop' },
    { type: 'output', command: 'tun stop' },
    { type: 'operator', body: 'exec: tun stop', command: 'tun stop' },
    { type: 'output', command: 'tun stop' },
  ])), ['tun stop']);
});
test('history seeding removes persisted pivot labels but preserves explicit shell input', () => {
  const c = historyFixture({agent: ['tun start rmm0', '/uid', 'tun stop']});
  c.seedShellHistory('agent', [
    {type: 'operator', body: 'tun: tun start rmm0', command: 'tun start rmm0'},
    {type: 'output', command: 'tun start rmm0'},
    {type: 'operator', body: 'tun: tun stop', command: 'tun stop'},
    {type: 'operator', body: 'exec: tun stop', command: 'tun stop'},
  ]);
  assert.deepEqual(Array.from(c.state.shellHistoryBySession.agent), ['/uid', 'tun stop']);
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
    ['/creds list', '/creds?include_passwords=1', null],
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
  for (const command of ['/creds purge extra', '/creds list extra', '/creds add alice', '/creds add alice pass extra', '/creds del', '/creds del id extra']) {
    const f = fixture();
    await f.context.dispatchShellMetaCommand(command);
    assert.equal(f.calls.length, 0, command);
    assert.match(f.errors[0], /Usage:/);
  }
});

test('creds completes subcommands and displays argument hints without credential history', () => {
  const f = fixture();
  f.type('/creds ');
  assert.deepEqual(f.menu.children.map(x => x.textContent), ['/creds list', '/creds add', '/creds del', '/creds purge']);
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

test('winrm independent default and --tethered opt-out; scoped away from other methods', async () => {
  const plain = fixture(); plain.type('/lateral winrm target'); await plain.context.runCommand(false);
  assert.deepEqual(JSON.parse(plain.calls[0][1].body), {method:'winrm',host:'target',independent:true});
  const noop = fixture(); noop.type('/lateral winrm target --independent'); await noop.context.runCommand(false);
  assert.deepEqual(JSON.parse(noop.calls[0][1].body), {method:'winrm',host:'target',independent:true});
  const tethered = fixture(); tethered.type('/lateral winrm target --tethered'); await tethered.context.runCommand(false);
  assert.deepEqual(JSON.parse(tethered.calls[0][1].body), {method:'winrm',host:'target',independent:false});
  const secure = fixture(); secure.type('/lateral winrm target -s -k'); await secure.context.runCommand(false);
  assert.deepEqual(JSON.parse(secure.calls[0][1].body), {method:'winrm',host:'target',https:true,insecure:true,independent:true});
  for (const command of ['/lateral wmiexec target', '/lateral psexec target']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    const body = JSON.parse(f.calls[0][1].body);
    assert.ok(f.calls.length === 1);
    assert.ok(!('independent' in body), `${command} must not carry the winrm-scoped option`);
  }
  for (const command of ['/lateral wmiexec target --tethered', '/lateral psexec target --tethered', '/lateral winrm target --tethered --tethered']) {
    const f = fixture(); f.type(command); await f.context.runCommand(false);
    assert.equal(f.calls.length, 0);
    assert.match(f.errors[0], /Usage:/);
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
  assert.equal(f.hint.textContent, '/lateral wmiexec <host> [-l csharp|powershell] [-i cred_id | -u identity [-p password]]');
});

test('lateral filters methods, supports clicks, and leaves the host untouched', () => {
  const f = fixture();
  f.type('/lateral w');
  assert.deepEqual(f.menu.children.map(x => x.textContent), ['/lateral wmiexec', '/lateral winrm']);
  f.menu.children[1].listeners.mousedown({preventDefault() {}});
  assert.equal(f.input.value, '/lateral winrm ');
  assert.equal(f.hint.textContent, '/lateral winrm <host> [-s|--https] [-k|--insecure] [--tethered] [-l csharp|powershell] [-i cred_id | -u identity [-p password]]');
  f.context.getShellHistoryList = () => ['/lateral winrm ws01.old.example'];
  f.type('/lateral winrm ws'); f.key('Tab');
  assert.equal(f.input.value, '/lateral winrm ws');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
  f.type('/lateral smbexec');
  assert.equal(f.input.attrs['aria-expanded'], 'false');
});

test('creds list displays literal passwords and copies only the credential ID', async () => {
  const f = fixture();
  let row;
  f.context.appendShellLine = () => { row = element(); return row; };
  const copied = [];
  f.context.navigator = {clipboard: {writeText: async text => copied.push(text)}};
  const password = '<img src=x> & " spaced é';
  f.context.api = async () => ({status:200,data:{ok:true,creds:[{cred_id:'cred-123',label:'alice',password}]}});
  await f.context.dispatchShellMetaCommand('/creds list');
  assert.equal(f.errors.length, 0);
  const [idGroup, details] = row.children;
  const [idText, copy] = idGroup.children;
  assert.equal(idGroup.className, 'credential-id-group');
  assert.equal(idText.textContent, 'cred-123');
  assert.equal(copy.className, 'secondary credential-copy-id');
  assert.equal(details.textContent, '  alice  ' + password);
  assert.equal(details.innerHTML, undefined);
  assert.equal(copy.type, 'button');
  assert.equal(copy.attrs['aria-label'], 'Copy credential ID cred-123');
  await copy.listeners.click();
  assert.deepEqual(copied, ['cred-123']);
  assert.equal(copy.textContent, 'Copied');
  f.context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  await copy.listeners.click();
  assert.equal(copy.textContent, 'Copy failed');
});

for (const status of [200, 400]) {
  test(`lateral clears the prompt before its HTTP ${status} response and preserves the next draft`, async () => {
    const f = fixture();
    let resolve;
    const response = new Promise(done => { resolve = done; });
    f.context.api = () => response;
    f.type('/lateral psexec lab-host');
    const operation = f.context.runCommand(false);
    assert.equal(f.input.value, '');
    assert.equal(f.input.disabled, false);
    assert.deepEqual(f.history, ['/lateral psexec lab-host']);
    f.type('whoami');
    resolve({status, data: {ok: true, error: 'request failed'}});
    await operation;
    assert.equal(f.input.value, 'whoami');
  });
}


test('creds purge uses DELETE and reports counts or failures', async () => {
  for (const [status, deleted] of [[200, 2], [200, 0], [503, null]]) {
    const f = fixture(), output = [];
    f.context.api = async (...args) => { f.calls.push(args); return {status, data: {deleted}}; };
    f.context.appendShellOutput = text => output.push(text);
    await f.context.dispatchShellMetaCommand('/creds purge');
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], '/creds');
    assert.equal(f.calls[0][1].method, 'DELETE');
    if (status === 200) assert.deepEqual(output, [`deleted ${deleted} credentials`]);
    else { assert.equal(output.length, 0); assert.match(f.errors[0], /creds purge failed/); }
  }
});

test('credential completion fetches redacted IDs, filters and inserts IDs with labels', async () => {
  for (const command of ['/make_token', '/spawnas', '/runas', '/lateral wmiexec host', '/creds del']) {
    const f = fixture(), requests = [];
    f.context.api = async (...args) => {
      requests.push(args);
      return {status: 200, data: {creds: [
        {cred_id: 'abc1', label: 'LAB\\<alice>', password: 'never show'},
        {cred_id: 'xyz2', label: 'bob'},
      ]}};
    };
    f.type(command + ' -i a');
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(requests, [['/creds']]);
    assert.equal(f.menu.children.length, 1);
    assert.match(f.menu.children[0].textContent, /LAB\\<alice>/);
    assert.ok(!f.menu.children[0].textContent.includes('never show'));
    f.key('Tab');
    assert.equal(f.input.value, command + ' -i abc1 ');
  }
});

test('credential completion ignores remote commands and runas command arguments', () => {
  const f = fixture();
  for (const line of ['echo -i a', '/runas -i existing echo -i a', '/creds add user -i a', '/unknown -i a']) {
    assert.equal(f.context.shellCredentialContext(line), null);
  }
});

test('credential completion handles unavailable stores and late responses', async () => {
  const f = fixture();
  let finish;
  f.context.api = () => new Promise(resolve => { finish = resolve; });
  f.type('/spawnas -i ');
  f.type('/sc');
  finish({status: 503, data: {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.input.value, '/sc');
  assert.equal(f.menu.children[0].textContent, '/screenshot');
});

test('credential registry supports additional command paths and selector flags', () => {
  const f = fixture();
  vm.runInContext('CREDENTIAL_ID_COMPLETION_COMMANDS["custom action"] = ["--credential"]', f.context);
  const result = f.context.shellCredentialContext('/CUSTOM ACTION --credential abc');
  assert.equal(result.prefix, 'abc');
  assert.equal(result.base, '/CUSTOM ACTION --credential ');
  assert.equal(f.context.shellCredentialContext('/SPAWNAS -i abc').prefix, 'abc');
});

test('credential completion discards cached IDs when the connection changes', async () => {
  const f = fixture();
  f.context.api = async () => ({status: 200, data: {creds: [{cred_id: 'old-id'}]}});
  f.type('/spawnas -i ');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(f.menu.children[0].textContent, /old-id/);
  f.context.state.connectionGeneration = 1;
  f.context.api = async () => ({status: 200, data: {creds: [{cred_id: 'new-id'}]}});
  f.type('/spawnas -i ');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.menu.children.length, 1);
  assert.match(f.menu.children[0].textContent, /new-id/);
});

test('an old credential response cannot overwrite a new authentication context', async () => {
  const f = fixture(), pending = [];
  f.context.api = () => new Promise(resolve => pending.push(resolve));
  f.type('/spawnas -i ');
  f.context.state.authGeneration = 1;
  f.type('/spawnas -i ');
  pending[1]({status: 200, data: {creds: [{cred_id: 'current-id'}]}});
  await new Promise(resolve => setImmediate(resolve));
  pending[0]({status: 200, data: {creds: [{cred_id: 'previous-id'}]}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.menu.children.length, 1);
  assert.match(f.menu.children[0].textContent, /current-id/);
});

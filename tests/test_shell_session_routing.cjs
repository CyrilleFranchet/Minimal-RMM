const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function fixture() {
  const calls = [], rendered = [], errors = [], polls = [], nodes = new Map();
  const pending = deferred();
  const state = {selectedId:'a', selectionGeneration:1, viewMode:'live', pendingCommandBlocks:[]};
  const input = {value:'echo original', disabled:false, focus(){}};
  nodes.set('#shell-input', input);
  const context = vm.createContext({state, Uint8Array, btoa: s => Buffer.from(s, 'binary').toString('base64'),
    $: key => nodes.get(key),
    api: async (path, options) => { calls.push({path, body:JSON.parse(options.body)}); return pending.promise; },
    clearShellInput: () => { input.value = ''; }, appendShellEcho: (...args) => rendered.push(args),
    appendEvent: ev => rendered.push(ev), appendShellError: error => errors.push(error),
    pollSessionEvents: async () => { polls.push(state.selectedId); }, parseShellWords: cmd => cmd.split(' '),
  });
  vm.runInContext(source.slice(source.indexOf('function captureShellTarget('), source.indexOf('async function queueDownload(')) +
    source.slice(source.indexOf('async function queueUpload('), source.indexOf('async function applyBeaconConfig(')), context);
  const switchSession = id => { state.selectedId = id; state.selectionGeneration++; input.value = 'draft for ' + id; input.disabled = true; rendered.length = 0; };
  return {context, calls, rendered, errors, polls, nodes, pending, state, input, switchSession};
}
for (const wait of [false, true]) {
  test(`command ${wait ? 'exec' : 'queue'} retains target through dispatch yield and ignores stale reply`, async () => {
    const f = fixture();
    const operation = f.context.runCommand(wait);
    f.switchSession('b');
    await Promise.resolve();
    assert.equal(f.calls[0].path, `/sessions/a/${wait ? 'exec' : 'commands'}`);
    assert.equal(f.calls[0].body.command, 'echo original');
    f.pending.resolve({status:200, data:{event:{id:1, body:'from a'}}});
    await operation;
    assert.equal(f.rendered.length, 0);
    assert.equal(f.errors.length, 0);
    assert.equal(f.input.value, 'draft for b');
    assert.equal(f.input.disabled, true);
  });
}
test('exec response recovers ordered events without skipping older commands', async () => {
  const f = fixture();
  const operation = f.context.runCommand(true);
  await Promise.resolve();
  f.pending.resolve({status:200, data:{event:{id:1, body:'from a'}}});
  await operation;
  assert.deepEqual(f.polls, ['a']);
  assert.equal(f.rendered.some(event => event.body === 'from a'), false);
  assert.equal(f.input.disabled, false);
});
test('returning to same session creates a new view and cannot receive old request UI mutations', async () => {
  const f = fixture(); const operation = f.context.runCommand(true);
  await Promise.resolve(); f.switchSession('b'); f.switchSession('a');
  f.pending.resolve({status:408, data:{}}); await operation;
  assert.equal(f.errors.length,0); assert.equal(f.input.value,'draft for a');
});
test('upload reading a file retains original destination after session switch', async () => {
  const f = fixture(); const fileRead = deferred();
  const upload = {files:[{name:'fixture.txt', arrayBuffer:()=>fileRead.promise}], value:'a-file'};
  const remote = {value:'fixture.txt'};
  f.nodes.set('#upload-file',upload); f.nodes.set('#upload-remote',remote);
  const operation = f.context.queueUpload(); f.switchSession('b');
  upload.value = 'b-file'; remote.value = 'b-path';
  fileRead.resolve(new Uint8Array([65]).buffer); await new Promise(setImmediate);
  assert.equal(f.calls[0].path,'/sessions/a/upload');
  assert.equal(f.calls[0].body.remote_path,'fixture.txt');
  f.pending.resolve({status:400,data:{error:'a error'}}); await operation;
  assert.equal(f.errors.length,0); assert.equal(upload.value,'b-file'); assert.equal(remote.value,'b-path');
});
for (const [fn,args,suffix] of [['postDownloadQueue',['file'],'download'],['postExfilQueue',['file','profile'],'exfil'],['postScreenshotQueue',[],'screenshot'],['postPeQueue',['plugin','Run',''],'pe']]) {
  test(`${suffix} queue cannot write errors in a different shell`, async () => {
    const f=fixture(); const operation=f.context[fn](...args); f.switchSession('b');
    assert.equal(f.calls[0].path,`/sessions/a/${suffix}`);
    f.pending.resolve({status:400,data:{error:'a error'}}); await operation;
    assert.equal(f.errors.length,0); assert.equal(f.rendered.length,0);
  });
}

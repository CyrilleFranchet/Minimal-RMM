const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../web/agent-gen.js'), 'utf8');
const template = fs.readFileSync(path.join(__dirname, '../agent-powershell/client_rmm.ps1'), 'utf8');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; }
function element(value = '') {
  const listeners = {}, classes = new Set();
  return {value, checked:false, disabled:false, textContent:'', open:true, listeners,
    classList:{toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }},
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelectorAll() { return []; }, click() {}, remove() {}, select() {},
  };
}
function fixture({mode='script', requested='', owner='alice', useApi=true} = {}) {
  const nodes = new Map();
  for (const id of ['agent-gen-panel','agent-gen-output','agent-gen-run','agent-gen-hint','agent-gen-copy-script',
    'agent-gen-download-script','agent-gen-copy-run','agent-server-url','agent-beacon-secret','agent-workspace-id',
    'agent-session-id','agent-sleep','agent-jitter','agent-max-retries']) nodes.set('#'+id, element());
  nodes.get('#agent-server-url').value = 'https://beacon.example.test';
  nodes.get('#agent-beacon-secret').value = 'fixture-beacon';
  nodes.get('#agent-workspace-id').value = requested;
  const storage = new Map([['rmm_api_token','token-a']]);
  const calls = [], copies = [], downloads = [];
  const window = {rmmState:{authGeneration:1,connectionGeneration:1}, location:{origin:'https://operator.example.test'}};
  const context = {window, console, setTimeout, crypto:{randomUUID:()=> 'fixture-session-id'},
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    document:{querySelector(selector) {
      if (selector === 'input[name="agent-output-mode"]:checked') return {value:mode};
      if (selector === 'input[name="agent-session-mode"]:checked') return {value:'new'};
      return nodes.get(selector) || null;
    },querySelectorAll(){return [];}, createElement(){return {click(){downloads.push(true);},remove(){}};},body:{appendChild(){}},execCommand(){}},
    navigator:{clipboard:{async writeText(text){copies.push(text);}}},
    Blob:class Blob {}, URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},
  };
  let apiHandler = async route => ({status:200,data:route === '/health' ? {workspace_id:owner} : {content:template}});
  if (useApi) window.rmmApi = async route => {calls.push(route); return apiHandler(route);};
  else context.fetch = async (route, options) => {
    calls.push([route, options]);
    const response = await apiHandler(route.replace('/api/v1',''));
    return {ok:response.status === 200,status:response.status,json:async()=>response.data};
  };
  vm.createContext(context); vm.runInContext(source, context);
  window.initAgentGenerator();
  return {window,nodes,storage,calls,copies,downloads,context,
    setApi(handler) { apiHandler = handler; },
    output:()=>nodes.get('#agent-gen-output').value,
    regenerate() { window.refreshAgentScriptTemplate(); },
    switchOwner(token='token-b') {storage.set('rmm_api_token',token); window.rmmState.authGeneration++;},
  };
}

test('all PowerShell output modes use the authenticated workspace instead of stale preferences or input', async () => {
  for (const mode of ['script','config','env']) {
    const f = fixture({mode,requested:'other-workspace'}); await settle();
    assert.equal(f.nodes.get('#agent-workspace-id').value,'alice');
    assert.ok(f.calls.includes('/health'));
    if (mode === 'env') assert.match(f.output(), /\$env:RMM_AGENT_WORKSPACE_ID = 'alice'/);
    else assert.match(f.output(), /\$workspaceId = 'alice'/);
    assert.doesNotMatch(f.output(), /other-workspace/);
    assert.equal(JSON.parse(f.storage.get('rmm_agent_gen_prefs')).workspaceId,'alice');
  }
});

test('administrators can select a syntactically valid workspace and invalid identifiers produce no artifact', async () => {
  const f = fixture({mode:'config',owner:null,requested:'team.lab-1'}); await settle();
  assert.match(f.output(), /\$workspaceId = 'team.lab-1'/);
  const invalid = fixture({mode:'env',owner:null,requested:'../workspace'}); await settle();
  assert.match(invalid.output(), /^# Workspace ID/);
  assert.doesNotMatch(invalid.output(), /RMM_AGENT_WORKSPACE_ID/);
});

test('fallback health and template fetches use the captured API token', async () => {
  const f = fixture({useApi:false}); await settle();
  assert.deepEqual(f.calls.map(call=>call[0]),['/api/v1/health','/api/v1/agent/script']);
  assert.ok(f.calls.every(call=>call[1].headers.Authorization === 'Bearer token-a'));
  assert.match(f.output(), /\$workspaceId = 'alice'/);
});

test('an old health response cannot publish or cache a binding after authentication changes', async () => {
  const f = fixture({mode:'config'}); await settle();
  const old = deferred();
  f.setApi(route => old.promise);
  f.regenerate();
  f.switchOwner();
  f.setApi(async route=>({status:200,data:route === '/health' ? {workspace_id:'bob'} : {content:template}}));
  f.regenerate(); await settle();
  assert.match(f.output(), /\$workspaceId = 'bob'/);
  old.resolve({status:200,data:{workspace_id:'alice'}}); await settle();
  assert.match(f.output(), /\$workspaceId = 'bob'/);
  assert.equal(f.nodes.get('#agent-workspace-id').value,'bob');
});

test('a previous connection template cannot overwrite a later connection output', async () => {
  const f = fixture(); await settle();
  const old = deferred();
  f.setApi(async route => route === '/health' ? {status:200,data:{workspace_id:'alice'}} : old.promise);
  f.regenerate(); await settle();
  f.window.rmmState.connectionGeneration++;
  f.setApi(async route=>({status:200,data:route === '/health' ? {workspace_id:'bob'} : {content:template+'\n# current-template'}}));
  f.regenerate(); await settle();
  assert.match(f.output(), /\$workspaceId = 'bob'/);
  assert.match(f.output(), /current-template/);
  old.resolve({status:200,data:{content:template+'\n# stale-template'}}); await settle();
  assert.match(f.output(), /current-template/);
  assert.doesNotMatch(f.output(), /stale-template/);
});

test('copy and download cannot export output from a previous authentication context', async () => {
  const f = fixture(); await settle();
  await f.nodes.get('#agent-gen-download-script').listeners.click();
  assert.equal(f.downloads.length,1); // Real templates begin with a comment and remain downloadable.
  f.switchOwner();
  f.setApi(async route=>({status:200,data:route === '/health' ? {workspace_id:'bob'} : {content:template}}));
  await f.nodes.get('#agent-gen-copy-script').listeners.click(); await settle();
  assert.equal(f.copies.length,0);
  assert.match(f.output(), /\$workspaceId = 'bob'/);
  await f.nodes.get('#agent-gen-copy-script').listeners.click();
  assert.equal(f.copies.length,1);
  assert.match(f.copies[0], /\$workspaceId = 'bob'/);
});

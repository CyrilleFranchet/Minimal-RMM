const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');

test('shell Download file fetches authenticated bytes and keeps blob URL alive', async () => {
  const timers = [], requests = [], errors = [];
  let handler, clicks = 0, revoked = 0;
  const link = {dataset:{artifactUrl:'/api/v1/artifacts/downloads/file', downloadName:'Logfile.PML'}, textContent:'Download file'};
  const context = vm.createContext({state:{token:'secret'},
    fetch:async (url, options) => {requests.push({url, options}); return {ok:true, blob:async()=>({})};},
    URL:{createObjectURL:()=> 'blob:test', revokeObjectURL:()=>revoked++},
    setTimeout:(callback, delay)=>timers.push({callback, delay}),
    document:{body:{appendChild(){}}, createElement:()=>({click(){clicks++; assert.equal(revoked,0); assert.equal(this.download,'Logfile.PML');},remove(){}})},
    $:()=>({addEventListener:(name, callback)=>handler=callback}),
    appendShellError:message=>errors.push(message),
  });
  vm.runInContext(source.slice(source.indexOf('async function fetchArtifact('),source.indexOf('\nfunction show(')), context);
  const start=source.indexOf('  $("#shell-output").addEventListener("click"');
  vm.runInContext(source.slice(start, source.indexOf('\n  document.addEventListener("visibilitychange"',start)),context);
  const event={target:{closest:selector=>selector==='a.artifact-download'?link:null}, preventDefault(){}};
  handler(event);
  assert.equal(link.textContent,'Preparing download…');
  handler(event);
  await new Promise(setImmediate);
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,link.dataset.artifactUrl);
  assert.equal(requests[0].options.headers.Authorization,'Bearer secret');
  assert.equal(clicks,1);
  assert.equal(revoked,0);
  assert.equal(link.textContent,'Download file');
  assert(timers[0].delay>=1000);
  timers[0].callback();
  assert.equal(revoked,1);
  context.fetch=async()=>({ok:false,status:404,json:async()=>({error:'missing file'})});
  handler(event);
  await new Promise(setImmediate);
  assert.match(errors[0],/404.*missing file/);
  assert.equal(link.dataset.downloading,undefined);
});

test('workspace progress renders without changing durable event pagination', async()=>{
  const rendered=[], calls=[];
  const context=vm.createContext({api:async path=>{calls.push(path); return {status:200,data:{events:[{id:4,type:'output'}],progress:[{type:'download_progress',body:'{}'}]}};},disconnect(){}});
  vm.runInContext(source.slice(source.indexOf('async function readShellEvents('),source.indexOf('\nasync function pollSessionEvents')),context);
  await context.readShellEvents('/sessions/id/events',3,()=>true,event=>rendered.push(event));
  assert.equal(rendered.length,2);
  assert.equal(rendered[1].type,'download_progress');
  assert.equal(rendered[1].id,undefined);
  assert.equal(calls.length,1);
});

test('Received age uses absolute timestamps across browser time zones',()=>{
  const originalZone=process.env.TZ;
  class Clock extends Date { static now(){return Date.parse('2026-11-23T12:00:00Z');} }
  try {
    for (const zone of ['Europe/Paris','America/Los_Angeles','UTC']) {
      process.env.TZ=zone;
      const context=vm.createContext({Date:Clock});
      vm.runInContext(source.slice(source.indexOf('function formatAgoFromIso('),source.indexOf('\nasync function refreshGlobalArtifacts')),context);
      vm.runInContext(source.slice(source.indexOf('function formatAgo(seconds)'),source.indexOf('\nfunction statusClass')),context);
      assert.equal(context.formatAgoFromIso('2026-11-23T12:00:00+00:00'),'0s ago',zone);
      assert.equal(context.formatAgoFromIso('2026-11-23T13:00:00+01:00'),'0s ago',zone);
      assert.equal(context.formatAgoFromIso('2026-11-23T11:59:01Z'),'59s ago',zone);
    }
  } finally { if(originalZone===undefined) delete process.env.TZ; else process.env.TZ=originalZone; }
});

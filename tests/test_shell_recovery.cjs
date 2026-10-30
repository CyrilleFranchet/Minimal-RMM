const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const functions = source.slice(source.indexOf('async function readShellEvents('), source.indexOf('\nfunction startEventPolling'));
(async () => {
  const events = Array.from({length: 1205}, (_, i) => ({id:i + 1, body:`output ${i}`}));
  const rendered = [];
  const calls = [];
  const state = { selectedId:'a', token:'token', viewMode:'live', selectionGeneration:1, lastEventId:0 };
  let changeSelection = false;
  const context = vm.createContext({state, WORKSPACE_MODE:false, disconnect(){},
    appendEvent(event) { if (!rendered.includes(event.id)) rendered.push(event.id); state.lastEventId = Math.max(state.lastEventId,event.id); },
    api: async path => {
      calls.push(path);
      const since = Number(new URL(path, 'http://local').searchParams.get('since'));
      if (changeSelection) state.selectedId = 'b';
      return {status:200, data:{events:events.filter(e=>e.id>since).slice(0,500).reverse()}};
    }
  });
  vm.runInContext(functions + source.slice(source.indexOf('async function handleLiveEvent('), source.indexOf('\nfunction disconnectWebSocket')), context);
  await context.handleLiveEvent({ id:1205, body:'truncated', body_truncated:true }, 'a');
  assert.deepEqual(rendered, events.map(e=>e.id), 'all pages recovered in order');
  assert.equal(calls.length,3);
  assert(calls.every(path=>path.includes('forward=1')));
  state.lastEventId = 0;
  rendered.length = 0;
  state.shellRecovering = true;
  await context.handleLiveEvent({id:1205}, 'a');
  assert.equal(rendered.length, 0, 'live notifications cannot race initial recovery');
  state.shellRecovering = false;
  changeSelection = true;
  state.lastEventId = 0;
  rendered.length = 0;
  await context.pollSessionEvents();
  assert.equal(rendered.length,0,'stale session response is discarded');
  console.log('Shell recovery tests passed');
})();

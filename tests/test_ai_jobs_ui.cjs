// Exercise asynchronous AI job recovery using the production UI functions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/ai.js'), 'utf8');
function setup(api) {
  const nodes = new Map();
  const storage = new Map();
  function element() {
    return { children: [], dataset: {}, value: '', disabled: false, classList: { toggle() {} },
      querySelector() { return this.children.find(node => node.type === 'button') || null; },
      appendChild(node) { this.children.push(node); if (node.id) nodes.set('#' + node.id, node); },
      replaceChildren() { this.children = []; nodes.delete('#ai-job-status'); },
      setAttribute() {}, addEventListener(name, callback) { this[name] = callback; } };
  }
  for (const key of ['#ai-chat-log', '#ai-chat-input', '#ai-send-btn', '#ai-chat-reset-btn', '#ai-provider-select', '#openai-key-input', '#ai-model-select']) nodes.set(key, element());
  nodes.get('#ai-provider-select').value = 'openai';
  nodes.get('#openai-key-input').value = 'test-key';
  nodes.get('#ai-model-select').value = 'test-model';
  const localStorage = { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v) };
  const window = { rmmState: { token: 'operator', selectedId: 'a' }, rmmApi: api };
  const context = vm.createContext({ window, document: { querySelector: s => nodes.get(s) || null, createElement: element },
    localStorage, sessionStorage: localStorage, crypto, setTimeout: () => 1, clearTimeout() {}, confirm: () => true });
  const exposed = source.replace(/\}\)\(\);\s*$/, `window.test = { syncAiChatWithSession, sendAiMessage, refreshAiJob, resetAiChatForCurrentSession,
    state: () => ({ history: chatHistory, token: chatLoadToken, sending }), jobState }; })();`);
  vm.runInContext(exposed, context);
  return { window, test: window.test, nodes, storage };
}
(async () => {
  const calls = [];
  let jobs = [];
  let failPost = true;
  const ui = setup(async (path, options = {}) => {
    calls.push({ path, options });
    if (options.method === 'POST') {
      if (failPost) throw Error('connection dropped');
      return { status: 202, data: { job: jobs[0] } };
    }
    if (path.includes('/sessions/')) return { status: 200, data: { messages: [] } };
    return { status: 200, data: path.includes('?') ? { jobs } : { job: jobs[0] } };
  });
  await ui.test.syncAiChatWithSession('a');
  await ui.test.sendAiMessage('Do the task');
  assert.equal(ui.test.state().history.length, 1, 'lost POST retains user history');
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  const request = JSON.parse(calls.find(c => c.options.method === 'POST').options.body);
  jobs = [{ id: 'older', request_id: 'old-request', session_id: 'a', status: 'succeeded', created_at: new Date(Date.now() - 10000).toISOString(), result: { message: 'Old answer' } }];
  await ui.test.refreshAiJob('a', ui.test.state().token);
  assert.equal(ui.test.state().sending, true, 'an older task cannot resolve unknown submission');
  assert.equal(ui.test.state().history.length, 1, 'old completion must not replace unresolved user turn');
  const retry = ui.nodes.get('#ai-job-status').children[0];
  assert.equal(retry.textContent, 'Retry the same submission');
  await ui.test.refreshAiJob('a', ui.test.state().token);
  assert.equal(ui.nodes.get('#ai-job-status').children[0], retry, 'polling preserves keyboard focus on the retry control');
  assert.equal(ui.nodes.get('#ai-job-status').children.length, 1, 'polling does not duplicate retry buttons');
  jobs = [{ id: 'job-a', request_id: request.request_id, session_id: 'a', status: 'running', created_at: new Date().toISOString(), progress: { phase: 'tool', round: 2, tool: 'list_sessions', status: 'started' } }];
  failPost = false;
  await retry.click();
  // The click handler starts an async request; allow its continuation to run.
  await Promise.resolve();
  const submissions = calls.filter(c => c.options.method === 'POST');
  assert.equal(JSON.parse(submissions[1].options.body).request_id, request.request_id, 'manual retry keeps idempotency key');
  await ui.test.refreshAiJob('a', ui.test.state().token);
  assert.equal(ui.test.state().sending, true);
  assert(ui.nodes.get('#ai-job-status').textContent.includes('tool · round 2 · list_sessions · started'));
  jobs[0].status = 'succeeded';
  jobs[0].result = { message: 'Done', chat_messages: [{ role: 'user', content: 'Do the task' }, { role: 'assistant', content: 'Done' }] };
  await ui.test.refreshAiJob('a', ui.test.state().token);
  await ui.test.refreshAiJob('a', ui.test.state().token);
  assert.equal(ui.test.state().history.length, 2, 'repeated recovery does not duplicate final response');
  assert.equal(ui.test.state().sending, false);
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 2, 'polling never resubmits');
  assert(![...ui.storage.values()].join('').includes('test-key'), 'job recovery state contains no API key');

  // Late response from the old session must never overwrite the selected chat.
  let completeOld;
  const oldToken = ui.test.state().token;
  ui.window.rmmApi = path => path.includes('/job-a') ? new Promise(resolve => { completeOld = resolve; })
    : Promise.resolve({ status: 200, data: path.includes('/sessions/') ? { messages: [] } : { jobs: [] } });
  const late = ui.test.refreshAiJob('a', oldToken, 'job-a');
  ui.window.rmmState.selectedId = 'b';
  await ui.test.syncAiChatWithSession('b');
  completeOld({ status: 200, data: { job: jobs[0] } });
  await late;
  assert.equal(ui.test.state().history.length, 0, 'old session completion is ignored');
  assert.equal(ui.test.state().sending, false);

  const reloaded = setup(async path => ({ status: 200, data: path.includes('/sessions/') ? { messages: [] } : { jobs } }));
  await reloaded.test.syncAiChatWithSession('a');
  assert.equal(reloaded.test.state().history.length, 2, 'a fresh browser recovers the full server transcript');
  jobs[0].status = 'interrupted';
  await reloaded.test.refreshAiJob('a', reloaded.test.state().token);
  assert.equal(reloaded.test.state().sending, false, 'interrupted jobs stop polling and allow a new operator request');
  assert(reloaded.nodes.get('#ai-job-status').textContent.includes('not automatically replayed'));

  const dismissed = setup(async path => ({ status: 200, data: path.includes('/sessions/') ? { messages: [] } : { jobs: [{ ...jobs[0], dismissed: true }] } }));
  await dismissed.test.syncAiChatWithSession('a');
  assert.equal(dismissed.test.state().history.length, 0, 'reset chats do not resurrect dismissed jobs');
  console.log('AI job UI recovery checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

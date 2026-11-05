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
    return { children: [], dataset: {}, value: '', disabled: false, classList: { toggle() {}, add() {}, remove() {} },
      querySelector() { return this.children.find(node => node.type === 'button') || null; },
      appendChild(node) { this.children.push(node); if (node.id) nodes.set('#' + node.id, node); },
      replaceChildren() { this.children = []; nodes.delete('#ai-job-status'); },
      setAttribute() {}, addEventListener(name, callback) { this[name] = callback; } };
  }
  for (const key of ['#ai-chat-log', '#ai-chat-input', '#ai-send-btn', '#ai-stop-btn', '#ai-chat-reset-btn', '#ai-provider-select', '#openai-key-input', '#ai-model-select']) nodes.set(key, element());
  nodes.get('#ai-provider-select').value = 'openai';
  nodes.get('#openai-key-input').value = 'test-key';
  nodes.get('#ai-model-select').value = 'test-model';
  const localStorage = { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k) };
  const window = { rmmState: { token: 'operator', selectedId: 'a' }, rmmApi: api };
  const context = vm.createContext({ window, document: { querySelector: s => nodes.get(s) || null, createElement: element },
    localStorage, sessionStorage: localStorage, crypto, setTimeout: () => 1, clearTimeout() {}, confirm: () => true });
  const exposed = source.replace(/\}\)\(\);\s*$/, `window.test = { syncAiChatWithSession, sendAiMessage, refreshAiJob, resetAiChatForCurrentSession,
    formatAiFailureDetails, displayAiJob, loadEnabledSkillIds, state: () => ({ history: chatHistory, token: chatLoadToken, sending }), jobState }; })();`);
  vm.runInContext(exposed, context);
  return { window, test: window.test, nodes, storage };
}
(async () => {
  const diagnosticUi = setup(async () => ({ status: 200, data: { messages: [] } }));
  diagnosticUi.storage.set('rmm_ai_skills_enabled', JSON.stringify(['ad-collection-analysis', 'bloodhound-analysis']));
  assert.deepEqual(Array.from(diagnosticUi.test.loadEnabledSkillIds()), ['bloodhound-ce-python-collection', 'bloodhound-analysis']);
  const webTopics = ['sqli', 'xss', 'ssrf', 'ssti', 'xxe', 'idor', 'file-upload', 'rce',
    'deserialization', 'race-condition', 'request-smuggling', 'open-redirect',
    'parameter-pollution', 'graphql', 'waf-bypass', 'business-logic'];
  diagnosticUi.storage.set('rmm_ai_skills_enabled', JSON.stringify([...webTopics.map(t => 'offensive-' + t), 'offensive-netexec', 'web-sqli']));
  assert.deepEqual(Array.from(diagnosticUi.test.loadEnabledSkillIds()), [...webTopics.map(t => 'web-' + t), 'offensive-netexec']);
  await diagnosticUi.test.syncAiChatWithSession('a');
  const failed = { id: 'job-failed', status: 'failed', progress: { phase: 'model', round: 7 },
    result: { message: 'Partial report', diagnostics: { exception_chain: [
      { type: 'HTTPError', http_status: 429, traceback_lines: [55], message: 'secret-token' }
    ] } } };
  diagnosticUi.test.displayAiJob(failed, 'a', diagnosticUi.test.state().token);
  assert.equal(diagnosticUi.test.state().history.at(-1).content, 'Partial report');
  const details = diagnosticUi.test.formatAiFailureDetails(failed);
  assert.match(details, /HTTP 429/);
  assert.match(details, /Last recorded round: 7/);
  assert.ok(!details.includes('secret-token'));
  const budgetDetails = diagnosticUi.test.formatAiFailureDetails({ result: {
    error: 'context_budget_exceeded', context_budget: {
      estimated_input_upper_bound: 70000, effective_input_limit: 59392,
      tool_schema_bytes: 23000, system_bytes: 35000, provider_text: 'private-token'
    }
  } });
  assert.match(budgetDetails, /70000 \/ 59392/);
  assert.match(budgetDetails, /Tool schema bytes: 23000/);
  assert.ok(!budgetDetails.includes('private-token'));
  const outputDetails = diagnosticUi.test.formatAiFailureDetails({ result: {
    error: 'provider_output_truncated', diagnostics: { provider_output: {
      limit: 4096, completion_tokens: 4096, reasoning_tokens: 4000, retries: 2, private: 'private-token'
    } }
  } });
  assert.match(outputDetails, /Per-response output limit: 4096 tokens/);
  assert.match(outputDetails, /Reasoning tokens: 4000/);
  assert.match(outputDetails, /Output recovery retries: 2/);
  assert.ok(!outputDetails.includes('private-token'));
  diagnosticUi.test.displayAiJob(failed, 'a', diagnosticUi.test.state().token);
  assert.equal(diagnosticUi.test.state().history.filter(m => m.content === 'Partial report').length, 1);
  diagnosticUi.test.displayAiJob({ id: 'step-job', status: 'running', progress: {
    tool_path: 'mcp', mcp_status: 'connected', exegol_status: 'disabled', step: 2,
    step_summary: 'Inspect <file> evidence', active_skills: ['file-review'], skills: ['file-review']
  } }, 'a', diagnosticUi.test.state().token);
  assert.match(diagnosticUi.nodes.get('#ai-job-execution').textContent, /Step 2: Inspect <file> evidence/);
  assert.match(diagnosticUi.nodes.get('#ai-job-execution').textContent, /Active skills: file-review/);
  let stopPath;
  const stopUi = setup(async (path, options) => {
    if (options?.method === 'POST') {
      stopPath = path;
      return { status: 200, data: { job: { id: 'stop-job', status: 'interrupted' } } };
    }
    return { status: 200, data: { messages: [] } };
  });
  await stopUi.test.syncAiChatWithSession('a');
  stopUi.test.displayAiJob({ id: 'stop-job', status: 'running' }, 'a', stopUi.test.state().token);
  const stopButton = stopUi.nodes.get('#ai-stop-btn');
  assert.equal(stopButton.textContent, 'Stop');
  await stopButton.onclick();
  assert.equal(stopPath, '/ai/jobs/stop-job/cancel');
  assert.equal(stopUi.test.state().sending, false);
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
  ui.storage.set('rmm_exegol_mcp_token', 'legacy-browser-token');
  ui.storage.set('rmm_exegol_mcp_enabled', '0');
  await ui.test.sendAiMessage('Do the task');
  assert.equal(ui.test.state().history.length, 1, 'lost POST retains user history');
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 1);
  const request = JSON.parse(calls.find(c => c.options.method === 'POST').options.body);
  assert.equal(request.exegol_mcp_enabled, undefined, "backend selection belongs to the service and skill policy");
  assert.equal(request.exegol_mcp_url, undefined);
  assert.equal(request.exegol_mcp_token, undefined);
  assert.equal(ui.storage.has('rmm_exegol_mcp_token'), false);
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

// Exercise the real dialog's lifecycle, safe projection, and protected evidence reads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/ai-operation-details.js'), 'utf8');
const document = { activeElement: null };
function element(tag) {
  return { tag, children: [], dataset: {}, attributes: {}, open: false, scrollTop: 0,
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(name, handler) { if (name === 'close') this.closeListener = handler; else this[name] = handler; },
    focus() { document.activeElement = this; },
    showModal() { this.open = true; }, close() { this.open = false; this.closeListener?.(); },
    querySelectorAll(selector) {
      const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      return descendants.filter(child => selector === '*' || selector === child.tag || (selector === 'details[open]' && child.tag === 'details' && child.open));
    },
  };
}
document.createElement = element;
document.body = element('body');
let copied, requests = [], response = { status: 200, data: { evidence: { content: '<script>credential</script>', characters: 27, offset: 0, next_offset: null } } };
const window = { rmmState: { authGeneration: 1, connectionGeneration: 1 }, rmmApi: async path => { requests.push(path); return response; } };
const timers = new Map(); let timerId = 0;
vm.runInNewContext(source, { window, document, setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id), navigator: { clipboard: { writeText: async text => { copied = text; } } } });
async function poll() { const [id, callback] = timers.entries().next().value; timers.delete(id); return callback(); }
const api = window.rmmAiOperationDetails;
const all = () => document.body.querySelectorAll('*');
const find = (tag, text) => all().find(node => node.tag === tag && node.textContent === text);
const contains = text => all().some(node => typeof node.textContent === 'string' && node.textContent.includes(text));
const job = { id: 'job-1', session_id: 'session-1', status: 'running', created_at: '2026-11-06T09:00:00Z', progress: { round: 48, tool: 'execute_command_in_container', secret: 'hidden' },
  activity_omitted: 3,
  activity: [{ timestamp: '2026-11-06T09:00:01Z', phase: 'tool', status: 'running', tool: 'execute_command_in_container', call_id: 'call-1', arguments_artifact_id: 'context-123', arguments: { password: 'hidden' } }],
  result: { diagnostics: { exception_chain: [{ type: 'ExceptionGroup', message: 'hidden', traceback_lines: [123] }, { type: 'TimeoutError', traceback_lines: [5] }] } }, token: 'hidden' };
(async () => {
  api.show(job);
  assert(document.body.children[0].open, 'native modal opened');
  assert(contains('3 older events'));
  assert(!contains('hidden'), 'unknown/raw fields excluded');
  assert.equal(requests.length, 0, 'evidence never auto-fetched');
  await find('button', 'Copy diagnostics').click();
  assert(!copied.includes('hidden'));
  assert(copied.includes('TimeoutError'));
  const evidenceButton = find('button', 'Load command arguments');
  await evidenceButton.click();
  assert.match(requests[0], /\/ai\/jobs\/job-1\/evidence\/context-123\?offset=0&limit=4096/);
  assert(contains('<script>credential</script>'), 'protected content is plain text');
  assert.equal(evidenceButton.disabled, true, 'null next_offset ends preview');
  await find('button', 'Copy diagnostics').click();
  assert(!copied.includes('credential'), 'loaded protected content excluded from copy');
  const details = all().find(node => node.tag === 'details');
  details.open = true;
  const summary = details.children[0]; summary.focus();
  api.remember({ ...job, status: 'failed' });
  assert(contains('Outcome unknown (no final event recorded)'), 'terminal start without finish stays unknown');
  assert(all().some(node => node.tag === 'details' && node.open), 'expanded events survive refresh');
  assert.equal(document.activeElement.dataset.event, summary.dataset.event, 'summary focus survives refresh');
  find('button', 'Close').click();
  assert(!document.body.children[0].open);
  api.show({ id: 'old-job', status: 'failed' });
  assert(contains('Older jobs cannot reconstruct missing events'));
  api.reset();
  assert(!document.body.children[0].open);
  api.show(job);
  response = { status: 403, data: {} };
  await find('button', 'Load command arguments').click();
  assert(find('button', 'Could not load command arguments. Retry'), 'evidence read failure visible and retryable');
  response = { status: 200, data: { evidence: { content: '😀', characters: 2, next_offset: 1 } } };
  await find('button', 'Could not load command arguments. Retry').click();
  response = { status: 200, data: { evidence: { content: 'x', characters: 2, next_offset: null } } };
  await find('button', 'Load more command arguments').click();
  assert.match(requests.at(-1), /offset=1&limit=4096/, 'pagination uses server Unicode cursor, not JS string length');
  assert(contains('😀x'));
  api.reset(); api.show(job);
  let rejectEvidence, evidenceRequests = 0;
  window.rmmApi = () => { evidenceRequests++; return new Promise((_resolve, reject) => { rejectEvidence = reject; }); };
  all().find(node => node.tag === 'details').open = true;
  const pollingRead = find('button', 'Load command arguments').click();
  api.remember({ ...job, progress: { ...job.progress, round: 49 } });
  const loading = find('button', 'Loading…');
  assert(loading.disabled, 'pending state survives replacement of dialog content by job polling');
  await loading.click();
  assert.equal(evidenceRequests, 1, 'a rerendered pending button cannot issue a duplicate read');
  rejectEvidence(new Error('connection lost'));
  await pollingRead;
  assert(find('button', 'Could not load command arguments. Retry'), 'read failure updates the current DOM after a poll');
  api.remember({ ...job, progress: { ...job.progress, round: 50 } });
  const retry = find('button', 'Could not load command arguments. Retry');
  assert(retry && !retry.disabled, 'error remains visible and retryable across another poll');
  assert(all().some(node => node.tag === 'details' && node.open), 'expanded evidence remains visible');
  window.rmmApi = async () => ({ status: 200, data: { evidence: { content: 'recovered', characters: 9, next_offset: null } } });
  await retry.click();
  assert(contains('recovered'), 'explicit retry succeeds without replaying a pending read');
  api.reset(); api.show(job);
  let release;
  window.rmmApi = () => new Promise(resolve => { release = resolve; });
  const pending = find('button', 'Load command arguments').click();
  api.reset();
  release({ status: 200, data: { evidence: { content: 'stale-secret', characters: 12, next_offset: null } } });
  await pending;
  api.show(job);
  assert(!contains('stale-secret'), 'response arriving after reset cannot repopulate protected evidence');
  window.rmmState.authGeneration++;
  api.remember({ id: 'new-owner-job', status: 'running' });
  assert(!document.body.children[0].open, 'account change closes and clears old dialog');
  api.show({ id: 'new-owner-job', status: 'running' });
  assert(!contains('job-1'), 'account change clears prior job cache');
  api.show({ id: 'ordered-job', status: 'running', updated_at: '2026-11-06T10:00:00Z' });
  api.remember({ id: 'ordered-job', status: 'failed', updated_at: '2026-11-06T09:00:00Z' });
  assert(contains('running · ordered-job'), 'older overlapping poll cannot replace a newer snapshot');
  api.show({ id: 'ordered-job', status: 'queued', updated_at: '2026-11-06T08:00:00Z' });
  assert(contains('running · ordered-job'), 'button captured before latest poll cannot roll details backward');
  api.reset();
  api.show({ ...job, status: 'interrupted' });
  assert.equal(timers.size, 1, 'dialog schedules one read-only refresh even for terminal jobs');
  window.rmmApi = async path => {
    assert.equal(path, '/ai/jobs/job-1');
    return { status: 200, data: { job: { ...job, status: 'interrupted', activity: [
      ...job.activity, { timestamp: '2026-11-06T09:01:00Z', phase: 'tool', status: 'completed', tool: 'execute_command_in_container', call_id: 'call-1' },
    ] } } };
  };
  await poll();
  assert(contains('Response received'), 'late observations after Stop appear without reopening');
  assert.equal(timers.size, 1, 'refresh reschedules only after response');
  window.rmmApi = async () => { throw new Error('offline'); };
  await poll();
  assert(contains('Could not refresh operation details'), 'refresh failure visible without hiding prior evidence');
  assert(contains('Response received'));
  find('button', 'Close').click();
  assert.equal(timers.size, 0, 'close cancels scheduled refresh');
  api.show(job);
  let finishRefresh;
  window.rmmApi = () => new Promise(resolve => { finishRefresh = resolve; });
  const staleRefresh = poll();
  api.reset();
  finishRefresh({ status: 200, data: { job: { ...job, progress: { tool: 'stale-tool' } } } });
  await staleRefresh;
  assert.equal(timers.size, 0, 'reset during refresh never reschedules');
  api.show(job);
  assert(!contains('stale-tool'), 'late refresh response cannot repopulate old scope');
  document.body.children[0].close();
  assert.equal(timers.size, 0, 'native Escape/close lifecycle cancels refresh');
  console.log('AI operation details checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

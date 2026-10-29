/* UI controller tests with a minimal DOM adapter; no browser or live agent. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.join(__dirname, '..');

function fixture(records = new Map(), savedResults = []) {
  const downloads = [];
  const blobs = new Map();
  class Element {
    constructor() { this.value = ''; this.children = []; this.handlers = {}; this.classList = { toggle() {} }; }
    addEventListener(event, handler) { this.handlers[event] = handler; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    add(child) { this.children.push(child); }
    showModal() { this.open = true; }
    close() { this.open = false; }
    click() {
      if (this.download) downloads.push({ name: this.download, blob: blobs.get(this.href) });
      this.handlers.click?.();
    }
  }
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="(embedding-[^"]+)"/g)].map((m) => [m[1], new Element()]));
  const calls = [];
  let ready;
  const document = {
    getElementById: (id) => { assert.ok(elements.has(id), `missing HTML control ${id}`); return elements.get(id); },
    createElement: () => new Element(),
    addEventListener: (event, fn) => { if (event === 'DOMContentLoaded') ready = fn; },
  };
  const request = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, method: options.method || 'GET', body });
    if (url === '/sessions') return { status: 200, data: { sessions: [] } };
    if (url.startsWith('/history/')) {
      const since = Number(new URL(url, 'http://fixture').searchParams.get('since'));
      const events = since === 0 ? Array.from({ length: 1000 }, (_, i) => ({ id: i + 1, type: 'output', body: 'unrelated' })) :
        since === 1000 ? [{ id: 1001, type: 'output', body: JSON.stringify({ model: 'example/model', results: [{}] }) }] : [];
      return { status: 200, data: { events } };
    }
    if (url === '/embeddings/profiles') return { status: 200, data: { profiles: [...records].map(([name, config]) => ({ name, config })) } };
    if (url.startsWith('/embeddings/profiles/')) {
      const name = url.split('/').pop();
      if (options.method === 'POST') records.set(name, structuredClone(body.config));
      if (options.method === 'DELETE') records.delete(name);
      return { status: 200, data: { ok: true } };
    }
    if (url === '/embeddings/results') return { status: 200, data: { results: savedResults } };
    if (url.startsWith('/embeddings/results/')) return { status: 200, data: { record: savedResults.find((r) => r.name === url.split('/').pop()) } };
    if (url === '/embeddings/run') return { status: 200, data: { session_id: 'fixture-session' } };
    throw new Error(`Unexpected URL ${url}`);
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'web/embeddings.js'), 'utf8'), {
    document, window: { rmmApi: request, confirm: () => true }, console, setTimeout,
    Option: class extends Element { constructor(label, value) { super(); this.textContent = label; this.value = value; } },
    Blob, URL: class extends URL {
      static createObjectURL(blob) { const url = `blob:fixture-${blobs.size}`; blobs.set(url, blob); return url; }
      static revokeObjectURL(url) { blobs.delete(url); }
    },
  });
  ready();
  const get = (id) => elements.get(`embedding-${id}`);
  const click = async (id) => { get(id).click(); for (let i = 0; i < 30; i++) await new Promise(setImmediate); };
  const change = async (id) => { get(id).handlers.change?.(); for (let i = 0; i < 30; i++) await new Promise(setImmediate); };
  return { get, click, change, records, calls, downloads };
}

test('save, reload, edit protection, run, and deletion use dedicated API controls', async () => {
  const ui = fixture();
  await ui.click('open');
  ui.get('name').value = 'strategy';
  await ui.click('save');
  assert.ok(ui.records.has('strategy'));
  assert.match(ui.get('status').textContent, /Saved strategy/);
  const reopened = fixture(ui.records);
  await reopened.click('open');
  reopened.get('profiles').value = 'strategy';
  await reopened.change('profiles');
  assert.equal(reopened.get('repo').value, 'Xenova/all-MiniLM-L6-v2');
  reopened.get('session').value = 'fixture-session';
  reopened.get('documents').value = 'C:\\Lab\\a.txt\nC:\\Lab\\b.docx';
  reopened.get('prompt').value = 'changed';
  await reopened.click('run');
  assert.match(reopened.get('status').textContent, /Save your configuration changes/);
  assert.equal(reopened.calls.filter((c) => c.url === '/embeddings/run').length, 0);
  await reopened.click('save'); await reopened.click('run');
  const run = reopened.calls.find((c) => c.url === '/embeddings/run');
  assert.deepEqual(run.body.documents, ['C:\\Lab\\a.txt', 'C:\\Lab\\b.docx']);
  assert.equal(run.body.profile, 'strategy');
  await reopened.click('delete'); assert.equal(reopened.records.size, 0);
});

test('event browsing advances past 1000 unrelated events and saves the explicit ID', async () => {
  const ui = fixture(); ui.get('session').value = 'fixture-session';
  await ui.click('load-events');
  assert.equal(ui.get('events').children.length, 1);
  assert.equal(ui.get('events').children[0].value, '1001');
  ui.get('event').value = '1001'; await ui.click('save-result');
  const save = ui.calls.find((c) => c.url === '/embeddings/results' && c.method === 'POST');
  assert.deepEqual(save.body, { session_ref: 'fixture-session', event_id: 1001 });
});

test('invalid label JSON reports an error and releases the busy button', async () => {
  const ui = fixture(); ui.get('name').value = 'bad'; ui.get('labels').value = '{';
  await ui.click('save');
  assert.equal(ui.records.size, 0); assert.equal(ui.get('save').disabled, false);
  assert.ok(ui.get('status').textContent);
});

test('copy settings survive import, save, reload, export, and protect unsaved runs', async () => {
  const ui = fixture();
  ui.get('name').value = 'copy-profile';
  await ui.click('save');
  const config = structuredClone(ui.records.get('copy-profile'));
  config.labels = [{ name: 'lab', text: 'Lab documentation' }];
  Object.assign(config, { copy_documents: true, copy_labels: ['lab'], copy_threshold: 0.4 });
  for (const value of [config, { config }, { record: { name: 'copy-profile', config } }]) {
    ui.get('import').files = [{ size: 1000, text: async () => JSON.stringify(value) }];
    await ui.change('import');
    assert.equal(ui.get('copy_documents').checked, true);
    assert.deepEqual(JSON.parse(ui.get('copy_labels').value), ['lab']);
    assert.equal(Number(ui.get('copy_threshold').value), 0.4);
  }
  await ui.click('save');
  assert.deepEqual(ui.records.get('copy-profile'), config);
  const reopened = fixture(ui.records);
  await reopened.click('open');
  reopened.get('profiles').value = 'copy-profile';
  await reopened.change('profiles');
  await reopened.click('export');
  assert.deepEqual(JSON.parse(await reopened.downloads[0].blob.text()).config, config);
  reopened.get('session').value = 'fixture-session';
  reopened.get('documents').value = 'C:\\Lab Docs\n\\\\server\\share\\Lab Docs';
  for (const [id, property, changed, original] of [
    ['copy_documents', 'checked', false, true],
    ['copy_labels', 'value', '[]', '["lab"]'],
    ['copy_threshold', 'value', '0.8', '0.4'],
  ]) {
    reopened.get(id)[property] = changed;
    await reopened.click('run');
    assert.match(reopened.get('status').textContent, /Save your configuration changes/);
    assert.equal(reopened.calls.filter((c) => c.url === '/embeddings/run').length, 0);
    reopened.get(id)[property] = original;
  }
  await reopened.click('run');
  assert.deepEqual(reopened.calls.find((c) => c.url === '/embeddings/run').body.documents,
    ['C:\\Lab Docs', '\\\\server\\share\\Lab Docs']);
});

test('legacy profiles default copying off and can run without resaving', async () => {
  const ui = fixture(); ui.get('name').value = 'legacy'; await ui.click('save');
  const config = ui.records.get('legacy');
  for (const key of ['copy_documents', 'copy_labels', 'copy_threshold']) delete config[key];
  const reopened = fixture(ui.records); await reopened.click('open');
  reopened.get('profiles').value = 'legacy'; await reopened.change('profiles');
  assert.equal(reopened.get('copy_documents').checked, false);
  assert.equal(reopened.get('copy_labels').value, '[]');
  assert.equal(Number(reopened.get('copy_threshold').value), 0);
  reopened.get('session').value = 'fixture-session'; reopened.get('documents').value = 'C:\\Lab';
  await reopened.click('run');
  assert.ok(reopened.calls.some((c) => c.url === '/embeddings/run'));
});

test('result export retains copied paths and directory error rows', async () => {
  const record = { name: 'collection', model: 'example/model', document_count: 2, session_id: 'fixture-session', event_id: 4,
    result: { copy_directory: 'C:\\Temp\\rmm-embeddings-test', results: [
      { path: 'C:\\Lab\\a.txt', embedding: [1, 0], classification: { label: 'lab', score: 0.8 }, copied_to: 'C:\\Temp\\rmm-embeddings-test\\lab\\a.txt' },
      { path: 'C:\\missing', error: 'not found', embedding: null },
    ] } };
  const ui = fixture(new Map(), [record]); await ui.click('open');
  const row = ui.get('results').children[0];
  assert.match(row.children[0].textContent, /2 result rows/);
  row.children[1].click();
  for (let i = 0; i < 30; i++) await new Promise(setImmediate);
  assert.deepEqual(JSON.parse(await ui.downloads[0].blob.text()), record);
});

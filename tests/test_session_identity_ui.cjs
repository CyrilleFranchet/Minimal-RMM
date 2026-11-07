const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({ escapeHtml: value => String(value)
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;') });
vm.runInContext(source.slice(source.indexOf('function sessionAccountLabel('),
  source.indexOf('function artifactAgentLabel(')), context);

test('account labels preserve domain and service identities', () => {
  for (const [username, hostname, expected] of [
    ['ARCHER\\Administrator', 'workstation', 'ARCHER\\Administrator'],
    ['WORKSTATION\\Administrator', 'workstation', '.\\Administrator'],
    ['WORKSTATION\\Administrator', 'workstation.example.test', '.\\Administrator'],
    ['.\\Administrator', 'workstation', '.\\Administrator'],
    ['NT AUTHORITY\\SYSTEM', 'workstation', 'NT AUTHORITY\\SYSTEM'],
    ['NT SERVICE\\Service', 'workstation', 'NT SERVICE\\Service'],
    ['BUILTIN\\Account', 'builtin', 'BUILTIN\\Account'],
    ['Administrator', 'workstation', 'Administrator'],
    ['admin@archer.example', 'workstation', 'admin@archer.example'],
    [null, null, '?'],
  ]) {
    const session = { username, hostname };
    assert.equal(context.sessionAccountLabel(session), expected);
    assert.equal(session.username, username);
  }
});

test('account and hostname are separate with escaped full identity tooltips', () => {
  const html = context.renderSessionIdentity({ username: 'HOST\\Administrator', hostname: 'host' });
  assert.match(html, />\.\\Administrator<\/div>/);
  assert.match(html, /class="sub session-hostname" title="host">host<\/div>/);
  assert.match(html, /title="HOST\\Administrator@host"/);
  const hostile = context.renderSessionIdentity({ username: '"><img src=x onerror=alert(1)>', hostname: '" onclick="bad' });
  assert.doesNotMatch(hostile, /<img|title=""/);
  for (const tag of hostile.match(/<div[^>]*>/g)) {
    assert.equal((tag.match(/"/g) || []).length, 4, "dynamic quotes escaped in attributes");
  }
  assert.match(hostile, /&quot;/);
  assert.match(hostile, /&lt;img/);
});

test('live and archived session cards render the compact account and separate host', () => {
  const node = () => ({ innerHTML: '', children: [], classList: { toggle() {} },
    appendChild(child) { this.children.push(child); }, insertBefore() {},
    addEventListener() {}, setAttribute() {} });
  const live = node(), history = node();
  Object.assign(context, {
    document: { createElement: node },
    $: selector => ({ '#session-list': live, '#history-list': history }[selector] || node()),
    state: { sessions: [{ id: 'live-session', username: 'HOST\\Administrator', hostname: 'host' }],
      historySessions: [{ session_id: 'archived-session', username: 'ARCHER\\Administrator', hostname: 'host' }] },
    sessionAgoSeconds: () => 1, formatAgo: () => '1s', formatTime: () => 'now',
    computeBeaconStatus: () => 'online', statusClass: () => 'online',
    formatFirstConnected: () => '', selectedSocksRelay: () => null, selectedTunRelay: () => null,
  });
  vm.runInContext(source.slice(source.indexOf('function renderSessionList('),
    source.indexOf('async function selectSession(')), context);
  vm.runInContext(source.slice(source.indexOf('function renderHistoryList('),
    source.indexOf('async function deleteHistorySession(')), context);
  context.renderSessionList();
  context.renderHistoryList();
  assert.match(live.children[0].innerHTML, />\.\\Administrator<\/div>/);
  assert.match(history.children[0].innerHTML, />ARCHER\\Administrator<\/div>/);
  for (const item of [live.children[0], history.children[0]]) {
    assert.match(item.innerHTML, /session-hostname" title="host">host<\/div>/);
  }
});

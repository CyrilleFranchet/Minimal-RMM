const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const blocks = [], lines = [], outputs = [];
const context = vm.createContext({
  state: { renderedEventIds: new Set(), lastEventId: 0 },
  createCommandBlock: (label, options) => blocks.push({label, options}),
  historyOperatorKind: () => null, historyOperatorMeta: () => null,
  historyRemoteFromOperatorCmd: () => null,
  appendShellLine: (kind, label) => lines.push(label),
  escapeHtml: text => text, appendShellOutput: text => outputs.push(text),
});
for (const [start, end] of [
  ['function shellCommandLabel(', 'function renderEventBodyHtml('],
  ['function appendEvent(', 'function clearShellInput('],
  ['function appendUnmatchedEvent(', 'function historyOperatorMeta('],
  ['function extractShellCommandFromEvent(', 'function mergeShellHistoryLists('],
]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);
context.appendEvent({id:1, type:'operator', body:'lateral: __LATERAL__ winrm lab-host loader=csharp'}, {history:true});
assert.equal(blocks[0].label, '/lateral winrm lab-host loader=csharp');
assert.equal(blocks[0].options.matchKeys[0], '__LATERAL__ winrm lab-host loader=csharp');
context.appendEvent({id:2, type:'operator', body:'exec: echo hello'}, {history:true});
assert.equal(blocks[1].label, 'echo hello');
context.appendUnmatchedEvent({type:'output', command:'__LATERAL__\n{"identity":{"password":"secret"}}', body:'result text'});
assert.equal(lines[0], 'result » /lateral');
assert.deepEqual(outputs, ['result text']);
for (const name of ['SPAWN', 'SPAWNAS', 'RUNAS', 'MAKE_TOKEN', 'REV2SELF', 'UID', 'SCREENSHOT', 'LATERAL_CLEANUP']) {
  assert.equal(context.shellCommandLabel(`__${name}__`), `/${name.toLowerCase()}`);
}
assert.equal(context.shellCommandLabel('echo __LATERAL__'), 'echo __LATERAL__');
assert.equal(context.shellCommandLabel('__UNKNOWN__ value'), '__UNKNOWN__ value');
assert.equal(context.extractShellCommandFromEvent({type:'operator', body:'lateral: __LATERAL__ winrm host'}), null);
console.log('Shell command label tests passed');
assert.equal(context.shellTimestampHtml(null, 'Submitted'), '');
assert.equal(context.shellTimestampHtml('invalid', 'Submitted'), '');
assert.match(context.shellTimestampHtml('2026-11-23T15:04:05+02:00', 'Submitted'), /Submitted: 2026-11-23 13:04:05 UTC/);
assert.match(context.shellTimestampHtml('2026-11-23T15:04:05.123456', 'Recorded'), /2026-11-23 15:04:05 server time/);
context.appendEvent({id:3, type:'operator', body:'exec: whoami', timestamp:'2026-11-23T15:04:05Z'}, {history:true});
assert.equal(blocks[2].options.timestamp, '2026-11-23T15:04:05Z');
assert.equal(blocks[0].options.timestamp, null);
context.appendUnmatchedEvent({type:'output', command:'whoami', body:'lab-user', timestamp:'2026-11-23T15:04:08Z'});
assert.match(lines[1], /Result received: 2026-11-23 15:04:08 UTC/);
// Exercise the actual command and result renderers with a minimal DOM.
const node = () => ({children:[], appendChild(child){ this.children.push(child); }, remove(){}});
const log = node();
Object.assign(context, {document:{createElement:node}, shellOutputEl:()=>log,
  $:()=>({textContent:'PS>'}), scrollBlockIntoView(){}, scrollShellToBottom(){}, normalizeCmdKey:s=>s,
  looksLikeTransferProgressJson:()=>false});
context.state.pendingCommandBlocks=[]; context.state.commandBlockSeq=0;
for (const [start,end] of [
  ['function createCommandBlock(', 'function appendUnmatchedEvent('],
  ['function fillCommandBlock(', 'function createCommandBlock('],
]) vm.runInContext(source.slice(source.indexOf(start),source.indexOf(end)),context);
const block=context.createCommandBlock('whoami');
assert.match(block.blockEl.children[0].innerHTML, /Submitted: .* UTC/);
context.fillCommandBlock(block,{type:'output',body:'lab-user',timestamp:'2026-11-23T15:04:08Z'});
assert.match(block.resultEl.children[0].innerHTML, /Result received: 2026-11-23 15:04:08 UTC/);
assert.equal(block.resultEl.children[1].innerHTML,'lab-user');
const legacy=context.createCommandBlock('old command',{timestamp:null});
assert.doesNotMatch(legacy.blockEl.children[0].innerHTML,/Submitted:/);
console.log('Shell timestamp tests passed');
for (const bad of ['2026-02-30T12:00:00Z', '2026-11-23', '11/23/2026 12:00', '2026-11-23T24:00:00Z', '2026-11-23T12:00:00+25:00', '<script>']) {
  assert.equal(context.shellTimestampHtml(bad, 'Recorded'), '', bad);
}
assert.match(context.shellTimestampHtml('2026-11-23T00:30:00+02:00', 'Recorded'), /2026-11-22 22:30:00 UTC/);
assert.match(context.shellTimestampHtml('2024-02-29T12:00:00Z', 'Recorded'), /2024-02-29 12:00:00 UTC/);
context.renderEventBodyHtml=()=>'<a>artifact</a>';
context.renderCloudUploadHtml=()=>'<a>cloud</a>';
context.scrollShellIfNearBottom=()=>{};
for (const type of ['output', 'file_upload', 'screenshot', 'cloud_upload']) {
  const before=lines.length;
  context.appendUnmatchedEvent({type, body:'result', timestamp:'2026-11-23T15:04:08Z'});
  assert.match(lines[before], /Result received: 2026-11-23 15:04:08 UTC/, type);
}
context.state.echoedCommands=new Set();
context.appendEvent({id:4,type:'operator',body:'exec: whoami',timestamp:'2026-11-23T15:04:05Z'});
assert.match(lines.at(-1), /Recorded: 2026-11-23 15:04:05 UTC/);
context.appendEvent({id:5,type:'operator',body:'exec: hostname'}, {history:true});
assert.doesNotMatch(log.children.at(-1).children[0].innerHTML,/Submitted:/);
console.log('Timestamp validation and unmatched result tests passed');

// Queued results must update their matching blocks even when returned out of order.
vm.runInContext(source.slice(source.indexOf('function normalizeCmdKey('), source.indexOf('function normalizeRemotePath(')), context);
assert.equal(context.commandKeysMatch('__LATERAL__', '__LATERAL__\n{"method":"winrm"}'), true);
context.state.pendingCommandBlocks = [];
const identity = context.createCommandBlock('/uid', {matchKeys:['/uid', '__UID__']});
const invalid = node();
log.appendChild(invalid);
context.appendEvent({id:6, type:'output', command:'__UID__', body:'LAB\\alice'});
assert.equal(identity.resultEl.children.at(-1).innerHTML, 'LAB\\alice');
assert.equal(log.children.at(-1), invalid);
const first = context.createCommandBlock('echo first', {meta:'(queued)'});
const second = context.createCommandBlock('echo second', {meta:'(queued)'});
context.appendEvent({id:7, type:'output', command:'echo second', body:'second'});
assert.equal(second.filled, true);
assert.equal(second.metaLine, null);
assert.equal(second.resultEl.children.at(-1).innerHTML, 'second');
assert.equal(first.filled, false);
context.appendEvent({id:8, type:'output', command:'echo first', body:'first'});
assert.equal(first.filled, true);
assert.equal(first.metaLine, null);
assert.equal(first.resultEl.children.at(-1).innerHTML, 'first');

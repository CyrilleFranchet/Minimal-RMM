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

const http = require('node:http');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { test } = require('node:test');
const assert = require('node:assert/strict');
const pwsh = process.env.RMM_TEST_PWSH || 'pwsh';
const available = spawnSync(pwsh, ['-NoProfile', '-Command', 'exit 0']).status === 0;
test('PowerShell detects the termination response and retries other forbidden responses', { skip: !available, timeout: 15000 }, async () => {
  const server = http.createServer((req, res) => {
    res.writeHead(req.url.includes('/server-error') ? 500 : 403, { 'Content-Type': 'text/plain', 'Server': 'regression-test' });
    res.end((req.url.includes('/terminated') || req.url.includes('/server-error')) ? 'TERMINATED' : 'FORBIDDEN');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const result = await new Promise((resolve, reject) => {
      const child = spawn(pwsh, ['-NoProfile', '-NonInteractive', '-File', path.join(__dirname, 'test_agent_termination.ps1'), '-BaseUrl', `http://127.0.0.1:${server.address().port}`]);
      let output = ''; child.stdout.on('data', d => output += d); child.stderr.on('data', d => output += d);
      child.on('error', reject); child.on('close', code => resolve({ code, output }));
    });
    assert.equal(result.code, 0, result.output);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

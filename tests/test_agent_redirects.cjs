/* Exercise production PowerShell HTTP functions against loopback redirects. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { test } = require('node:test');
const pwsh = process.env.RMM_TEST_PWSH || 'pwsh';
const available = spawnSync(pwsh, ['-NoProfile', '-NonInteractive', '-Command', 'exit 0']).status === 0;

test('PowerShell registration failures cannot discard an already-dequeued command',
  { skip: !available && 'pwsh is required for the offline PowerShell regression' }, () => {
    const result = spawnSync(pwsh, ['-NoProfile', '-NonInteractive', '-File', path.join(__dirname, 'test_agent_poll_order.ps1')],
      { encoding: 'utf8', timeout: 15000 });
    assert.equal(result.status, 0, result.stdout + result.stderr);
  });

test('PowerShell GET, POST and file downloads reject cross-origin redirects without leaking the beacon token',
  { skip: !available && 'pwsh is required for the offline PowerShell regression', timeout: 20000 }, async () => {
    let redirectedRequests = 0;
    let sourceRequests = 0;
    const destination = http.createServer((req, res) => { redirectedRequests++; res.end('unexpected'); });
    const listen = (server) => new Promise((resolve, reject) => {
      server.once('error', reject); server.listen(0, '127.0.0.1', resolve);
    });
    const source = http.createServer((req, res) => {
      sourceRequests++;
      assert.equal(req.headers['x-rmm-beacon-token'], 'fake-test-token');
      req.resume();
      res.writeHead(Number(req.url.split('?')[0].slice(1)), {
        Location: `http://127.0.0.1:${destination.address().port}/destination`,
      });
      res.end('redirect');
    });
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'rmm-ps-redirect-'));
    let child;
    try {
      await listen(destination);
      await listen(source);
      const result = await new Promise((resolve, reject) => {
        child = spawn(pwsh, ['-NoProfile', '-NonInteractive', '-File', path.join(__dirname, 'test_agent_redirects.ps1'),
          '-BaseUrl', `http://127.0.0.1:${source.address().port}`, '-OutputPath', path.join(directory, 'artifact')], { timeout: 15000 });
        let output = '';
        child.stdout.on('data', (data) => { output += data; });
        child.stderr.on('data', (data) => { output += data; });
        child.on('error', reject);
        child.on('close', (code) => resolve({ code, output }));
      });
      assert.equal(result.code, 0, result.output);
      assert.equal(sourceRequests, 15);
      assert.equal(redirectedRequests, 0);
    } finally {
      child?.kill();
      source.closeAllConnections(); destination.closeAllConnections();
      await Promise.all([new Promise((resolve) => source.close(resolve)), new Promise((resolve) => destination.close(resolve))]);
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });

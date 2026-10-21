const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

assert.equal(process.platform, 'win32');
assert.equal(process.env.GITHUB_ACTIONS, 'true', 'This runner launches GUI processes only in isolated Windows CI');
const electron = require('electron');
const runs = [];
for (const scale of ['1', '1.5']) {
  const env = { ...process.env, CEF_PROBE_SCALE: scale };
  delete env.ELECTRON_RUN_AS_NODE;
  const result = spawnSync(electron, [path.join(__dirname, 'host.cjs')], {
    env, windowsHide: true, timeout: 120_000, encoding: 'utf8',
  });
  fs.writeFileSync(path.join(env.CEF_PROBE_OUTPUT, `electron-${scale}-stdout.log`), result.stdout ?? '');
  fs.writeFileSync(path.join(env.CEF_PROBE_OUTPUT, `electron-${scale}-stderr.log`), result.stderr ?? '');
  runs.push({ scale: Number(scale), exitCode: result.status, signal: result.signal,
    error: result.error?.message, status: result.status === 0 ? 'passed' : 'failed' });
}
fs.writeFileSync(path.join(process.env.CEF_PROBE_OUTPUT, 'runs.json'), `${JSON.stringify(runs, null, 2)}\n`);
assert(runs.every((run) => run.status === 'passed'), 'CEF sidebar runtime proof failed; inspect uploaded evidence');

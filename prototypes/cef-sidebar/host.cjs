const { app, BrowserWindow } = require('electron');
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const readline = require('node:readline');
const { setTimeout: delay } = require('node:timers/promises');

assert.equal(process.platform, 'win32');
assert.equal(process.env.GITHUB_ACTIONS, 'true', 'Run this prototype only in isolated Windows CI');
const output = path.join(process.env.CEF_PROBE_OUTPUT, `scale-${process.env.CEF_PROBE_SCALE}`);
fs.mkdirSync(output, { recursive: true });
app.setPath('userData', path.join(output, 'electron-profile'));
app.commandLine.appendSwitch('force-device-scale-factor', process.env.CEF_PROBE_SCALE);
const eventsPath = path.join(output, 'host.jsonl');
const record = (event) => fs.appendFileSync(eventsPath, `${JSON.stringify(event)}\n`);
let expectedOwnerClose = false;
let activeChild;
let ownerClosed = false;

// Only the controlled local origin sets or reads this key. The second helper
// process loads the same origin and CEF profile without writing the value.
const page = `<!doctype html><meta charset="utf-8"><title>CEF sidebar probe</title>
<input autofocus aria-label="focus target"><p>Isolated CEF Chrome child</p>
<script>
const params = new URLSearchParams(location.search);
if (params.get('phase') === 'write') localStorage.setItem('cef-sidebar-probe', params.get('token'));
window.probeReport = (stage) => {
  document.title = 'CEF_PROBE:' + JSON.stringify({ stage,
    value: localStorage.getItem('cef-sidebar-probe'), dpr: devicePixelRatio,
    focused: document.hasFocus(), width: innerWidth, height: innerHeight });
};
</script>`;

function runPhase(owner, origin, token, phase) {
  return new Promise((resolve, reject) => {
    const hwnd = owner.getNativeWindowHandle().readBigUInt64LE().toString();
    const child = spawn(process.env.CEF_PROBE_EXE, [
      `--owner-hwnd=${hwnd}`, `--profile-root=${path.join(output, 'cef-profile')}`,
      `--output=${output}`, `--phase=${phase}`, `--token=${token}`,
      `--url=${origin}/probe?phase=${phase}&token=${token}`,
      `--force-device-scale-factor=${process.env.CEF_PROBE_SCALE}`,
    ], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    activeChild = child;
    record({ event: 'helper-start', phase, owner: hwnd, ownerPid: process.pid, helperPid: child.pid });
    const checks = [];
    const seen = [];
    child.stdout.on('data', (bytes) => fs.appendFileSync(path.join(output, `${phase}-stdout.log`), bytes));
    child.stderr.on('data', (bytes) => fs.appendFileSync(path.join(output, `${phase}-stderr.log`), bytes));
    const timeout = setTimeout(() => {
      record({ event: 'timeout', phase });
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
      reject(new Error(`CEF ${phase} phase exceeded 45 seconds; see raw logs`));
    }, 45_000);
    child.on('error', reject);
    const lines = readline.createInterface({ input: child.stdout });
    lines.on('line', (line) => {
      if (!line.startsWith('CEF_PROBE_EVENT ')) return;
      try {
        const event = JSON.parse(line.slice('CEF_PROBE_EVENT '.length));
        record(event);
        seen.push(event.event);
        if (event.event === 'check') checks.push(event);
        if (event.event === 'ready-resize') {
          assert(!owner.isDestroyed(), 'Closing CEF also closed the owner');
          owner.setContentSize(1120, 760);
          child.stdin.write('resize\n');
        }
        if (event.event === 'ready-close') child.stdin.end('close\n');
        if (event.event === 'message-loop-exited') child.stdin.end();
      } catch (error) {
        reject(error);
      }
    });
    child.once('close', async (code, signal) => {
      clearTimeout(timeout);
      activeChild = undefined;
      record({ event: 'helper-exit', phase, code, signal });
      // Observe asynchronous WM_CLOSE delivery after the helper has exited.
      await delay(500);
      try {
        assert.equal(code, 0, `CEF helper failed in ${phase}`);
        assert(seen.includes('before-close'), 'Native OnBeforeClose was never received');
        assert(seen.includes('ready-close'), 'Resize/focus sequence never completed');
        assert.equal(checks.filter((check) => !check.passed).length, 0, 'Native checks failed');
        assert(checks.length > 20, 'Native check evidence is incomplete');
        assert(!ownerClosed && !owner.isDestroyed(), 'CEF close destroyed the original Electron owner');
        assert.equal(BrowserWindow.getAllWindows().length, 1, 'Unexpected Electron owner window');
        record({ event: 'phase-complete', phase, ownerSurvived: true, checks: checks.length });
        resolve(checks.length);
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function main() {
  await app.whenReady();
  const server = http.createServer((request, response) => {
    if (!request.url.startsWith('/probe')) {
      response.writeHead(404);
      response.end();
      return;
    }
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(page);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const owner = new BrowserWindow({
    width: 1000, height: 700, useContentSize: true, frame: false,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  owner.on('closed', () => {
    ownerClosed = true;
    record({ event: 'owner-closed', expected: expectedOwnerClose });
  });
  await owner.loadURL(`data:text/html,${encodeURIComponent(`<!doctype html>
    <style>body{margin:0;display:grid;grid-template-columns:40% 60%;height:100vh;font:16px sans-serif}main{padding:24px}aside{background:#eef}</style>
    <main>Isolated Electron owner<p>CEF must occupy the original right rectangle.</p></main><aside>CEF child bounds</aside>`)}`);
  owner.show();
  owner.focus();
  record({ event: 'host-ready', electron: process.versions.electron,
    chromium: process.versions.chrome, scale: Number(process.env.CEF_PROBE_SCALE) });
  const token = crypto.randomUUID();
  let checks = 0;
  for (const phase of ['write', 'read']) {
    owner.setContentSize(1000, 700);
    owner.focus();
    checks += await runPhase(owner, origin, token, phase);
  }
  const summary = {
    status: 'passed', scale: Number(process.env.CEF_PROBE_SCALE), checks,
    proof: ['chrome-style-child', 'original-electron-hwnd', 'resize', 'native-and-renderer-focus',
      'forced-scale-coordinates', 'cef-close-owner-survives', 'profile-settings-and-local-storage-restart'],
    skipped: ['physical-cross-monitor-dpi', 'human-input-and-ime', 'native-password-permission-download-ui',
      'pane-hide-and-owner-initiated-close', 'full-browser-parity'],
  };
  fs.writeFileSync(path.join(output, 'summary.json'), `${JSON.stringify(summary, null, 2)}\n`);
  expectedOwnerClose = true;
  owner.destroy();
  server.close();
  app.exit(0);
}

main().catch((error) => {
  record({ event: 'failure', message: error.stack });
  fs.writeFileSync(path.join(output, 'summary.json'), `${JSON.stringify({ status: 'failed', error: error.stack }, null, 2)}\n`);
  if (activeChild?.pid) {
    spawnSync('taskkill', ['/PID', String(activeChild.pid), '/T', '/F'], { windowsHide: true });
  }
  app.exit(1);
});

"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const { execFileSync, spawn } = require("node:child_process");
const readline = require("node:readline");
const { createWindowsPty } = require("./windows-pty");
const windows = process.platform === "win32";
const executable = path.join(__dirname, "windows-sandbox-runtime", "codex.exe");
const python = () => execFileSync("python", ["-c", "import sys;print(sys.executable)"], { encoding: "utf8", windowsHide: true }).trim();
function deadline(promise, label) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(label)), 15000); })])
    .finally(() => clearTimeout(timer));
}
function terminal(t, program, args, { binary = executable, env = {}, cols = 80, rows = 24 } = {}) {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-source-pty-"));
  const owned = createWindowsPty(binary).spawn(program, args, { cwd, env: { ...process.env, HOME: cwd, USERPROFILE: cwd, ...env }, cols, rows });
  let output = "", exit;
  const ready = new Promise(resolve => owned.onReady(resolve));
  const closed = new Promise(resolve => owned.onExit(value => { exit = value; resolve(value); }));
  owned.onData(data => { output += data; });
  t.after(async () => { if (!exit) { owned.forceKill(); await deadline(closed, "owned test terminal did not close"); } });
  return { owned, ready, closed, cwd, output: () => output };
}
test("the default Source owner naturally releases its native process and preserves UTF-8 tail and exit 7", { skip: !windows }, async t => {
  const value = terminal(t, python(), ["-I", "-c", "import sys;sys.stdout.buffer.write(('终端🌕\\n'*12000+'TAIL_UTF8_终端🌕\\n').encode());sys.stdout.flush();sys.exit(7)"]);
  assert.deepEqual(await deadline(value.closed, "natural terminal exit did not settle"), { exitCode: 7 });
  assert.equal(value.output().split("终端🌕").length - 1, 12001);
  assert.match(value.output(), /TAIL_UTF8_终端🌕/);
  assert.equal(value.owned.write("late input"), false);
  assert.equal(value.owned.resize(100, 30), false);
  value.owned.kill(); value.owned.kill(); value.owned.forceKill();
});
test("the actual Source PTY applies resize before accepting the next interactive input", { skip: !windows }, async t => {
  const value = terminal(t, python(), ["-I", "-c", "import os,sys;sys.stdin.readline();s=os.get_terminal_size();print('SIZE:%d:%d'%(s.columns,s.lines),flush=True)"]);
  await deadline(value.ready, "Source terminal did not become ready");
  const resized = new Promise(resolve => value.owned.onResize(resolve));
  value.owned.resize(117, 39);
  assert.deepEqual(await deadline(resized, "Source resize did not acknowledge"), { cols: 117, rows: 39 });
  value.owned.write("measure\r");
  assert.deepEqual(await deadline(value.closed, "resized terminal did not exit"), { exitCode: 0 }, value.output());
  assert.match(value.output(), /SIZE:117:39/);
});
test("terminate releases the Source-owned child tree and repeated termination is idempotent", { skip: !windows }, async t => {
  const marker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "minicode-source-child-")), "child.pid");
  const script = "import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(120)']);pathlib.Path(sys.argv[1]).write_text(str(p.pid));print('CHILD_READY',flush=True);time.sleep(120)";
  const value = terminal(t, python(), ["-I", "-c", script, marker]);
  await deadline(new Promise(resolve => { const subscription = value.owned.onData(() => { if (value.output().includes("CHILD_READY")) { subscription.dispose(); resolve(); } }); }), "owned child did not start");
  const childPid = Number(fs.readFileSync(marker, "utf8"));
  value.owned.kill(); value.owned.kill();
  assert.equal(value.owned.write("after terminate"), false);
  const result = await deadline(value.closed, "Source tree termination did not settle");
  assert.equal(Number.isFinite(result.exitCode), true);
  const alive = execFileSync(python(), ["-c", "import psutil,sys;print(psutil.pid_exists(int(sys.argv[1])))", String(childPid)], { encoding: "utf8", windowsHide: true }).trim();
  assert.equal(alive, "False");
});
test("a queued paste cannot prevent the actual native terminate control from closing the owner", { skip: !windows }, async t => {
  const value = terminal(t, python(), ["-I", "-c", "import time;print('BUSY',flush=True);time.sleep(120)"]);
  await deadline(value.ready, "busy terminal did not become ready");
  value.owned.write("x".repeat(1024 * 1024));
  value.owned.kill();
  assert.equal(Number.isFinite((await deadline(value.closed, "queued input blocked termination")).exitCode), true);
});
test("caller transport EOF releases the actual Source owner and its child job without a PID command", { skip: !windows }, async t => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-source-pty-eof-"));
  const marker = path.join(cwd, "child.pid");
  const owner = spawn(executable, ["--desktop-pty"], { cwd, env: { ...process.env, HOME: cwd, USERPROFILE: cwd }, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  let output = "", diagnostic = "";
  const messages = [];
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  readline.createInterface({ input: owner.stdout }).on("line", line => {
    const message = JSON.parse(line); messages.push(message);
    if (message.event === "data") {
      output += Buffer.from(message.data, "base64").toString();
      if (output.includes("EOF_CHILD_READY")) started();
    }
  });
  owner.stderr.on("data", bytes => { diagnostic += bytes; });
  const closed = new Promise(resolve => owner.once("close", (code, signal) => resolve({ code, signal })));
  t.after(async () => { if (owner.exitCode === null) owner.kill(); await deadline(closed, "EOF owner cleanup did not finish"); });
  owner.stdin.write(JSON.stringify({ method: "start", params: { program: python(), args: ["-I", "-c",
    "import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(120)']);pathlib.Path(sys.argv[1]).write_text(str(p.pid));print('EOF_CHILD_READY',flush=True);time.sleep(120)", marker], cwd, cols: 80, rows: 24 } }) + "\n");
  await deadline(ready, "EOF child fixture did not start");
  const childPid = Number(fs.readFileSync(marker, "utf8"));
  owner.stdin.end();
  assert.deepEqual(await deadline(closed, "Source did not settle after caller EOF"), { code: 0, signal: null }, diagnostic);
  assert.equal(Number.isFinite(messages.find(message => message.event === "exit").exitCode), true);
  const alive = execFileSync(python(), ["-c", "import psutil,sys;print(psutil.pid_exists(int(sys.argv[1])))", String(childPid)], { encoding: "utf8", windowsHide: true }).trim();
  assert.equal(alive, "False");
});

let fixture;
function transportFixture() {
  if (fixture) return fixture;
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-pty-owner-fixture-"));
  fixture = path.join(directory, "owner.exe");
  const source = path.join(__dirname, "test-fixtures", "windows-pty-owner.cs");
  const command = `$ProgressPreference = 'SilentlyContinue'; Add-Type -Path '${source.replaceAll("'", "''")}' -OutputAssembly '${fixture.replaceAll("'", "''")}' -OutputType ConsoleApplication`;
  execFileSync(path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(command, "utf16le").toString("base64")], { windowsHide: true });
  return fixture;
}
for (const mode of ["transport-error", "missing-result", "invalid-protocol"]) {
  test(`an actual owner ${mode} never fabricates a successful PTY exit`, { skip: !windows }, async t => {
    const value = terminal(t, "fixture", [], { binary: transportFixture(), env: { MINICODE_TEST_PTY_OWNER_MODE: mode } });
    const result = await deadline(value.closed, `${mode} transport did not close`);
    assert.equal(result.exitCode, null);
    assert.match(result.signal, /^owner-exit-/);
  });
}
test("the manager's timed-out native terminate really escalates through its held owner handle", { skip: !windows }, async t => {
  const events = [], terminals = [];
  const box = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "pty-manager.js"), "utf8"), {
    module: box, process, console, setTimeout, clearTimeout,
    require(name) { return name === "./preferences" ? { resolveTerminalShell: () => ({ executable: "fixture", args: [] }) } : require(name); },
  });
  const manager = box.exports;
  manager.init({ pty: { spawn(program, args, options) {
    const value = createWindowsPty(transportFixture()).spawn(program, args, { ...options, env: { ...process.env, MINICODE_TEST_PTY_OWNER_MODE: "ignore-terminate" } });
    terminals.push(value); return value;
  } }, killExitTimeoutMs: 50, forceKillExitTimeoutMs: 3000, sanitizedPtyEnv: () => ({}), assertTrustedPath: value => value,
  appendDesktopLog: text => events.push(text), getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }) });
  t.after(async () => { terminals.forEach(value => value.forceKill()); await manager.killAllSessions(); });
  const first = manager.spawnSession(os.tmpdir(), "owner", "window-one");
  const ready = new Promise(resolve => terminals[0].onReady(resolve));
  await deadline(ready, "controlled owner did not start");
  assert.equal(await manager.killSession(first.session_id, "owner", "window-two"), false);
  assert.equal(await deadline(manager.killSession(first.session_id, "owner", "window-one"), "forced owner did not close"), true);
  assert.equal(events.some(line => line.includes("forcing kill")), true);
  assert.equal(manager.snapshotSession(first.session_id, 20, "owner", "window-one"), null);
  assert.equal(await manager.killSession(first.session_id, "owner", "window-one"), false);
});

"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const { createDictationHotkeys, parseDictationShortcut } = require("./dictation-hotkeys");

function fixture(t, directory) {
  const root = directory ?? fs.mkdtempSync(path.join(os.tmpdir(), "minicode-dictation-"));
  if (!directory) t.after(() => { assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir())); fs.rmSync(root, { recursive: true }); });
  const workers = []; const events = []; const pendingTimers = new Map(); let clock = 1000; let timerId = 0;
  const handle = Buffer.alloc(8); handle.writeBigUInt64LE(123n);
  const window = { isDestroyed: () => false, getNativeWindowHandle: () => handle, webContents: { id: 7, send: (name, value) => events.push({ name, value }) } };
  const spawnCalls = [];
  const spawnWorker = (...args) => {
    spawnCalls.push(args);
    const worker = new EventEmitter(); worker.stdout = new PassThrough(); worker.stderr = new PassThrough(); worker.commands = [];
    worker.stdin = new Writable({ write(chunk, _encoding, callback) {
      const command = JSON.parse(String(chunk)); worker.commands.push(command);
      const failed = command.op === "configure" && command.hold?.vk === 88;
      worker.stdout.write(JSON.stringify({ id: command.id, ok: !failed, value: command.op === "insert" ? worker.insertResult !== false : true,
        error: "fixture OS shortcut conflict" }) + "\n");
      if (command.op === "shutdown") setImmediate(() => worker.emit("exit", 0));
      callback();
    } });
    workers.push(worker);
    queueMicrotask(() => worker.stdout.write(JSON.stringify({ kind: "ready", doubleClickTime: 500 }) + "\n"));
    return worker;
  };
  const manager = createDictationHotkeys({ app: { isPackaged: false, getPath: () => root }, getMainWindows: () => [window], getFocusedMainWindow: () => window, getFocusedWebContents: () => window.webContents,
    spawnWorker, platform: "win32", now: () => clock, timers: { setTimeout: fn => { const id = ++timerId; pendingTimers.set(id, fn); return id; }, clearTimeout: id => pendingTimers.delete(id) } });
  const input = (phase, hotkey="hold", foreground="456") => workers[0].stdout.write(JSON.stringify({ kind: "key", phase, hotkey, target: { foreground, pid: 9, focus: "789" } }) + "\n");
  const actionEvents = () => events.filter(event => event.name === "minicode:dictation:event").map(event => event.value);
  return { manager, root, workers, spawnCalls, input, actionEvents, pendingTimers, tick(ms) { clock += ms; }, flushTimers() { const timers = [...pendingTimers.values()]; pendingTimers.clear(); timers.forEach(fn => fn()); } };
}

test("parses actual Windows chords including bare modifiers and rejects unusable bindings", () => {
  assert.deepEqual(parseDictationShortcut("Ctrl+Shift+Space"), { vk: 32, modifiers: 6, bare: false });
  assert.deepEqual(parseDictationShortcut("Ctrl+Shift"), { vk: 16, modifiers: 2, bare: true });
  assert.deepEqual(parseDictationShortcut("F12"), { vk: 123, modifiers: 0, bare: false });
  assert.equal(parseDictationShortcut(""), null);
  assert.throws(() => parseDictationShortcut("A"));
  assert.throws(() => parseDictationShortcut("Ctrl+Ctrl+K"));
});

test("starts one hidden fixture worker and persists only successfully registered shortcuts", async t => {
  const f = fixture(t);
  const [first, second] = await Promise.all([f.manager.get(), f.manager.get()]);
  assert.equal(first.available, true); assert.equal(second.available, true); assert.equal(f.workers.length, 1);
  assert.equal(f.spawnCalls[0][0], "powershell.exe"); assert.equal(f.spawnCalls[0][2].windowsHide, true);
  assert.ok(f.spawnCalls[0][1].includes(path.join(__dirname, "scripts", "dictation-keyboard.ps1")));
  await f.manager.configure({ holdHotkey: "Ctrl+Shift+Space", toggleHotkey: "Alt+F12" });
  assert.equal((await f.manager.get()).holdHotkey, "Ctrl+Shift+Space");
  assert.equal((await f.manager.get()).toggleHotkey, "Alt+F12");
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "dictation-hotkeys.json"), "utf8")), { holdHotkey: "Ctrl+Shift+Space", toggleHotkey: "Alt+F12" });
  await assert.rejects(f.manager.configure({ toggleHotkey: "Ctrl+Shift+Space" }), /must be different/);
  await assert.rejects(f.manager.configure({ holdHotkey: "Ctrl+X" }), /fixture OS shortcut conflict/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root, "dictation-hotkeys.json"), "utf8")).holdHotkey, "Ctrl+Shift+Space");
  const restarted = fixture(t, f.root);
  assert.equal((await restarted.manager.get()).holdHotkey, "Ctrl+Shift+Space");
  assert.equal(restarted.workers[0].commands.find(command => command.op === "configure").hold.vk, 32);
  await f.manager.configure({ holdHotkey: "", toggleHotkey: "" });
  assert.equal((await f.manager.get()).holdHotkey, "");
  assert.deepEqual(f.workers[0].commands.filter(command => command.op === "configure").at(-1).hold, null);
  await restarted.manager.dispose(); await f.manager.dispose();
});

test("real down/up event contract separates hold, hands-free double tap, single tap, and Escape", async t => {
  const f = fixture(t); await f.manager.get();
  f.input("down"); f.tick(700); f.input("up");
  assert.deepEqual(f.actionEvents().map(event => event.action), ["start", "stop"]);
  const first = f.actionEvents()[0];
  await f.manager.complete(99, first.token);
  f.input("down", "toggle");
  assert.equal(f.actionEvents().length, 2);
  await f.manager.complete(7, first.token);
  f.input("down"); f.tick(60); f.input("up");
  assert.equal(f.pendingTimers.size, 1);
  f.tick(80); f.input("down"); f.input("up"); f.flushTimers();
  assert.deepEqual(f.actionEvents().slice(2).map(event => event.action), ["start"]);
  f.input("down");
  assert.deepEqual(f.actionEvents().slice(2).map(event => event.action), ["start", "stop"]);
  await f.manager.complete(7, f.actionEvents()[2].token);
  f.input("down", "toggle"); f.input("up", "toggle");
  assert.equal(f.actionEvents().at(-1).action, "start");
  f.workers[0].stdout.write(JSON.stringify({ kind: "escape" }) + "\n");
  assert.equal(f.actionEvents().at(-1).action, "cancel");
  await f.manager.complete(7, f.actionEvents().at(-1).token);
  await f.manager.dispose();
});

test("external insertion retains original native target and exact renderer token without stealing focus", async t => {
  const f = fixture(t); await f.manager.get();
  f.input("down"); f.tick(600); f.input("up"); const first = f.actionEvents()[0];
  assert.equal(first.targetKind, "external");
  assert.equal(await f.manager.insert(99, first.token, "wrong sender"), false);
  assert.equal(await f.manager.insert(7, "wrong-token", "wrong token"), false);
  assert.equal(await f.manager.insert(7, first.token, "original owner transcript"), true);
  const inserted = f.workers[0].commands.find(command => command.op === "insert");
  assert.deepEqual(inserted.target, { foreground: "456", pid: 9, focus: "789" });
  f.workers[0].insertResult = false;
  assert.equal(await f.manager.insert(7, first.token, "target changed"), false);
  await f.manager.complete(7, first.token);
  assert.equal(await f.manager.insert(7, first.token, "stale result"), false);
  await f.manager.suspend(7, true); f.input("down");
  assert.equal(f.actionEvents().length, 2);
  await f.manager.suspend(7, false); f.input("down", "hold", "123");
  assert.equal(f.actionEvents().at(-1).targetKind, "composer");
  const appToken = f.actionEvents().at(-1).token;
  f.tick(600); f.input("up", "hold", "123");
  f.workers[0].insertResult = true;
  assert.equal(await f.manager.insert(7, appToken, "original app input"), true);
  assert.deepEqual(f.workers[0].commands.filter(command => command.op === "insert").at(-1).target, { foreground: "123", pid: 9, focus: "789" });
  await f.manager.complete(7, appToken);
  f.input("down", "hold", "123");
  f.manager.releaseWindow(7);
  assert.equal(f.actionEvents().at(-1).action, "cancel");
  await f.manager.dispose();
});

test("packaged worker resources and native insertion keep the required provenance and target checks", () => {
  const packaged = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
  assert.ok(packaged.build.files.includes("dictation-hotkeys.js"));
  for (const extension of ["ps1", "cs"]) assert.ok(packaged.build.extraResources.some(resource => resource.from === `scripts/dictation-keyboard.${extension}` && resource.to === `dictation/dictation-keyboard.${extension}`));
  const native = fs.readFileSync(path.join(__dirname, "scripts", "dictation-keyboard.cs"), "utf8");
  assert.match(native, /SetWindowsHookEx\(13/); assert.match(native, /GetDoubleClickTime\(\)/);
  assert.match(native, /expected\["foreground"\]/); assert.match(native, /expected\["pid"\]/); assert.match(native, /expected\["focus"\]/);
  assert.match(native, /if \(value\) \{ Unregister\(Shortcuts\); Suspended=true/);
  assert.match(native, /IgnoreUntilUp/);
  assert.doesNotMatch(native, /SetForegroundWindow|Clipboard|WriteAllText|ReadAllText/);
});

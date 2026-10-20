"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const { createInterface } = require("node:readline");
const { atomicWriteTextSync } = require("./utils");

const MODIFIERS = { Alt: 1, Ctrl: 2, Shift: 4, Win: 8 };
const KEYS = { Space: 32, Enter: 13, Tab: 9, Backspace: 8, Delete: 46, Insert: 45, Home: 36, End: 35,
  PageUp: 33, PageDown: 34, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  Ctrl: 17, Shift: 16, Alt: 18, Win: 91, Backquote: 192, Minus: 189, Equal: 187, BracketLeft: 219,
  BracketRight: 221, Backslash: 220, Semicolon: 186, Quote: 222, Comma: 188, Period: 190, Slash: 191 };

function parseDictationShortcut(value) {
  if (!value) return null;
  const parts = value.split("+");
  const key = parts.pop();
  const modifiers = parts.reduce((mask, part) => {
    if (!MODIFIERS[part] || mask & MODIFIERS[part]) throw new Error("Invalid dictation shortcut modifiers");
    return mask | MODIFIERS[part];
  }, 0);
  const bare = Boolean(MODIFIERS[key]);
  const vk = KEYS[key] ?? (/^[A-Z0-9]$/.test(key) ? key.charCodeAt(0) : /^F(?:[1-9]|1\d|2[0-4])$/.test(key) ? 111 + Number(key.slice(1)) : null);
  if (!vk || (!bare && !modifiers && !/^F\d+$/.test(key)) || bare && modifiers & MODIFIERS[key]) throw new Error("Use a modifier key or function key for dictation");
  return { vk, modifiers, bare };
}

function createDictationHotkeys({ app, getMainWindows, getFocusedMainWindow, getFocusedWebContents, spawnWorker = spawn, platform = process.platform, timers = globalThis, now = Date.now }) {
  const file = () => path.join(app.getPath("userData"), "dictation-hotkeys.json");
  let configuration = { holdHotkey: "", toggleHotkey: "" };
  let processHandle;
  let initialization;
  let available = false;
  let error = "";
  let doubleClickTime;
  let releaseTimer;
  let lastWindow;
  let session;
  let downAt = 0;
  let handsFree = false;
  let sequence = 0;
  const requests = new Map();
  const suspendedOwners = new Set();
  const snapshot = () => ({ available, ...configuration, error });
  const publish = () => getMainWindows().forEach(window => window.webContents.send("minicode:dictation:state", snapshot()));
  const command = (op, payload = {}) => new Promise((resolve, reject) => {
    const id = String(++sequence); requests.set(id, { resolve, reject });
    processHandle.stdin.write(JSON.stringify({ id, op, ...payload }) + "\n");
  });
  const send = action => {
    if (session && !session.window.isDestroyed()) session.window.webContents.send("minicode:dictation:event", { action, token: session.token, targetKind: session.targetKind });
  };
  const reset = () => {
    timers.clearTimeout(releaseTimer); releaseTimer = undefined;
    session = undefined; handsFree = false;
  };
  const finish = cancelled => {
    timers.clearTimeout(releaseTimer); releaseTimer = undefined;
    if (!session?.recording) return;
    session.recording = false; send(cancelled ? "cancel" : "stop");
  };
  const start = target => {
    const windows = getMainWindows();
    const own = windows.find(window => {
      const handle = window.getNativeWindowHandle();
      return (handle.length === 8 ? handle.readBigUInt64LE(0) : BigInt(handle.readUInt32LE(0))).toString() === target.foreground;
    });
    const window = own ?? (windows.includes(lastWindow) ? lastWindow : undefined) ?? getFocusedMainWindow() ?? windows[0];
    if (!window || window.isDestroyed()) return;
    const mainRendererFocused = own && getFocusedWebContents()?.id === own.webContents.id;
    session = { token: randomUUID(), window, senderId: window.webContents.id, target, targetKind: mainRendererFocused ? "composer" : "external", recording: true };
    handsFree = false;
    void command("active", { value: true }).catch(fail);
    send("start");
  };
  const onInput = event => {
    if (event.kind === "escape") { finish(true); return; }
    if (event.kind !== "key" || suspendedOwners.size) return;
    if (event.hotkey === "toggle") {
      if (event.phase === "down") { if (session) finish(false); else start(event.target); }
      return;
    }
    if (event.phase === "down") {
      downAt = now();
      if (releaseTimer && session?.recording) {
        timers.clearTimeout(releaseTimer); releaseTimer = undefined; handsFree = true;
      } else if (session?.recording && handsFree) finish(false);
      else if (!session) start(event.target);
    } else if (session?.recording && !handsFree) {
      if (now() - downAt >= doubleClickTime) finish(false);
      else releaseTimer = timers.setTimeout(() => finish(false), doubleClickTime);
    }
  };
  function fail(reason) {
    error = reason.message; available = false;
    finish(true); reset();
    for (const request of requests.values()) request.reject(reason);
    requests.clear(); publish();
  }
  async function initialize() {
    if (platform !== "win32") return snapshot();
    if (initialization) return initialization;
    initialization = (async () => {
      if (fs.existsSync(file())) configuration = JSON.parse(fs.readFileSync(file(), "utf8"));
      const script = app.isPackaged ? path.join(process.resourcesPath, "dictation", "dictation-keyboard.ps1") : path.join(__dirname, "scripts", "dictation-keyboard.ps1");
      await new Promise((resolve, reject) => {
        processHandle = spawnWorker("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script], { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
        processHandle.stdin.on("error", fail);
        processHandle.once("error", reason => { reject(reason); fail(reason); });
        processHandle.once("exit", code => { const reason = new Error(workerError.trim() || `Dictation keyboard worker stopped (${code})`); reject(reason); fail(reason); processHandle = undefined; initialization = undefined; });
        let workerError = "";
        processHandle.stderr.on("data", chunk => { workerError += String(chunk); error = workerError.trim(); });
        createInterface({ input: processHandle.stdout }).on("line", line => {
          const event = JSON.parse(line.replace(/^\uFEFF/, ""));
          if (event.kind === "ready") { doubleClickTime = event.doubleClickTime; resolve(); }
          else if (event.id) {
            const request = requests.get(event.id); requests.delete(event.id);
            if (event.ok) request?.resolve(event.value); else request?.reject(new Error(event.error));
          } else onInput(event);
        });
      });
      available = true;
      await command("configure", { hold: parseDictationShortcut(configuration.holdHotkey), toggle: parseDictationShortcut(configuration.toggleHotkey) });
      available = true; error = ""; publish(); return snapshot();
    })().catch(reason => { error = reason.message; publish(); return snapshot(); });
    return initialization;
  }
  return {
    async get() { await initialize(); return snapshot(); },
    noteFocusedWindow(window) { if (getMainWindows().includes(window)) lastWindow = window; },
    async configure(patch) {
      await initialize();
      if (!available) throw new Error(error || "Native dictation shortcuts are unavailable");
      const next = { ...configuration, ...patch };
      const hold = parseDictationShortcut(next.holdHotkey); const toggle = parseDictationShortcut(next.toggleHotkey);
      if (hold && toggle && hold.vk === toggle.vk && hold.modifiers === toggle.modifiers) throw new Error("The hold and single-tap shortcuts must be different");
      finish(true);
      await command("configure", { hold, toggle });
      try { atomicWriteTextSync(file(), JSON.stringify(next, null, 2)); }
      catch (reason) { await command("configure", { hold: parseDictationShortcut(configuration.holdHotkey), toggle: parseDictationShortcut(configuration.toggleHotkey) }); throw reason; }
      configuration = next; error = ""; publish(); return snapshot();
    },
    async suspend(senderId, value, source = "dictation") {
      const owner = `${senderId}:${source}`;
      if (value) suspendedOwners.add(owner); else suspendedOwners.delete(owner);
      if (value) finish(true);
      if (available) {
        try { await command("suspend", { value: suspendedOwners.size > 0 }); }
        catch (reason) { error = reason.message; publish(); throw reason; }
      }
    },
    async insert(senderId, token, text) {
      if (!session || session.token !== token || session.senderId !== senderId || session.recording) return false;
      return command("insert", { target: session.target, text });
    },
    async complete(senderId, token) {
      if (!session || session.token !== token || session.senderId !== senderId) return;
      reset(); if (available) await command("active", { value: false });
    },
    releaseWindow(senderId) {
      for (const owner of suspendedOwners) if (owner.startsWith(`${senderId}:`)) suspendedOwners.delete(owner);
      if (session?.senderId === senderId) { finish(true); reset(); }
      if (available) { void command("suspend", { value: suspendedOwners.size > 0 }).catch(fail); void command("active", { value: Boolean(session) }).catch(fail); }
    },
    async dispose() {
      timers.clearTimeout(releaseTimer);
      if (!processHandle) return;
      const worker = processHandle;
      const closed = new Promise(resolve => worker.once("exit", resolve));
      await command("shutdown"); worker.stdin.end(); await closed;
    },
  };
}

module.exports = { createDictationHotkeys, parseDictationShortcut };

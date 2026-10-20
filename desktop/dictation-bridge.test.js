"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const os = require("node:os");
const { EventEmitter } = require("node:events");
const preferences = require("./preferences");
const { RemoteDesktop } = require("./remote-desktop");

test("dictation preload and IPC carry the exact main-renderer owner and discard guest access", async () => {
  const handlers = new Map(); const listeners = new Map(); const ipcListeners = new Map(); const calls = [];
  const mainFrame = {}; const contents = { id: 17, mainFrame }; const window = { webContents: contents, isDestroyed: () => false };
  const event = { sender: contents, senderFrame: mainFrame };
  const state = { available: true, holdHotkey: "Ctrl+Shift+Space", toggleHotkey: "", error: "" };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console,
    require: name => name === "electron" ? { ipcMain: { on: (name, listener) => listeners.set(name, listener), handle: (name, handler) => handlers.set(name, handler) } } : require(name),
  });
  moduleBox.exports.init({ getWindowForSender: sender => sender === contents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "dictation-owner" }),
    dictationHotkeys: { get: async () => state,
      configure: async patch => { calls.push(["configure", patch]); return { ...state, ...patch }; },
      suspend: async (...args) => calls.push(["suspend", ...args]),
      insert: async (...args) => { calls.push(["insert", ...args]); return true; },
      complete: async (...args) => calls.push(["complete", ...args]),
    },
  });
  moduleBox.exports.registerIpcHandlers();
  let runtime;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({ ipcRenderer: { on: (channel, listener) => ipcListeners.set(channel, listener), removeListener: channel => ipcListeners.delete(channel), sendSync: () => ({}),
      invoke: async (channel, ...args) => handlers.get(channel)(event, ...args) }, contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  const native = runtime.desktop.dictation;
  assert.equal((await native.get()).available, true);
  await native.configure({ toggleHotkey: "Alt+F12" }); await native.suspend(true);
  assert.equal(await native.insert("original-token", "retained transcript"), true); await native.complete("original-token");
  assert.deepEqual(calls, [["configure", { toggleHotkey: "Alt+F12" }], ["suspend", 17, true], ["insert", 17, "original-token", "retained transcript"], ["complete", 17, "original-token"]]);
  for (const channel of ["get", "configure", "suspend", "insert", "complete"]) assert.throws(() => handlers.get("minicode:dictation:" + channel)({ sender: contents, senderFrame: {} }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.throws(() => handlers.get("minicode:dictation:configure")(event, { unknown: "invalid" }), /Invalid dictation/);
  let delivered; const stop = native.onEvent(value => { delivered = value; });
  const payload = { action: "start", token: "actual-token", targetKind: "external" };
  ipcListeners.get("minicode:dictation:event")({}, payload); assert.equal(delivered, payload); stop();
  assert.equal(ipcListeners.has("minicode:dictation:event"), false);
});

async function localVoiceFixture(t, { local = true } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-local-voice-"));
  const preferencesFile = path.join(directory, "preferences.json");
  preferences.init(preferencesFile);
  let focused;
  let contentsId = 0;
  let nextLoad;
  const receivers = new Map();
  class Window extends EventEmitter {
    static getFocusedWindow() { return focused; }
    constructor(options) {
      super(); this.options = options; this.dead = false; this.visible = false; this.sent = [];
      this.webContents = Object.assign(new EventEmitter(), { id: ++contentsId, mainFrame: {}, setWindowOpenHandler() {},
        send: (channel, payload) => { this.sent.push({ channel, payload }); receivers.get(this)?.get(channel)?.({}, payload); } });
    }
    isDestroyed() { return this.dead; }
    isMinimized() { return false; }
    isVisible() { return this.visible; }
    isMaximized() { return false; }
    isFocused() { return focused === this; }
    show() { this.visible = true; }
    focus() { focused = this; }
    getBounds() { return { x: 0, y: 0, width: 1440, height: 920 }; }
    getNormalBounds() { return this.getBounds(); }
    async loadURL(url) { this.loaded = url; const pending = nextLoad; nextLoad = undefined; await pending; }
    destroy() { this.dead = true; this.emit("closed"); }
  }
  const app = { isPackaged: true, getPath: () => directory, getLocale: () => "zh-CN" };
  const managerBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "window-manager.js"), "utf8"), {
    module: managerBox, process, __dirname, Buffer, URL, console, setTimeout, clearTimeout,
    require: name => name === "electron" ? { BrowserWindow: Window, app, shell: {} } : require(name),
  });
  const manager = managerBox.exports;
  manager.init({ frontendDevUrl: "http://127.0.0.1:5173/" });
  t.after(() => {
    for (const window of manager.getMainWindows()) window.destroy();
    assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
    assert.match(path.basename(directory), /^minicode-local-voice-/);
    fs.rmSync(directory, { recursive: true });
  });
  const localWindow = local ? await manager.createNewWindow({ conversationId: "retained-local-chat" }) : null;
  const remoteRuntime = { apiBaseUrl: "http://127.0.0.1:45123", wsBaseUrl: "ws://127.0.0.1:45123", runtimeToken: "remote-only-token", revision: 91 };
  const remoteWindow = await manager.createNewWindow({ remoteHostId: "host-a", runtime: remoteRuntime, conversationId: "retained-remote-chat" });
  let localRuntime = { apiBaseUrl: "http://127.0.0.1:8011", wsBaseUrl: "ws://127.0.0.1:8011", runtimeToken: "local-only-token", revision: 1 };
  const handlers = new Map(), created = [];
  const ipcBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: ipcBox, process, console,
    require: name => name === "electron" ? { app, ipcMain: { on: (channel, listener) => handlers.set(channel, listener), handle: (channel, handler) => handlers.set(channel, handler) } } : require(name),
  });
  const runtimeFor = window => manager.getWindowRecord(window).runtime ?? localRuntime;
  ipcBox.exports.init({ getWindowForSender: manager.getWindowForSender, getWindowRecord: manager.getWindowRecord,
    getMainWindows: manager.getMainWindows, focusMainWindow: manager.focusMainWindow, getRuntimeConfig: runtimeFor,
    getBackendRuntimeConfig: () => localRuntime, createNewWindow: async options => { created.push(structuredClone(options)); return manager.createNewWindow(options); },
    getRemoteDesktop: () => ({ invokes: RemoteDesktop.prototype.invokes, invoke: () => assert.fail("Local voice must never invoke a remote transport") }),
    desktopPreferences: { ...preferences, fileOpenTargets: () => [], terminalShellTargets: () => [] },
  });
  ipcBox.exports.registerIpcHandlers();
  const preload = window => {
    const listeners = new Map(), events = [];
    receivers.set(window, listeners);
    const event = { sender: window.webContents, senderFrame: window.webContents.mainFrame };
    let runtime;
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
      require: () => ({ ipcRenderer: { on: (channel, listener) => listeners.set(channel, listener), removeListener: channel => listeners.delete(channel),
        sendSync: (channel, ...args) => { const request = { ...event }; handlers.get(channel)(request, ...args); return request.returnValue; },
        invoke: async (channel, ...args) => handlers.get(channel)(event, ...args) }, contextBridge: { exposeInMainWorld: (_name, value) => { runtime = value; } } }),
      CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
      process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: { dispatchEvent: event => events.push(event) }, console,
    });
    return { runtime, native: runtime.desktop.dictation, preferences: runtime.desktop.preferences, event, events, listeners };
  };
  return { directory, preferencesFile, manager, localWindow, remoteWindow, remoteRuntime, handlers, created, preload,
    get localRuntime() { return localRuntime; }, setLocalRuntime: value => { localRuntime = value; },
    deferNextLoad() { let release; nextLoad = new Promise(resolve => { release = resolve; }); return release; } };
}

const legacyVoice = (changes = {}) => ({ speechEnabled: true, speechProvider: "custom", speechModel: "legacy-local-asr", speechLanguage: "zh",
  microphoneId: "legacy-local-mic", dictationDictionary: ["完整词组,不拆开", "MiniCode"], ...changes });

test("remote dictation reads the current local backend runtime through the real preload and rejects guest senders", async t => {
  const f = await localVoiceFixture(t);
  const remote = f.preload(f.remoteWindow);
  assert.equal(remote.runtime.apiBaseUrl, f.remoteRuntime.apiBaseUrl);
  assert.deepEqual(structuredClone(await remote.native.getLocalRuntime()), { apiBaseUrl: f.localRuntime.apiBaseUrl, runtimeToken: "local-only-token", revision: 1 });
  f.setLocalRuntime({ apiBaseUrl: "http://127.0.0.1:8012", runtimeToken: "restarted-local-token", revision: 2 });
  assert.deepEqual(structuredClone(await remote.native.getLocalRuntime()), f.localRuntime);
  for (const method of ["getLocalRuntime", "openProviderSettings", "consumeProviderSettings", "migratePreferences"]) {
    assert.throws(() => f.handlers.get("minicode:dictation:" + method)({ ...remote.event, senderFrame: {} }, legacyVoice()), { code: "ERR_UNTRUSTED_IPC_SENDER" });
    assert.throws(() => f.handlers.get("minicode:dictation:" + method)({ ...remote.event, sender: { mainFrame: {} } }, legacyVoice()), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  }
  assert.deepEqual(f.created, []);
});

test("remote voice configuration focuses an existing local provider route without replacing either conversation", async t => {
  const f = await localVoiceFixture(t);
  const local = f.preload(f.localWindow), remote = f.preload(f.remoteWindow);
  const localNavigation = f.manager.getWindowRecord(f.localWindow).initialNavigation;
  const remoteNavigation = f.manager.getWindowRecord(f.remoteWindow).initialNavigation;
  assert.equal(await remote.native.openProviderSettings(), true);
  assert.equal(f.manager.getFocusedMainWindow(), f.localWindow);
  assert.deepEqual(local.events.map(event => ({ type: event.type, detail: structuredClone(event.detail) })), [{ type: "open-settings", detail: { tab: "provider" } }]);
  assert.deepEqual(remote.events, []); assert.deepEqual(f.created, []);
  assert.equal(f.manager.getWindowRecord(f.localWindow).initialNavigation, localNavigation);
  assert.equal(f.manager.getWindowRecord(f.remoteWindow).initialNavigation, remoteNavigation);
  assert.equal(localNavigation.conversationId, "retained-local-chat"); assert.equal(remoteNavigation.conversationId, "retained-remote-chat");
});

test("remote voice creates a local provider settings window and initial navigation is consumed exactly once", async t => {
  const f = await localVoiceFixture(t, { local: false });
  const remote = f.preload(f.remoteWindow);
  assert.equal(await remote.native.openProviderSettings(), true);
  assert.deepEqual(f.created, [{ settingsTab: "provider" }]);
  const localWindow = f.manager.getFocusedMainWindow(), record = f.manager.getWindowRecord(localWindow);
  assert.equal(record.remoteHostId, null); assert.equal(record.runtime, null);
  assert.equal(localWindow.options.webPreferences.partition, undefined);
  const local = f.preload(localWindow);
  assert.equal(local.runtime.apiBaseUrl, f.localRuntime.apiBaseUrl);
  assert.deepEqual(structuredClone(local.runtime.initialNavigation), { conversationId: null });
  assert.equal(record.pendingProviderSettings, true);
  local.runtime.desktop.windowControls.consumeInitialNavigation();
  const refreshed = { ...local.event }; f.handlers.get("minicode:runtime:get")(refreshed);
  assert.equal(refreshed.returnValue.initialNavigation, null);
  assert.equal(f.manager.getWindowRecord(f.remoteWindow).initialNavigation.conversationId, "retained-remote-chat");
  const explicit = await f.manager.createNewWindow({ conversationId: "explicit-local-chat", settingsTab: "provider" });
  assert.deepEqual(structuredClone(f.manager.getWindowRecord(explicit).initialNavigation), { conversationId: "explicit-local-chat" });
  assert.equal(f.manager.getWindowRecord(explicit).pendingProviderSettings, true);
});

test("partition voice migration atomically merges full dictionary entries while only local settings can select the machine service", async t => {
  const f = await localVoiceFixture(t);
  const remote = f.preload(f.remoteWindow), local = f.preload(f.localWindow);
  const changes = []; const stop = local.preferences.onChanged(value => changes.push(value));
  const first = await remote.native.migratePreferences(legacyVoice({ speechProvider: "anthropic", speechModel: "remote-must-not-be-used", microphoneId: "remote-device" }));
  assert.equal(first.speechModel, ""); assert.equal(first.speechProvider, ""); assert.equal(first.speechEnabled, false); assert.equal(first.microphoneId, "");
  assert.deepEqual([...first.dictationDictionary], ["完整词组,不拆开", "MiniCode"]);
  assert.equal(changes.length, 1); assert.deepEqual([...changes[0].dictationDictionary], [...first.dictationDictionary]);
  await remote.native.migratePreferences(legacyVoice({ dictationDictionary: ["not imported a second time"] }));
  const hostB = await f.manager.createNewWindow({ remoteHostId: "host-b", runtime: f.remoteRuntime });
  await f.preload(hostB).native.migratePreferences(legacyVoice({ dictationDictionary: ["MiniCode", "另一台主机的完整词组"] }));
  const migrated = await local.native.migratePreferences(legacyVoice({ dictationDictionary: ["local-only entry"] }));
  assert.equal(migrated.speechProvider, "custom"); assert.equal(migrated.speechModel, "legacy-local-asr"); assert.equal(migrated.microphoneId, "legacy-local-mic");
  assert.deepEqual([...migrated.dictationDictionary], ["完整词组,不拆开", "MiniCode", "另一台主机的完整词组", "local-only entry"]);
  const persisted = JSON.parse(fs.readFileSync(f.preferencesFile, "utf8"));
  assert.deepEqual(persisted.dictationMigratedPartitions, ["remote:host-a", "remote:host-b", "local"]);
  assert.deepEqual(persisted.dictationDictionary, [...migrated.dictationDictionary]);
  preferences.init(f.preferencesFile);
  assert.equal(preferences.getPreferences().speechModel, "legacy-local-asr");
  assert.deepEqual(preferences.getPreferences().dictationMigratedPartitions, persisted.dictationMigratedPartitions);
  stop();
});

test("explicit machine voice choices win over later legacy migration and use the same preferences broadcast", async t => {
  const f = await localVoiceFixture(t);
  const remote = f.preload(f.remoteWindow), local = f.preload(f.localWindow);
  const changed = []; remote.preferences.onChanged(value => changed.push(value));
  await local.preferences.set({ speechProvider: "openai", speechEnabled: false, speechModel: "chosen-local-asr", speechLanguage: "en", microphoneId: "chosen-local-mic", dictationDictionary: ["manually authored phrase"] });
  const result = await local.native.migratePreferences(legacyVoice());
  assert.equal(result.speechProvider, "openai"); assert.equal(result.speechModel, "chosen-local-asr"); assert.equal(result.speechEnabled, false);
  assert.equal(result.speechLanguage, "en"); assert.equal(result.microphoneId, "chosen-local-mic");
  assert.deepEqual([...result.dictationDictionary], ["manually authored phrase", "完整词组,不拆开", "MiniCode"]);
  assert.equal(changed.length, 2); assert.equal(changed.at(-1).speechModel, "chosen-local-asr");
  await assert.rejects(local.preferences.set({ dictationConfiguredFields: [] }), /Unsupported desktop preference/);
  await assert.rejects(remote.native.migratePreferences(legacyVoice({ dictationDictionary: [3] })), /Invalid legacy dictation/);
  assert.deepEqual(preferences.getPreferences().dictationMigratedPartitions, ["local"]);
  preferences.init(f.preferencesFile);
  assert.equal(preferences.getPreferences().speechModel, "chosen-local-asr");
});

test("a remote-window microphone edit preserves that device while the later local migration restores the untouched service fields", async t => {
  const f = await localVoiceFixture(t, { local: false });
  const remote = f.preload(f.remoteWindow);
  await remote.native.migratePreferences(legacyVoice({ speechProvider: "anthropic", speechModel: "remote-service", microphoneId: "remote-old-mic" }));
  await remote.preferences.set({ microphoneId: "new-machine-mic" });
  const localWindow = await f.manager.createNewWindow({ settingsTab: "provider" });
  const local = f.preload(localWindow);
  const imported = await local.native.migratePreferences(legacyVoice());
  assert.equal(imported.microphoneId, "new-machine-mic");
  assert.equal(imported.speechProvider, "custom"); assert.equal(imported.speechModel, "legacy-local-asr"); assert.equal(imported.speechEnabled, true);
  assert.deepEqual([...imported.dictationConfiguredFields], ["microphoneId"]);
  preferences.init(f.preferencesFile);
  assert.equal(preferences.getPreferences().microphoneId, "new-machine-mic");
  assert.equal(preferences.getPreferences().speechModel, "legacy-local-asr");
});

test("explicit model and device edits retain only their own field ownership during first local legacy migration", async t => {
  const f = await localVoiceFixture(t);
  const local = f.preload(f.localWindow);
  await local.preferences.set({ microphoneId: "explicit-machine-mic" });
  await local.preferences.set({ speechModel: "explicit-machine-model" });
  const imported = await local.native.migratePreferences(legacyVoice({ speechLanguage: "ja" }));
  assert.equal(imported.speechModel, "explicit-machine-model"); assert.equal(imported.microphoneId, "explicit-machine-mic");
  assert.equal(imported.speechProvider, "custom"); assert.equal(imported.speechLanguage, "ja"); assert.equal(imported.speechEnabled, true);
  assert.deepEqual([...imported.dictationConfiguredFields], ["microphoneId", "speechModel"]);
});

test("provider settings intent survives choosing a local window whose load and React consumer are not ready", async t => {
  const f = await localVoiceFixture(t, { local: false });
  const remote = f.preload(f.remoteWindow);
  const release = f.deferNextLoad();
  const loading = f.manager.createNewWindow({ conversationId: "inflight-local-conversation" });
  const target = f.manager.getMainWindows().find(window => !f.manager.getWindowRecord(window).remoteHostId);
  assert.ok(target);
  const record = f.manager.getWindowRecord(target);
  assert.equal(await remote.native.openProviderSettings(), true);
  assert.equal(record.pendingProviderSettings, true);
  assert.deepEqual(f.created, []);
  assert.equal(target.sent.some(event => event.channel === "minicode:menu:settings"), true);
  release(); assert.equal(await loading, target);
  const local = f.preload(target);
  local.runtime.desktop.windowControls.consumeInitialNavigation();
  assert.equal(record.initialNavigation, null);
  assert.equal(record.pendingProviderSettings, true);
  assert.equal(await local.native.consumeProviderSettings(), true);
  assert.equal(await local.native.consumeProviderSettings(), false);
  assert.equal(record.pendingProviderSettings, false);
  assert.equal(f.manager.getWindowRecord(f.remoteWindow).initialNavigation.conversationId, "retained-remote-chat");
});

test("provider settings intent survives a local reload gap and only its original main-frame consumer can acknowledge it", async t => {
  const f = await localVoiceFixture(t);
  const old = f.preload(f.localWindow), remote = f.preload(f.remoteWindow);
  old.runtime.desktop.windowControls.consumeInitialNavigation();
  old.listeners.clear();
  await remote.native.openProviderSettings();
  const record = f.manager.getWindowRecord(f.localWindow);
  f.localWindow.webContents.emit("did-navigate");
  assert.equal(record.pendingProviderSettings, true);
  assert.equal(record.initialNavigation, null);
  assert.deepEqual(old.events, []);
  assert.equal(await remote.native.consumeProviderSettings(), false);
  assert.equal(record.pendingProviderSettings, true);
  const local = f.preload(f.localWindow);
  assert.throws(() => f.handlers.get("minicode:dictation:consumeProviderSettings")({ ...local.event, senderFrame: {} }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.equal(record.pendingProviderSettings, true);
  assert.equal(await local.native.consumeProviderSettings(), true);
  assert.equal(await local.native.consumeProviderSettings(), false);
});

test("provider settings intent is independent from early WebSocket initial-navigation consumption in a newly created local window", async t => {
  const f = await localVoiceFixture(t, { local: false });
  const remote = f.preload(f.remoteWindow);
  const release = f.deferNextLoad();
  const opening = remote.native.openProviderSettings();
  const target = f.manager.getMainWindows().find(window => !f.manager.getWindowRecord(window).remoteHostId);
  const record = f.manager.getWindowRecord(target);
  assert.deepEqual(f.created, [{ settingsTab: "provider" }]);
  assert.equal(record.pendingProviderSettings, true);
  assert.deepEqual(structuredClone(record.initialNavigation), { conversationId: null });
  const beforeReact = { sender: target.webContents, senderFrame: target.webContents.mainFrame };
  f.handlers.get("minicode:window:initialNavigation:consume")(beforeReact);
  assert.equal(record.initialNavigation, null); assert.equal(record.pendingProviderSettings, true);
  release(); assert.equal(await opening, true);
  const local = f.preload(target);
  assert.equal(local.runtime.initialNavigation, null);
  assert.equal(await local.native.consumeProviderSettings(), true);
  assert.equal(await local.native.consumeProviderSettings(), false);
  assert.equal(record.remoteHostId, null); assert.equal(target.options.webPreferences.partition, undefined);
});

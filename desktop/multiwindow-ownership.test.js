"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { releaseEditorWindow } = require("./window-lifecycle");

function load(file, overrides = {}, globals = {}) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, file), "utf8"), {
    module, process, Buffer, URL, console, setTimeout, clearTimeout,
    require: (name) => Object.hasOwn(overrides, name) ? overrides[name] : require(name),
    ...globals,
  }, { filename: file });
  return module.exports;
}

function temporaryProfile(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-window-owner-"));
  t.after(() => {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true, force: true });
  });
  return root;
}

function windowFixture(t) {
  const profile = temporaryProfile(t);
  let focused = null;
  class Window extends EventEmitter {
    static getFocusedWindow() { return focused; }
    constructor(options) {
      super(); this.options = options; this.dead = false; this.shows = 0; this.sent = [];
      this.webContents = Object.assign(new EventEmitter(), {
        mainFrame: {}, setWindowOpenHandler() {}, getZoomLevel: () => 0, setZoomLevel() {},
        reload: () => { this.reloads = (this.reloads || 0) + 1; },
        reloadIgnoringCache: () => { this.forceReloads = (this.forceReloads || 0) + 1; },
        send: (channel, payload) => this.sent.push({ channel, payload }),
      });
    }
    isDestroyed() { return this.dead; }
    isMinimized() { return false; }
    isVisible() { return this.shows > 0; }
    isMaximized() { return this.maximized === true; }
    isFocused() { return focused === this; }
    maximize() { this.maximized = true; this.emit("maximize"); }
    unmaximize() { this.maximized = false; this.emit("unmaximize"); }
    show() { this.shows++; }
    focus() { focused = this; }
    getBounds() { return { x: 10, y: 20, width: 1200, height: 800 }; }
    getNormalBounds() { return this.getBounds(); }
    async loadURL(url) { this.loaded = url; }
    close() { this.emit("close"); this.destroy(); }
    destroy() { this.dead = true; this.emit("closed"); }
  }
  const created = [], closed = [];
  const manager = load("window-manager.js", { electron: { BrowserWindow: Window, shell: {},
    app: { isPackaged: true, getPath: () => profile } } });
  manager.init({ frontendDevUrl: "http://127.0.0.1:5173/", onMainWindowCreated: (_win, record) => created.push(record),
    onMainWindowClosed: (_win, record) => closed.push(record) });
  t.after(() => { for (const win of manager.getMainWindows()) win.destroy(); });
  return { manager, created, closed, profile };
}

test("new windows own distinct instances, captured callbacks and reusable persistence slots", async (t) => {
  const { manager, created, closed, profile } = windowFixture(t);
  const primary = await manager.createMainWindow();
  const secondary = await manager.createNewWindow();
  assert.equal(await manager.createMainWindow(), primary);
  assert.equal(created[0].windowOwnerId, "primary");
  assert.equal(created[0].initialNavigation, null);
  assert.equal(created[1].windowOwnerId, "secondary-1");
  assert.equal(created[1].initialNavigation.conversationId, null);
  assert.notEqual(created[0].windowInstanceId, created[1].windowInstanceId);
  assert.match(created[0].windowBindingToken, /^[0-9a-f]{64}$/);
  assert.notEqual(created[0].windowBindingToken, created[1].windowBindingToken);
  assert.equal(primary.options.webPreferences.additionalArguments.includes(`--minicode-window-instance-id=${created[0].windowInstanceId}`), true);
  primary.emit("ready-to-show");
  assert.equal(primary.shows, 2);
  assert.equal(secondary.shows, 0);
  manager.focusMainWindow(secondary);
  assert.equal(manager.getFocusedMainWindow(), secondary);
  let prevented = false;
  primary.webContents.emit("before-input-event", { preventDefault() { prevented = true; } }, { type: "keyDown", control: true, key: "`" });
  assert.equal(prevented, true);
  assert.equal(primary.sent.at(-1).channel, "minicode:shortcut:terminal");
  assert.equal(secondary.sent.length, 0);
  secondary.close();
  assert.equal(manager.getWindowForSender(secondary.webContents), null);
  assert.equal(closed[0], created[1]);
  assert.equal(fs.existsSync(path.join(profile, "window-state-secondary-1.json")), true);
  const reopened = await manager.createNewWindow();
  assert.equal(manager.getWindowRecord(reopened).initialNavigation.conversationId, null);
  assert.equal(manager.getWindowRecord(reopened).windowOwnerId, "secondary-1");
  assert.notEqual(manager.getWindowRecord(reopened).windowInstanceId, closed[0].windowInstanceId);
  assert.notEqual(manager.getWindowRecord(reopened).windowBindingToken, closed[0].windowBindingToken);
  assert.equal(manager.getWindowByInstanceId(closed[0].windowInstanceId), null);
  assert.equal(manager.getWindowForSender(primary.webContents), primary);
});

test("a remote window retains its actual runtime and a separate persistent storage partition", async (t) => {
  const { manager } = windowFixture(t);
  const local = await manager.createMainWindow();
  const runtime = { apiBaseUrl: "http://127.0.0.1:59101", wsBaseUrl: "ws://127.0.0.1:59101", runtimeToken: "remote-runtime",
    remoteHost: { id: "actual-host", generation: "actual-generation" } };
  const remote = await manager.createNewWindow({ remoteHostId: "actual-host", runtime, conversationId: "remote-conversation" });
  const record = manager.getWindowRecord(remote);
  assert.equal(record.runtime, runtime);
  assert.equal(record.initialNavigation.conversationId, "remote-conversation");
  assert.equal(remote.options.webPreferences.partition, "persist:minicode-host-actual-host");
  assert.equal(local.options.webPreferences.partition, undefined);
  assert.ok(remote.options.webPreferences.additionalArguments.includes("--minicode-remote-host-id=actual-host"));
});

test("remote preload cannot borrow the local runtime if its selected host binding is missing", () => {
  const exposed = [];
  const processOwner = { ...process, contextIsolated: true, argv: ["electron", "--minicode-remote-host-id=host-a"],
    env: { MINICODE_RUNTIME_TOKEN: "local-must-not-be-borrowed" } };
  let runtime = { apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000", runtimeToken: "local-token" };
  const electron = { ipcRenderer: { sendSync: () => runtime, on() {} }, contextBridge: { exposeInMainWorld: (_name, value) => exposed.push(value) } };
  assert.throws(() => load("preload.js", { electron }, { process: processOwner }), /selected remote host's runtime binding/);
  assert.equal(exposed.length, 0);
  runtime = { apiBaseUrl: "http://127.0.0.1:59101", wsBaseUrl: "ws://127.0.0.1:59101", runtimeToken: "remote-token", remoteHost: { id: "host-a" } };
  load("preload.js", { electron }, { process: processOwner });
  assert.equal(exposed[0].runtimeToken, "remote-token");
  assert.equal(exposed[0].apiBaseUrl, runtime.apiBaseUrl);
});

test("connection IPC reports the real error to the renderer without its internal transport wrapper", async () => {
  const handlers = new Map(), exposed = [];
  const ipcMain = { handle: (name, handler) => handlers.set(name, handler), on() {} };
  const window = { isDestroyed: () => false, webContents: { mainFrame: {}, getURL: () => "file:///MiniCode/index.html" } };
  const event = { sender: window.webContents, senderFrame: window.webContents.mainFrame };
  const ipc = load("ipc-handlers.js", { electron: { ipcMain, app: {}, dialog: {}, shell: {} } });
  ipc.init({ getWindowForSender: () => window, getWindowRecord: () => ({ windowInstanceId: "owned-window" }), appendDesktopLog() {},
    getRemoteDesktop: () => ({ async open() { throw new Error("ssh: connection refused"); }, async connect() { return { ready: { bridge_token: "private-secret" } }; } }) });
  ipc.registerIpcHandlers();
  const runtime = { apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" };
  load("preload.js", { electron: { ipcRenderer: { sendSync: () => runtime, on() {}, invoke: (channel, ...args) => handlers.get(channel)(event, ...args) },
    contextBridge: { exposeInMainWorld: (_name, value) => exposed.push(value) } } }, { process: { ...process, contextIsolated: true, argv: [], env: {} } });
  await assert.rejects(exposed[0].desktop.connections.open("owned-host"), error => error.message === "ssh: connection refused");
  assert.equal(await exposed[0].desktop.connections.connect("owned-host"), undefined);
});

test("IPC derives the actual secondary owner for controls, dialogs, resources and updater ACKs", async (t) => {
  const { manager } = windowFixture(t);
  const primary = await manager.createMainWindow();
  const secondary = await manager.createNewWindow();
  const handlers = new Map(), dialogs = [], browserCalls = [], terminalCalls = [], activityCalls = [];
  const ipc = load("ipc-handlers.js", { electron: {
    ipcMain: { on: (channel, handler) => handlers.set(channel, handler), handle: (channel, handler) => handlers.set(channel, handler) },
    dialog: { async showOpenDialog(owner) { dialogs.push(owner); return { canceled: true }; } },
  } });
  primary.minimize = () => { throw new Error("Primary should stay open"); };
  secondary.minimize = () => { secondary.minimized = true; };
  ipc.init({ getWindowForSender: manager.getWindowForSender, getWindowRecord: manager.getWindowRecord,
    getRuntimeConfig: (win) => manager.getWindowRecord(win),
    embeddedBrowserManager: { create: (payload) => browserCalls.push(payload) },
    ptyManager: { spawnSession: (...args) => terminalCalls.push(args) },
    updater: { updateActivity: (...args) => activityCalls.push(args) },
  });
  ipc.registerIpcHandlers();
  const sender = { sender: secondary.webContents, senderFrame: secondary.webContents.mainFrame };
  const instance = manager.getWindowRecord(secondary).windowInstanceId;
  handlers.get("minicode:runtime:get")(sender);
  assert.equal(sender.returnValue.windowInstanceId, instance);
  handlers.get("minicode:window:minimize")(sender);
  assert.equal(secondary.minimized, true);
  await handlers.get("minicode:pickDirectory")(sender);
  assert.equal(dialogs[0], secondary);
  await handlers.get("minicode:embeddedBrowser:create")(sender, { conversation_id: "same-conversation", window_instance_id: manager.getWindowRecord(primary).windowInstanceId });
  assert.equal(browserCalls[0].window_instance_id, instance);
  handlers.get("minicode:pty:spawn")(sender, "C:/workspace", "same-conversation");
  assert.equal(terminalCalls[0][2], instance);
  handlers.get("minicode:update:activity")(sender, { requestIds: ["foreign-request"] });
  assert.equal(activityCalls[0][0], instance);
  assert.throws(() => handlers.get("minicode:runtime:get")({ ...sender, senderFrame: {} }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  secondary.close();
  assert.throws(() => handlers.get("minicode:runtime:get")(sender), { code: "ERR_UNTRUSTED_IPC_SENDER" });
});

test("opening an existing conversation creates a distinct window and consumes its initial route once", async (t) => {
  const { manager } = windowFixture(t);
  const primary = await manager.createMainWindow();
  const handlers = new Map();
  const ipc = load("ipc-handlers.js", { electron: { ipcMain: {
    on: (channel, handler) => handlers.set(channel, handler), handle: (channel, handler) => handlers.set(channel, handler),
  } } });
  ipc.init({ getWindowForSender: manager.getWindowForSender, getWindowRecord: manager.getWindowRecord,
    getRuntimeConfig: win => ({ windowOwnerId: manager.getWindowRecord(win).windowOwnerId }),
    createNewWindow: manager.createNewWindow, focusMainWindow: manager.focusMainWindow });
  ipc.registerIpcHandlers();
  const source = { sender: primary.webContents, senderFrame: primary.webContents.mainFrame };
  const receipt = await handlers.get("minicode:window:openConversation")(source, "existing-running-thread");
  const opened = manager.getWindowByInstanceId(receipt.windowInstanceId);
  assert.notEqual(opened, primary); assert.equal(receipt.windowOwnerId, "secondary-1");
  assert.equal(manager.getWindowRecord(primary).initialNavigation, null);
  assert.equal(manager.getFocusedMainWindow(), opened);
  const target = { sender: opened.webContents, senderFrame: opened.webContents.mainFrame };
  handlers.get("minicode:runtime:get")(target);
  assert.equal(target.returnValue.initialNavigation.conversationId, "existing-running-thread");
  assert.equal(manager.getWindowRecord(opened).initialNavigation.conversationId, "existing-running-thread");
  handlers.get("minicode:runtime:get")(target);
  assert.equal(target.returnValue.initialNavigation.conversationId, "existing-running-thread");
  handlers.get("minicode:window:initialNavigation:consume")(target);
  assert.equal(target.returnValue, true);
  handlers.get("minicode:runtime:get")(target);
  assert.equal(target.returnValue.initialNavigation, null);
  const empty = await manager.createNewWindow();
  assert.equal(manager.getWindowRecord(empty).initialNavigation.conversationId, null);
  assert.throws(() => handlers.get("minicode:window:openConversation")({ ...source, senderFrame: {} }, "thread"), { code: "ERR_UNTRUSTED_IPC_SENDER" });
});

test("native restored/maximize/unmaximize state follows its own renderer even while another window is focused", async (t) => {
  const { manager, profile } = windowFixture(t);
  fs.writeFileSync(path.join(profile, "window-state.json"), JSON.stringify({ isMaximized: true }));
  const primary = await manager.createMainWindow();
  const secondary = await manager.createNewWindow();
  manager.focusMainWindow(primary);
  const handlers = new Map();
  const ipc = load("ipc-handlers.js", { electron: { ipcMain: {
    on: (channel, handler) => handlers.set(channel, handler),
    handle: (channel, handler) => handlers.set(channel, handler),
  } } });
  ipc.init({ getWindowForSender: manager.getWindowForSender, getWindowRecord: manager.getWindowRecord });
  ipc.registerIpcHandlers();
  const primarySender = { sender: primary.webContents, senderFrame: primary.webContents.mainFrame };
  const secondarySender = { sender: secondary.webContents, senderFrame: secondary.webContents.mainFrame };
  const getState = handlers.get("minicode:window:state");
  const toggle = handlers.get("minicode:window:maximize");
  const states = win => win.sent.filter(event => event.channel === "minicode:window:state").map(event => event.payload.isMaximized);

  // Restoring a maximized native window happens before the renderer subscribes.
  assert.equal(getState(primarySender).isMaximized, true);
  assert.equal(getState(secondarySender).isMaximized, false);
  assert.equal(getState(primarySender).isFocused, true);
  assert.equal(getState(secondarySender).isFocused, false);
  assert.deepEqual(states(primary), [true]);
  assert.deepEqual(states(secondary), []);

  assert.equal(toggle(secondarySender), true);
  assert.equal(getState(secondarySender).isMaximized, true);
  assert.equal(getState(primarySender).isMaximized, true);
  assert.deepEqual(states(secondary), [true]);
  assert.deepEqual(states(primary), [true]);
  assert.equal(toggle(secondarySender), true);
  assert.equal(getState(secondarySender).isMaximized, false);
  assert.deepEqual(states(secondary), [true, false]);

  // Native titlebar/double-click changes use the same state publication path.
  primary.unmaximize();
  assert.equal(getState(primarySender).isMaximized, false);
  assert.deepEqual(states(primary), [true, false]);
  assert.deepEqual(states(secondary), [true, false]);
  assert.throws(() => getState({ ...secondarySender, senderFrame: {} }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  secondary.close();
  assert.throws(() => getState(secondarySender), { code: "ERR_UNTRUSTED_IPC_SENDER" });
});

test("blocked native closes and reloads replay only their own current unload intent", async (t) => {
  const { manager } = windowFixture(t);
  const primary = await manager.createMainWindow(), secondary = await manager.createNewWindow();
  primary.emit("close");
  const closing = manager.captureUnloadIntent(primary);
  assert.equal(closing.action, "close");
  primary.webContents.emit("will-prevent-unload", {});
  const reloading = manager.captureUnloadIntent(primary);
  assert.equal(reloading.action, "reload");
  assert.equal(manager.resumeUnload(primary, closing.id), false);
  assert.equal(manager.resumeUnload(secondary, reloading.id), false);
  assert.equal(manager.resumeUnload(primary, reloading.id), true);
  assert.equal(primary.isDestroyed(), false);
  assert.equal(primary.reloads, 1);
  manager.reloadWindow(primary, true);
  const forceReloading = manager.captureUnloadIntent(primary);
  assert.equal(forceReloading.ignoreCache, true);
  assert.equal(manager.resumeUnload(primary, forceReloading.id), true);
  assert.equal(primary.forceReloads, 2);
  primary.webContents.emit("did-navigate", {});
  assert.equal(manager.resumeUnload(primary, forceReloading.id), false);
  primary.emit("close");
  const finalClose = manager.captureUnloadIntent(primary);
  assert.equal(manager.resumeUnload(primary, finalClose.id), true);
  assert.equal(primary.isDestroyed(), true);
  assert.equal(secondary.isDestroyed(), false);
});

function resourceWindows() {
  const windows = new Map();
  for (const instance of ["instance-a", "instance-b"]) {
    windows.set(instance, { dead: false, events: [], children: [], isDestroyed() { return this.dead; },
      getContentBounds: () => ({ width: 1000, height: 800 }),
      webContents: { getZoomFactor: () => 1, send(channel, payload) { windows.get(instance).events.push({ channel, payload }); } },
      contentView: { addChildView(view) { windows.get(instance).children.push(view); }, removeChildView(view) { const children = windows.get(instance).children; children.splice(children.indexOf(view), 1); } },
    });
  }
  return windows;
}

test("same-conversation browser tabs remain independent across windows and late closed work cannot publish", async () => {
  const windows = resourceWindows();
  let sequence = 0, resolveDns;
  class View {
    constructor() {
      this.bounds = { x: 0, y: 0, width: 0, height: 0 };
      this.webContents = Object.assign(new EventEmitter(), {
        id: ++sequence, dead: false, url: "", navigationHistory: {},
        session: { on() {}, setPermissionCheckHandler() {}, setPermissionRequestHandler() {}, webRequest: {} },
        setWindowOpenHandler() {}, isDestroyed() { return this.dead; }, close() { this.dead = true; },
        getURL() { return this.url; }, getTitle: () => "", isLoading: () => false, async loadURL(url) { this.url = url; },
      });
    }
    getBounds() { return this.bounds; }
    setBounds(value) { this.bounds = value; }
    setVisible(value) { this.visible = value; }
  }
  const browser = load("embedded-browser-manager.js", { electron: { WebContentsView: View, app: {}, dialog: {} } });
  browser.init({ getWindowByInstanceId: (instance) => windows.get(instance), lookupHostAddresses: () => new Promise((resolve) => { resolveDns = resolve; }) });
  const payload = { id: "same-tab", conversation_id: "same-conversation", url: "about:blank" };
  await browser.create({ ...payload, window_instance_id: "instance-a" });
  await browser.create({ ...payload, window_instance_id: "instance-b" });
  browser.activate("same-tab", "same-conversation", "instance-a");
  browser.setBounds({ ...payload, window_instance_id: "instance-a", x: 0, y: 0, width: 500, height: 600 });
  browser.activate("same-tab", "same-conversation", "instance-b");
  browser.setBounds({ ...payload, window_instance_id: "instance-b", x: 0, y: 0, width: 400, height: 600 });
  assert.equal(windows.get("instance-a").children[0].visible, true);
  assert.equal(windows.get("instance-b").children[0].visible, true);
  const oldContents = windows.get("instance-a").children[0].webContents;
  const late = browser.create({ ...payload, id: "late-tab", window_instance_id: "instance-a", url: "https://example.com" });
  browser.releaseWindow("instance-a");
  windows.delete("instance-a");
  resolveDns([{ address: "93.184.216.34", family: 4 }]);
  await assert.rejects(late, /cancelled/);
  const remaining = browser.listTargets("same-conversation", "instance-b");
  assert.equal(remaining.length, 1);
  const events = windows.get("instance-b").events.length;
  oldContents.emit("did-finish-load");
  assert.equal(windows.get("instance-b").events.length, events);
  await assert.rejects(browser.create({ ...payload, window_instance_id: "unknown-instance" }), /window owner/);
  browser.releaseWindow("instance-b");
});

test("terminal output, operations and close cleanup stay with the originating window", async () => {
  const windows = resourceWindows(), processes = [];
  const terminal = load("pty-manager.js");
  terminal.init({ getWindowByInstanceId: (instance) => windows.get(instance), assertTrustedPath: (value) => value,
    pty: { spawn() {
      const callbacks = { writes: [] }; processes.push(callbacks);
      return { pid: 0, write: (value) => callbacks.writes.push(value), resize() {}, kill() { callbacks.exit({ exitCode: 0 }); },
        onData: (callback) => { callbacks.data = callback; }, onExit: (callback) => { callbacks.exit = callback; } };
    } },
  });
  const a = terminal.spawnSession(__dirname, "same-conversation", "instance-a");
  const b = terminal.spawnSession(__dirname, "same-conversation", "instance-b");
  processes[0].data("first-window-output");
  assert.equal(windows.get("instance-a").events.length, 1);
  assert.equal(windows.get("instance-b").events.length, 0);
  assert.equal(terminal.writeToSession(a.session_id, "cross-window", "same-conversation", "instance-b"), false);
  assert.equal(terminal.snapshotSession(a.session_id, 100, "same-conversation", "instance-b"), null);
  assert.equal(terminal.listSessions("same-conversation", 100, "instance-b")[0].session_id, b.session_id);
  const firstWindow = windows.get("instance-a");
  firstWindow.dead = true;
  windows.delete("instance-a");
  await terminal.releaseWindow("instance-a");
  processes[0].data("late-first-window-output");
  assert.equal(firstWindow.events.length, 1);
  assert.equal(terminal.listActiveSessions()[0].session_id, b.session_id);
  await terminal.releaseWindow("instance-b");
  assert.equal(terminal.listActiveSessions().length, 0);
});

function clearActivity(revision, rendererInstanceId) {
  return { revision, rendererInstanceId, runtimeReady: true, activeTurns: [], sideChatStreams: [], pendingPrompts: [],
    uploadingAttachments: [], dirtyEditors: [], backgroundTasks: [], pendingDrafts: [], draftStates: [] };
}

test("updater aggregates every window, binds ACK ownership and fingerprints draft revisions and window topology", async (t) => {
  const profile = temporaryProfile(t), listeners = new Map(), windows = resourceWindows();
  const updater = load("updater.js", { "electron-updater": { autoUpdater: { on: (event, callback) => listeners.set(event, callback), setFeedURL() {} } } }, {
    process: { env: { MINICODE_UPDATE_FEED_URL: "https://updates.example.com/minicode" }, resourcesPath: profile },
    setTimeout: (callback, delay) => delay === 15000 ? 0 : setTimeout(callback, delay),
  });
  const reports = new Map([ ["instance-a", clearActivity(1, "renderer-a")], ["instance-b", clearActivity(1, "renderer-b")] ]);
  const requests = new Map();
  let automaticAck = false;
  for (const [instance, window] of windows) {
    window.webContents.send = (channel, payload) => {
      window.events.push({ channel, payload });
      if (channel === "minicode:update:activity:request") {
        requests.set(instance, payload.requestId);
        if (automaticAck) queueMicrotask(() => updater.updateActivity(instance, { ...reports.get(instance), revision: ++reports.get(instance).revision, requestIds: [payload.requestId] }));
      }
    };
  }
  await updater.init({ app: { isPackaged: true, getVersion: () => "1.0.0", getPath: () => profile },
    getMainWindows: () => Array.from(windows.values()), getWindowRecord: (window) => ({ windowInstanceId: Array.from(windows).find(([, value]) => value === window)[0] }) });
  for (const instance of windows.keys()) updater.registerWindow(instance);
  listeners.get("update-downloaded")({ version: "2.0.0" });
  const requested = updater.preflight();
  let finished = false;
  void requested.then(() => { finished = true; });
  updater.updateActivity("instance-b", { ...clearActivity(1, "renderer-b"), requestIds: [requests.get("instance-a"), requests.get("instance-b")] });
  await new Promise(setImmediate);
  assert.equal(finished, false, "another window cannot acknowledge the primary request");
  updater.updateActivity("instance-a", { ...clearActivity(1, "renderer-a"), requestIds: [requests.get("instance-a")] });
  assert.equal((await requested).allowed, true);
  automaticAck = true;
  reports.get("instance-b").pendingDrafts = ["recovery-write:secondary-1"];
  const blocked = await updater.preflight();
  assert.equal(blocked.allowed, false);
  assert.equal(blocked.checks.find((check) => check.code === "draft.pending").details.count, 1);
  reports.get("instance-b").pendingDrafts = [];
  reports.get("instance-b").draftStates = ["secondary-1:committed:7:conversation-a"];
  const committed = await updater.preflight();
  assert.equal(committed.allowed, true);
  reports.get("instance-b").draftStates = ["secondary-1:committed:8:conversation-a"];
  const changed = await updater.preflight();
  assert.notEqual(changed.fingerprint, committed.fingerprint);
  let installed = false;
  const install = await updater.executeInstallTransaction({ fingerprint: changed.fingerprint, readPreflight: () => updater.preflight(),
    createBackup: async () => { reports.get("instance-b").dirtyEditors = ["hidden-workspace/file.ts"]; return {}; },
    commitInstall: () => { installed = true; } });
  assert.equal(install.reason, "preflight_changed");
  assert.equal(installed, false);
  reports.get("instance-b").dirtyEditors = [];
  const beforeClose = await updater.preflight();
  updater.unregisterWindow("instance-b"); windows.delete("instance-b");
  const afterClose = await updater.preflight();
  assert.equal(afterClose.allowed, true);
  assert.notEqual(afterClose.fingerprint, beforeClose.fingerprint);
  updater.invalidateActivity("instance-a", "activity.renderer_navigation");
  assert.equal(updater.updateActivity("instance-a", { ...clearActivity(100, "renderer-a"), requestIds: [] }).accepted, false);
  reports.set("instance-a", clearActivity(1, "renderer-a-reloaded"));
  assert.equal((await updater.preflight()).allowed, true);
  updater.unregisterWindow("instance-a");
});

test("native editor close uses the captured runtime and instance and reports a failed stop", async () => {
  let complete, request;
  const runtime = { apiBaseUrl: "http://127.0.0.1:8001", runtimeToken: "old-runtime-token", windowOwnerId: "secondary-1", windowInstanceId: "old-instance", desktopLifecycleToken: "main-backend-only-token" };
  const close = releaseEditorWindow(runtime, (url, options) => { request = { url, options }; return new Promise((resolve) => { complete = resolve; }); });
  runtime.apiBaseUrl = "http://127.0.0.1:8002"; runtime.runtimeToken = "new-runtime-token"; runtime.windowInstanceId = "new-instance";
  assert.equal(request.url, "http://127.0.0.1:8001/api/workspace/language/lifecycle");
  assert.equal(request.options.headers["x-minicode-token"], "old-runtime-token");
  assert.equal(request.options.headers["x-minicode-desktop-lifecycle-token"], "main-backend-only-token");
  assert.equal(JSON.parse(request.options.body).window_instance_id, "old-instance");
  complete({ ok: false, status: 503 });
  await assert.rejects(close, /HTTP 503/);
});

test("backend runtime changes publish to each registered window and capture its latest close endpoint", async (t) => {
  const { manager } = windowFixture(t);
  const primary = await manager.createMainWindow(), secondary = await manager.createNewWindow();
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8")
    .match(/function publishBackendRuntimeChange\(previousRuntime, nextRuntime\) \{[\s\S]*?\n\}/)[0];
  const next = { revision: 2, apiBaseUrl: "http://127.0.0.1:8002", wsBaseUrl: "ws://127.0.0.1:8002", runtimeToken: "current-token" };
  const publish = vm.runInNewContext(`${source}\npublishBackendRuntimeChange;`, {
    windowManager: manager,
    getWindowRuntimeConfig: (win) => ({ ...next, windowOwnerId: manager.getWindowRecord(win).windowOwnerId,
      windowInstanceId: manager.getWindowRecord(win).windowInstanceId }),
  });
  publish({ apiBaseUrl: "http://127.0.0.1:8001", wsBaseUrl: "ws://127.0.0.1:8001" }, next);
  for (const win of [primary, secondary]) {
    const event = win.sent.find((message) => message.channel === "minicode:runtime:changed");
    assert.equal(event.payload.apiBaseUrl, next.apiBaseUrl);
    assert.equal(event.payload.windowInstanceId, manager.getWindowRecord(win).windowInstanceId);
    assert.equal(manager.getWindowRecord(win).runtime.runtimeToken, next.runtimeToken);
  }
});

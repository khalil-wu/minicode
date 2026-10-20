"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const preferences = require("./preferences");

function service({ focused = true, otherFocused = false, settings = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-notifications-"));
  preferences.init(path.join(root, "preferences.json"), settings);
  const shown = [];
  const sounds = [];
  const navigation = [];
  const owner = { isFocused: () => focused, isDestroyed: () => false };
  class Notification extends EventEmitter {
    static isSupported() { return true; }
    constructor(payload) { super(); this.payload = payload; }
    show() { shown.push(this); }
    close() { this.emit("close"); }
  }
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const start = source.indexOf("const desktopNotifications = new Map();");
  const end = source.indexOf("\n// ---------------------------------------------------------------------------", start);
  const body = source.slice(start, end);
  const context = vm.createContext({ Notification, desktopPreferences: preferences, DESKTOP_ICON_PATH: "fixture-icon",
    notificationSounds: { playSound: (sound) => { sounds.push(sound); return Promise.resolve(true); } },
    app: { getPath: () => root }, appendDesktopLog: () => {},
    windowManager: { getMainWindows: () => [owner, { isFocused: () => otherFocused }], getFocusedMainWindow: () => owner, focusMainWindow: () => {} },
    normalizeDeepLinkTarget: target => target, dispatchDeepLink: target => navigation.push(target) });
  vm.runInContext(body + ";globalThis.api={showDesktopNotification,hideDesktopNotifications};", context);
  return { api: context.api, owner, shown, sounds, navigation, close: () => {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true });
  } };
}

const request = (kind = "permission", conversationId = "conversation-A", requestId = "request-1", turnId = "turn-1") => ({
  kind, title: "Actual request", body: "Original evidence", target: { kind: "conversation", conversationId }, requestId, turnId,
});

test("real completion identity deduplicates another renderer without consuming the next turn", () => {
  const fixture = service({ focused: false });
  try {
    const completed = { kind: "completion", title: "Actual conversation", body: "Actual final text", messageId: "real-message", target: { kind: "conversation", conversationId: "conversation-A" } };
    assert.equal(fixture.api.showDesktopNotification(completed, fixture.owner), true);
    assert.equal(fixture.api.showDesktopNotification(completed, { isFocused: () => false }), false);
    assert.equal(fixture.api.showDesktopNotification({ ...completed, messageId: "real-next-message" }, fixture.owner), true);
    assert.equal(fixture.shown[0].payload.body, "Actual final text");
  } finally { fixture.close(); }
});

for (const kind of ["permission", "question"]) {
  test(`${kind} stays in the visible focused conversation instead of posting an OS notification`, () => {
    const fixture = service();
    try {
      assert.equal(fixture.api.showDesktopNotification({ ...request(kind), conversationVisible: true }, fixture.owner), false);
      assert.equal(fixture.shown.length, 0);
      assert.equal(fixture.api.showDesktopNotification({ ...request(kind), conversationVisible: false }, fixture.owner), true);
      assert.equal(fixture.shown[0].payload.body, "Original evidence");
    } finally { fixture.close(); }
  });
}

test("an unfocused owner still receives a request while another app window has focus", () => {
  const fixture = service({ focused: false, otherFocused: true });
  try {
    assert.equal(fixture.api.showDesktopNotification({ ...request(), conversationVisible: true }, fixture.owner), true);
    fixture.shown[0].emit("click");
    assert.equal(fixture.navigation[0].conversationId, "conversation-A");
  } finally { fixture.close(); }
});

test("persisted request toggles drive the native producer and completion policy remains independent", () => {
  const fixture = service({ settings: { permissionNotifications: false, questionNotifications: false, turnCompletionNotifications: "never" } });
  try {
    for (const kind of ["permission", "question", "completion"]) assert.equal(fixture.api.showDesktopNotification(request(kind), fixture.owner), false);
    preferences.setPreferences({ questionNotifications: true });
    assert.equal(fixture.api.showDesktopNotification(request("question"), fixture.owner), true);
    assert.equal(fixture.api.showDesktopNotification(request("permission"), fixture.owner), false);
  } finally { fixture.close(); }
});

test("real request and input owner receipts prevent duplicates and a late response cannot hide a new turn", () => {
  const fixture = service({ focused: false });
  try {
    assert.equal(fixture.api.showDesktopNotification(request(), fixture.owner), true);
    assert.equal(fixture.api.showDesktopNotification(request(), fixture.owner), false);
    assert.equal(fixture.api.showDesktopNotification(request("question", "conversation-A", "request-1", "turn-2"), fixture.owner), true);
    assert.equal(fixture.api.showDesktopNotification(request("question", "conversation-B"), fixture.owner), true);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A", requestId: "request-1", turnId: "turn-1" }, fixture.owner), 1);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A", requestId: "request-1", turnId: "turn-1" }, fixture.owner), 0);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A", requestId: "request-1", turnId: "turn-2" }, fixture.owner), 1);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-B", requestId: "request-1" }, fixture.owner), 1);
  } finally { fixture.close(); }
});

test("returning to a conversation only hides that conversation and requires actual native focus", () => {
  const fixture = service({ focused: false });
  try {
    fixture.api.showDesktopNotification(request(), fixture.owner);
    fixture.api.showDesktopNotification(request("question", "conversation-B"), fixture.owner);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A", onlyIfFocused: true }, fixture.owner), 0);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A", onlyIfFocused: true }, { isFocused: () => true }), 1);
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-B" }, fixture.owner), 1);
  } finally { fixture.close(); }
});

test("native dismissal releases the actual notification receipt", () => {
  const fixture = service({ focused: false });
  try {
    fixture.api.showDesktopNotification(request(), fixture.owner);
    fixture.shown[0].close();
    assert.equal(fixture.api.hideDesktopNotifications({ conversationId: "conversation-A" }, fixture.owner), 0);
    assert.equal(fixture.api.showDesktopNotification(request(), fixture.owner), true);
  } finally { fixture.close(); }
});

test("the persisted sound choice reaches the producer without an additional system sound", () => {
  const fixture = service({ focused: false, settings: { notificationSound: "classic" } });
  try {
    fixture.api.showDesktopNotification(request(), fixture.owner);
    assert.equal(fixture.shown[0].payload.silent, true);
    assert.deepEqual(fixture.sounds, ["classic"]);
    preferences.setPreferences({ notificationSound: "none" });
    fixture.api.showDesktopNotification(request("permission", "conversation-A", "request-2"), fixture.owner);
    assert.deepEqual(fixture.sounds, ["classic", "none"]);
  } finally { fixture.close(); }
});

test("notification hide IPC uses its actual main-frame owner and the declared capability", () => {
  const handlers = new Map();
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console, require(name) {
      if (name === "electron") return { ipcMain: { on() {}, handle(channel, handler) { handlers.set(channel, handler); } } };
      return require(name);
    },
  });
  const mainFrame = { url: "file:///minicode/index.html" };
  const webContents = { mainFrame, getURL: () => mainFrame.url };
  const window = { webContents, isDestroyed: () => false, isMaximized: () => false, isFocused: () => true };
  const calls = [];
  moduleBox.exports.init({ getWindowForSender: sender => sender === webContents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "fixture-window" }), hideDesktopNotifications: (selector, owner) => { calls.push([selector, owner]); return 2; } });
  moduleBox.exports.registerIpcHandlers();
  const hide = handlers.get("minicode:notifications:hide");
  assert.throws(() => hide({ sender: webContents, senderFrame: { url: mainFrame.url } }, { conversationId: "conversation-A" }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  const selector = { conversationId: "conversation-A", requestId: "request-1", turnId: "turn-1" };
  assert.equal(hide({ sender: webContents, senderFrame: mainFrame }, selector), 2);
  assert.equal(calls[0][0], selector);
  assert.equal(calls[0][1], window);
  const state = handlers.get("minicode:window:state")({ sender: webContents, senderFrame: mainFrame });
  assert.equal(state.isFocused, true);
});

test("preload preserves the real request selector and native window focus event", async () => {
  let runtime;
  const calls = [], listeners = new Map();
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({ ipcRenderer: { sendSync: () => ({}), on: (channel, listener) => listeners.set(channel, listener), removeListener: (channel) => listeners.delete(channel),
      invoke: (...args) => { calls.push(args); return Promise.resolve(1); } }, contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  const selector = { conversationId: "conversation-A", requestId: "request-1", onlyIfFocused: true };
  await runtime.desktop.hideNotifications(selector);
  assert.deepEqual(calls, [["minicode:notifications:hide", selector]]);
  let observed;
  const unsubscribe = runtime.desktop.windowControls.onStateChanged(state => { observed = state; });
  const state = { isMaximized: false, isFocused: true };
  listeners.get("minicode:window:state")({}, state);
  assert.equal(observed, state);
  unsubscribe();
  assert.equal(listeners.has("minicode:window:state"), false);
});

test("existing native focus and blur publish their actual owner state", () => {
  const source = fs.readFileSync(path.join(__dirname, "window-manager.js"), "utf8");
  const start = source.indexOf("const publishWindowState = () =>");
  const end = source.indexOf("mainWindow.webContents.on(\"will-navigate\"", start);
  const owner = new EventEmitter();
  let focused = false;
  const observed = [];
  owner.isMaximized = () => false;
  owner.isFocused = () => focused;
  owner.webContents = { send: (channel, state) => observed.push([channel, state]) };
  vm.runInNewContext(source.slice(start, end), { mainWindow: owner });
  focused = true; owner.emit("focus");
  focused = false; owner.emit("blur");
  assert.deepEqual(observed.map(([channel, state]) => [channel, state.isFocused]), [["minicode:window:state", true], ["minicode:window:state", false]]);
});

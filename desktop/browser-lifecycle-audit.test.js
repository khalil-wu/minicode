const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const bridge = require("./embedded-browser-bridge");
bridge.init({ getWindowRecordByInstanceId: (instance) => instance === "fixture-window" ? {
  windowInstanceId: instance, windowOwnerId: "primary", windowBindingToken: "fixture-binding", window: { isDestroyed: () => false },
} : null });

function browserHarness(dialog = {}) {
  let id = 0;
  let finishDns;
  const views = [];
  const events = [];
  let responseStarted;
  const session = {
    setPermissionCheckHandler() {}, setPermissionRequestHandler() {}, on() {},
    clearedOrigins: [], clearCacheCalls: 0,
    async clearStorageData(options) { this.clearedOrigins.push(options?.origin ?? "all"); },
    async clearCache() { this.clearCacheCalls++; },
    async resolveProxy() { return "DIRECT"; },
    webRequest: { onResponseStarted(_filter, callback) { responseStarted = callback; } },
  };
  class View {
    constructor() {
      this.bounds = { x: 0, y: 0, width: 0, height: 0 };
      this.visible = false;
      this.webContents = Object.assign(new EventEmitter(), {
        id: ++id, session, url: "", dead: false, navigationHistory: {},
        pickerCalls: [], stopCalls: 0,
        captureCalls: 0,
        backgroundThrottling: true,
        getBackgroundThrottling() { return this.backgroundThrottling; },
        setBackgroundThrottling(value) { this.backgroundThrottling = value; },
        async capturePage() {
          this.captureCalls++;
          return { isEmpty: () => false, getSize: () => ({ width: 500, height: 600 }), toPNG: () => Buffer.from("actual-frame") };
        },
        stop() { this.stopCalls++; },
        focus() { this.pickerCalls.push("focus"); },
        async executeJavaScript(expression) { this.pickerCalls.push(expression); return null; },
        setWindowOpenHandler() {}, getURL() { return this.url; }, getTitle: () => "", isLoading: () => false,
        isDestroyed() { return this.dead; }, close() { this.dead = true; }, async loadURL(url) { this.url = url; },
      });
      views.push(this);
    }
    getBounds() { return this.bounds; }
    setBounds(value) { this.bounds = value; }
    setVisible(value) { this.visible = value; }
  }
  const ownerWindow = {
    isDestroyed: () => false, webContents: { send(_channel, event) { events.push(event); }, getZoomFactor: () => 1 },
    getContentBounds: () => ({ width: 1000, height: 800 }),
    contentView: { addChildView() {}, removeChildView() {} },
  };
  const moduleValue = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "embedded-browser-manager.js"), "utf8"), {
    module: moduleValue, exports: moduleValue.exports, URL, Buffer, setTimeout, clearTimeout,
    require(name) { return name === "electron" ? { WebContentsView: View, app: {}, dialog, session: { fromPartition: () => session } } : require(name); },
  });
  const manager = moduleValue.exports;
  manager.init({
    getWindowByInstanceId: () => ownerWindow,
    browserSettingsPath: path.join(__dirname, ".nonexistent-audit-settings.json"),
    lookupHostAddresses: () => new Promise((resolve) => { finishDns = resolve; }),
  });
  return { manager, views, events, session,
    responseStarted: (details) => responseStarted(details),
    resolveDns: () => finishDns([{ address: "93.184.216.34", family: 4 }]),
  };
}

function browserSettingsRenderer() {
  const ipcRenderer = new EventEmitter();
  ipcRenderer.sendSync = () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" });
  const window = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    process: { env: {}, argv: [], contextIsolated: false }, window,
    require: () => ({ ipcRenderer, contextBridge: {} }),
  });
  return { browser: window.__MINICODE_RUNTIME__.desktop.embeddedBrowser,
    hostWindow: { webContents: { send: (channel, payload) => ipcRenderer.emit(channel, {}, structuredClone(payload)) } } };
}

test("committed browser settings cross the actual preload subscription in both windows and clear per-origin permissions", async () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-browser-settings-sync-"));
  const first = browserSettingsRenderer();
  const second = browserSettingsRenderer();
  const firstChanges = []; const secondChanges = [];
  const stop = first.browser.onSettingsChanged(value => firstChanges.push(value));
  second.browser.onSettingsChanged(value => secondChanges.push(value));
  const { manager, session } = browserHarness();
  manager.init({ browserSettingsPath: path.join(root, "settings.json"),
    getMainWindows: () => [first.hostWindow, second.hostWindow],
    lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }],
    browserHistory: { clear() {} } });
  manager.setBrowserSettings({ downloadPolicy: "allow" });
  manager.setBrowserSettings({ origin: "https://a.example/one", permission: "notifications", allowed: true });
  manager.setBrowserSettings({ origin: "https://b.example/two", permission: "geolocation", allowed: true });
  assert.deepEqual(firstChanges, secondChanges);
  assert.deepEqual(firstChanges.at(-1), { downloadPolicy: "allow", sitePermissions: { "https://a.example": ["notifications"], "https://b.example": ["geolocation"] } });
  assert.deepEqual(firstChanges.at(-1), JSON.parse(fs.readFileSync(path.join(root, "settings.json"), "utf8")));
  await manager.create({ id: "clear-a", url: "https://a.example/one", conversation_id: "settings-owner", window_instance_id: "fixture-window" });
  await manager.clearSiteData({ id: "clear-a", conversation_id: "settings-owner", window_instance_id: "fixture-window" });
  assert.equal(session.clearedOrigins[0], "https://a.example");
  assert.deepEqual(secondChanges.at(-1), { downloadPolicy: "allow", sitePermissions: { "https://b.example": ["geolocation"] } });
  stop();
  const firstCount = firstChanges.length;
  await manager.clearBrowsingData();
  assert.equal(firstChanges.length, firstCount);
  assert.equal(session.clearedOrigins.at(-1), "all");
  assert.equal(session.clearCacheCalls, 1);
  assert.deepEqual(secondChanges.at(-1), { downloadPolicy: "allow", sitePermissions: {} });
});

test("a failed browser settings commit cannot emit a new selection to another window", (t) => {
  const os = require("node:os");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-browser-settings-publish-"));
  const renderer = browserSettingsRenderer();
  const changes = [];
  renderer.browser.onSettingsChanged(value => changes.push(value));
  const { manager } = browserHarness();
  manager.init({ browserSettingsPath: path.join(root, "settings.json"), getMainWindows: () => [renderer.hostWindow] });
  manager.setBrowserSettings({ downloadPolicy: "ask" });
  t.mock.method(fs, "renameSync", () => { throw new Error("settings commit failed"); });
  assert.throws(() => manager.setBrowserSettings({ downloadPolicy: "allow" }), /settings commit failed/);
  assert.equal(changes.length, 1);
  assert.equal(changes[0].downloadPolicy, "ask");
  assert.equal(manager.getBrowserSettings("").downloadPolicy, "ask");
});

test("authenticated backend owned-preview navigation skips host approval while manual payload claims do not", async () => {
  let prompts = 0;
  const { manager, views, events, responseStarted } = browserHarness({
    async showMessageBox() { prompts++; return { response: 0 }; },
  });
  const owner = "preview-owner";
  const url = "http://127.0.0.1:5173/preview-token/index.html";
  const authorization = {
    kind: "owned_preview", url, preview_url: url, preview_id: "preview-runtime-id",
    session_id: "session", conversation_id: owner, permission_mode: "bypass", operation_id: "owned-navigation",
  };
  bridge.init({ token: "backend-only-token", manager });
  const endpoint = await bridge.start();
  try {
    const payload = { action: "navigate", url, conversation_id: owner, window_instance_id: "fixture-window", window_binding_token: "fixture-binding",
      operation_id: "owned-navigation", navigation_authorization: authorization };
    const rendererRequest = await fetch(`${endpoint}/v1/command`, {
      method: "POST", headers: { authorization: "Bearer renderer-api-token" }, body: JSON.stringify(payload),
    });
    assert.equal(rendererRequest.status, 401);
    assert.equal(views.length, 0);
    const response = await fetch(`${endpoint}/v1/command`, {
      method: "POST", headers: { authorization: "Bearer backend-only-token" }, body: JSON.stringify(payload),
    });
    const result = await response.json();
    assert.equal(result.ok, true);
    assert.equal(result.target.url, url);
    assert.equal(result.cleanup_receipt.completed, true);
    assert.equal(result.cleanup_receipt.conversation_id, owner);
    assert.equal(prompts, 0);
    assert.equal(views.length, 1);
    const contents = views[0].webContents;
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    contents.emit("will-redirect", event, "http://127.0.0.1:5173/preview-token/next.html", false, true);
    assert.equal(event.prevented, false);
    responseStarted({ webContentsId: contents.id, resourceType: "mainFrame", url, ip: "127.0.0.1" });
    await new Promise(setImmediate);
    assert.equal(contents.stopCalls, 0);
    const other = "http://127.0.0.1:8000/admin";
    contents.emit("will-redirect", event, other, false, true);
    await new Promise(setImmediate);
    assert.equal(event.prevented, true);
    assert.equal(prompts, 1);
    assert.equal(contents.getURL(), url);
    responseStarted({ webContentsId: contents.id, resourceType: "mainFrame", url: other, ip: "127.0.0.1" });
    await new Promise(setImmediate);
    assert.equal(contents.stopCalls, 1);
    assert.match(events.at(-1).error, /private peer/);
    await assert.rejects(manager.create({ id: "forged-manual", conversation_id: "manual-owner", url,
      navigation_authorization: { ...authorization, conversation_id: "manual-owner" }, window_instance_id: "fixture-window" }), /cancelled/);
    assert.equal(prompts, 2);
    await assert.rejects(manager.create({ id: "other-private", conversation_id: owner, url: "http://192.168.1.20/admin", window_instance_id: "fixture-window" }), /cancelled/);
    assert.equal(prompts, 3);
    assert.equal(views.length, 1);
  } finally { await bridge.stop(); manager.disposeAll(); }
});

for (const interrupt of ["timeout", "cancelled"]) test(`late host confirmation cannot resume a ${interrupt} backend navigation`, async () => {
  let confirm;
  let dialogStarted;
  const started = new Promise((resolve) => { dialogStarted = resolve; });
  const dialog = new Promise((resolve) => { confirm = resolve; });
  const { manager, views } = browserHarness({ showMessageBox() { dialogStarted(); return dialog; } });
  bridge.init({ token: "backend-only-token", manager });
  const endpoint = await bridge.start();
  const owner = { operation_id: `late-${interrupt}`, conversation_id: "owner" };
  const post = (payload, suffix = "command") => fetch(`${endpoint}/v1/${suffix}`, {
    method: "POST", headers: { authorization: "Bearer backend-only-token" }, body: JSON.stringify({ window_instance_id: "fixture-window", window_binding_token: "fixture-binding", ...payload }),
  }).then((response) => response.json());
  try {
    const navigation = post({ ...owner, action: "navigate", url: "http://127.0.0.1:5173/",
      operation_timeout_ms: interrupt === "timeout" ? 20 : 30000 });
    await started;
    if (interrupt === "cancelled") await post({ ...owner, action: "cancel" }, "operation");
    const interrupted = await navigation;
    assert.equal(interrupted.status, interrupt);
    assert.equal(interrupted.cleanup_receipt.pending, 1);
    assert.equal(interrupted.cleanup_receipt.submitted, false);
    confirm({ response: 1 });
    const settled = await post({ ...owner, action: "wait" }, "operation");
    assert.equal(settled.cleanup_receipt.completed, true);
    assert.equal(settled.cleanup_receipt.execution_outcome, interrupt);
    assert.equal(views.length, 0);
    assert.equal(manager.listTargets("owner", "fixture-window").length, 0);
    assert.equal((await post({ ...owner, action: "navigate", url: "https://example.com" })).ok, false);
  } finally { confirm({ response: 0 }); await bridge.stop(); manager.disposeAll(); }
});

for (const terminal of ["cancelled", "load-failed"]) test(`late redirect confirmation cannot revive a ${terminal} navigation`, async () => {
  let confirm;
  let dialogStarted;
  let finishLoad;
  const started = new Promise((resolve) => { dialogStarted = resolve; });
  const dialog = new Promise((resolve) => { confirm = resolve; });
  const { manager, views, events } = browserHarness({ showMessageBox() { dialogStarted(); return dialog; } });
  await manager.create({ id: "redirect", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const contents = views[0].webContents;
  const loads = [];
  contents.loadURL = (url) => {
    loads.push(url);
    contents.emit("will-redirect", { preventDefault() {} }, "http://127.0.0.1:8000/admin", false, true);
    return new Promise((_resolve, reject) => { finishLoad = reject; });
  };
  const controller = new AbortController();
  const navigation = manager.executeControlCommand({ action: "navigate", target_id: "redirect",
    conversation_id: "owner", url: "https://93.184.216.34/", window_instance_id: "fixture-window" }, { signal: controller.signal });
  const failed = assert.rejects(navigation, /ERR_ABORTED/);
  try {
    await started;
    if (terminal === "cancelled") controller.abort(new Error("Browser operation cancelled."));
    finishLoad(new Error("ERR_ABORTED"));
    await failed;
    confirm({ response: 1 });
    await new Promise(setImmediate);
    assert.deepEqual(loads, ["https://93.184.216.34/"]);
    assert.match(events.at(-1).error, /cancelled/);
  } finally { confirm({ response: 0 }); manager.disposeAll(); }
});

test("agent page has an initial viewport but waits for the presented panel's actual frame", async () => {
  const { manager, views, events } = browserHarness();
  await manager.executeControlCommand({ action: "navigate", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" }, { requestPresentation: true });
  const target = manager.listTargets("owner", "fixture-window")[0];
  const view = views[0];
  assert.deepEqual({ ...view.getBounds() }, { x: 0, y: 0, width: 1000, height: 800 });
  assert.equal(view.visible, false);
  assert.equal(events.at(-1).type, "presentation-requested");
  manager.activate(target.id, "owner", "fixture-window");
  assert.equal(view.visible, false, "activation cannot expose the initial full-window viewport over chat");
  const capture = manager.executeControlCommand({ action: "screenshot", target_id: target.id, conversation_id: "owner", window_instance_id: "fixture-window" });
  assert.equal(view.webContents.captureCalls, 0);
  manager.setBounds({ id: target.id, conversation_id: "owner", x: 200, y: 80, width: 500, height: 600, window_instance_id: "fixture-window" });
  assert.equal(view.visible, true);
  const result = await capture;
  assert.equal(result.width, 500);
  assert.equal(result.height, 600);
  assert.equal(Buffer.from(result.data, "base64").toString(), "actual-frame");
  assert.equal(view.webContents.captureCalls, 1);
  manager.disposeAll();
});

for (const terminal of ["cancelled", "closed"]) test(`screenshot presentation wait is released when ${terminal}`, async () => {
  const { manager, views } = browserHarness();
  await manager.create({ id: "capture", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const controller = new AbortController();
  const capture = manager.executeControlCommand({ action: "screenshot", target_id: "capture", conversation_id: "owner", window_instance_id: "fixture-window" }, { signal: controller.signal });
  const failed = assert.rejects(capture, /cancelled|closed/);
  if (terminal === "cancelled") controller.abort(new Error("capture cancelled"));
  else manager.close({ id: "capture", conversation_id: "owner", window_instance_id: "fixture-window" });
  await failed;
  assert.equal(views[0].webContents.captureCalls, 0);
  manager.disposeAll();
});

test("background navigation preserves the selected owner's visible browser", async () => {
  const { manager, views } = browserHarness();
  await manager.create({ id: "a", conversation_id: "A", url: "about:blank", window_instance_id: "fixture-window" });
  await manager.create({ id: "b", conversation_id: "B", url: "about:blank", window_instance_id: "fixture-window" });
  manager.activate("b", "B", "fixture-window");
  manager.setBounds({ id: "b", conversation_id: "B", x: 0, y: 0, width: 600, height: 400, window_instance_id: "fixture-window" });
  await manager.executeControlCommand({ action: "navigate", target_id: "a", conversation_id: "A", url: "about:blank", window_instance_id: "fixture-window" });
  assert.equal(views[0].visible, false);
  assert.equal(views[1].visible, true);
  await manager.executeControlCommand({ action: "navigate", target_id: "b", conversation_id: "B", url: "about:blank", window_instance_id: "fixture-window" });
  assert.equal(views[1].visible, true);
});

for (const scope of ["tab", "conversation", "all"]) test(`closing ${scope} cancels an unpublished navigation`, async () => {
  const { manager, views, resolveDns } = browserHarness();
  const pending = manager.create({ id: "pending", conversation_id: "A", url: "https://pending.example/", window_instance_id: "fixture-window" });
  const cancelled = assert.rejects(pending, /cancelled/);
  if (scope === "tab") manager.close({ id: "pending", conversation_id: "A", window_instance_id: "fixture-window" });
  else if (scope === "conversation") manager.closeConversation("A", "fixture-window");
  else manager.disposeAll();
  resolveDns();
  await cancelled;
  assert.equal(views.length, 0);
  assert.equal(manager.listTargets("A", "fixture-window").length, 0);
});

for (const action of ["pick_element", "pick_region"]) test(`${action} gives Escape to the selected native page before starting selection`, async () => {
  const { manager, views } = browserHarness();
  await manager.create({ id: "design", conversation_id: "A", url: "about:blank", window_instance_id: "fixture-window" });
  const result = await manager.executeControlCommand({ action, target_id: "design", conversation_id: "A", window_instance_id: "fixture-window" });
  assert.equal(views[0].webContents.pickerCalls[0], "focus");
  assert.match(views[0].webContents.pickerCalls[1], /Escape/);
  assert.equal(result.ok, true);
  assert.equal(result.value, null);
});

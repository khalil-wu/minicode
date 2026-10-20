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

function browserHarness(dialog = {}, options = {}) {
  let id = 0;
  let finishDns;
  const views = [];
  const events = [];
  let responseStarted;
  let beforeRequest;
  let headersReceived;
  const session = {
    setPermissionCheckHandler(handler) { this.permissionCheck = handler; }, setPermissionRequestHandler(handler) { this.permissionRequest = handler; },
    setDevicePermissionHandler(handler) { this.devicePermission = handler; },
    listeners: new Map(), on(name, listener) { this.listeners.set(name, listener); },
    cookies: { async get() { return [{ name: "session" }, { name: "theme" }]; } },
    clearedOrigins: [], clearedStorage: [], clearCacheCalls: 0,
    async clearStorageData(options) { this.clearedOrigins.push(options?.origin ?? "all"); this.clearedStorage.push(options); },
    async clearCache() { this.clearCacheCalls++; },
    async resolveProxy() { return "DIRECT"; },
    webRequest: { onResponseStarted(_filter, callback) { responseStarted = callback; }, onBeforeRequest(_filter, callback) { beforeRequest = callback; }, onHeadersReceived(_filter, callback) { headersReceived = callback; } },
  };
  class View {
    constructor() {
      this.bounds = { x: 0, y: 0, width: 0, height: 0 };
      this.visible = false;
      this.webContents = Object.assign(new EventEmitter(), {
        id: ++id, session, url: "", dead: false, navigationHistory: {},
        pickerCalls: [], stopCalls: 0,
        captureCalls: 0, zoomFactor: 1, findRequests: [], findStopped: false,
        findInPage(text, options) { this.findRequests.push({ text, options }); return this.findRequests.length; },
        stopFindInPage(action) { this.findStopped = action; },
        getZoomFactor() { return this.zoomFactor; }, setZoomFactor(value) { this.zoomFactor = value; },
        setAudioMuted(value) { this.audioMuted = value; },
        print(_options, callback) { callback(true); },
        enableDeviceEmulation(value) { this.emulation = value; }, disableDeviceEmulation() { this.emulation = null; },
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
        isDestroyed() { return this.dead; }, close() { this.dead = true; }, async loadURL(url) { this.url = url; this.mainFrame.url = url; },
      });
      this.webContents.mainFrame = { url: "", detached: false, isDestroyed: () => false }; this.webContents.mainFrame.top = this.webContents.mainFrame;
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
    module: moduleValue, exports: moduleValue.exports, __dirname, URL, Buffer, setTimeout, clearTimeout,
    require(name) {
      if (name === "node:process") return { platform: options.platform ?? process.platform };
      return name === "electron" ? { WebContentsView: View, app: { getPath: () => require("node:os").tmpdir(), getLocale: () => "en-US" }, dialog,
        Menu: { getApplicationMenu: () => options.menu ?? null }, shell: { openPath: async value => value, showItemInFolder() {} }, session: { fromPartition: () => session } } : require(name);
    },
  });
  const manager = moduleValue.exports;
  manager.init({
    getWindowByInstanceId: () => ownerWindow,
    getMainWindows: () => [ownerWindow],
    browserSettingsPath: path.join(fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-browser-audit-")), "settings.json"),
    lookupHostAddresses: () => new Promise((resolve) => { finishDns = resolve; }),
  });
  return { manager, views, events, session, ownerWindow,
    beforeRequest: (details, callback) => beforeRequest(details, callback),
    headersReceived: (details, callback) => headersReceived(details, callback),
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

test("site management persists permission edits and reset changes the native permission callbacks", async () => {
  const { manager, views, session } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  await manager.create({ id: "permissions", conversation_id: "permission-owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  const contents = views[0].webContents;
  manager.setBrowserSettings({ origin: "https://example.com/path", permission: "notifications", allowed: true });
  manager.setBrowserSettings({ origin: "https://example.com", permission: "geolocation", allowed: true });
  assert.deepEqual(structuredClone(manager.listSitePermissions()), [{ origin: "https://example.com", permissions: ["geolocation", "notifications"] }]);
  assert.equal(session.permissionCheck(contents, "notifications", "https://example.com"), true);
  assert.equal(session.permissionCheck(contents, "notifications", "https://other.example.com"), false);
  let granted;
  session.permissionRequest(contents, "geolocation", value => { granted = value; }, { requestingUrl: "https://example.com/" });
  assert.equal(granted, true);
  manager.setBrowserSettings({ origin: "https://example.com", permission: "notifications", allowed: false });
  assert.equal(session.permissionCheck(contents, "notifications", "https://example.com"), false);
  manager.removeSitePermissions({ origin: "https://example.com" });
  assert.deepEqual(structuredClone(manager.listSitePermissions()), []);
  session.permissionRequest(contents, "geolocation", value => { granted = value; }, { requestingUrl: "https://example.com/" });
  assert.equal(granted, false);
  assert.throws(() => manager.removeSitePermissions({ origin: "file:///local" }), /valid HTTP or HTTPS origin/);
});

test("legacy media grants migrate to independent camera and microphone consumers without losing the user's grant", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  await h.manager.create({ id: "media", conversation_id: "owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  const contents = h.views[0].webContents;
  h.manager.setBrowserSettings({ origin: "https://example.com", permission: "media", allowed: true });
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "video" }), true);
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "audio" }), true);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "microphone", setting: "block" });
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "video" }), true);
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaTypes: ["audio", "video"] }), false);
  assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites[0].settings), { camera: "allow", microphone: "block" });
  assert.throws(() => h.manager.setSitePolicy({ category: "camera", setting: "allow" }), /individual website/);
  assert.throws(() => h.manager.setSitePolicy({ category: "ads", setting: "block" }), /Unsupported/);
});

test("content policies change the actual image request, audio and protected-content consumers", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  await h.manager.create({ id: "content", conversation_id: "owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  const contents = h.views[0].webContents;
  h.manager.setSitePolicy({ category: "images", setting: "block" });
  let result;
  h.beforeRequest({ webContentsId: contents.id, resourceType: "image", url: "https://images.example.net/picture.png" }, value => { result = value; });
  assert.equal(result.cancel, true);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "images", setting: "allow" });
  h.beforeRequest({ webContentsId: contents.id, resourceType: "image", url: "https://images.example.net/picture.png" }, value => { result = value; });
  assert.equal(result.cancel, false);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "sound", setting: "block" }); assert.equal(contents.audioMuted, true);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "sound", setting: "default" }); assert.equal(contents.audioMuted, false);
  h.manager.setSitePolicy({ category: "mediaKeySystem", setting: "block" });
  assert.equal(h.session.permissionCheck(contents, "mediaKeySystem", "https://example.com", {}), false);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "javascript", setting: "block" });
  h.headersReceived({ webContentsId: contents.id, resourceType: "mainFrame", url: "https://example.com/", responseHeaders: { "Content-Security-Policy": ["default-src https:"] } }, value => { result = value; });
  assert.deepEqual([...result.responseHeaders["Content-Security-Policy"]], ["default-src https:", "script-src 'none'"]);
});

test("one-time permissions remain document-scoped and a stale prompt cannot persist a grant after navigation", async () => {
  let respond;
  const h = browserHarness({ showMessageBox: async () => new Promise(resolve => { respond = resolve; }) });
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "permission-prompt", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents;
  const requested = new Promise(resolve => h.session.permissionRequest(contents, "geolocation", resolve, { requestingUrl: contents.url }));
  respond({ response: 1 }); assert.equal(await requested, true);
  assert.equal(h.session.permissionCheck(contents, "geolocation", "https://example.com", {}), true);
  assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
  contents.emit("did-navigate", {}, "https://example.com/next");
  assert.equal(h.session.permissionCheck(contents, "geolocation", "https://example.com", {}), false);
  const stale = new Promise(resolve => h.session.permissionRequest(contents, "geolocation", resolve, { requestingUrl: contents.url }));
  contents.emit("did-navigate", {}, "https://example.com/later"); respond({ response: 2 });
  assert.equal(await stale, false);
  assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
});

test("USB chooser grants only the selected real device and removal revokes future permission checks", async () => {
  const h = browserHarness({ showMessageBox: async () => ({ response: 0 }) });
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "usb", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents;
  const device = { deviceId: "native-device", vendorId: 123, productId: 456, serialNumber: "fixture-serial", productName: "Fixture device" };
  const selected = await new Promise(resolve => h.session.listeners.get("select-usb-device")({ preventDefault() {} }, { frame: contents.mainFrame, deviceList: [device] }, resolve));
  assert.equal(selected, "native-device");
  assert.equal(h.session.devicePermission({ deviceType: "usb", origin: "https://example.com", device }), true);
  assert.equal(h.session.devicePermission({ deviceType: "usb", origin: "https://other.example.com", device }), false);
  const metadata = h.manager.getSitePolicy().devices[0];
  assert.deepEqual(Object.keys(metadata).sort(), ["id", "name", "origin"]);
  h.manager.removeSiteDevice({ id: metadata.id });
  assert.equal(h.session.devicePermission({ deviceType: "usb", origin: "https://example.com", device }), false);
});

test("trusted guest password submissions require an owner Save or Update and never expose password values in the prompt", async () => {
  const prompts = []; const writes = []; let response = 0;
  const h = browserHarness({ showMessageBox: async (_owner, options) => { prompts.push(options); return { response }; } });
  const repository = { listPasswords: () => ({ available: true, items: writes.map((item, index) => ({ id: String(index), origin: item.origin, username: item.username })) }),
    getPassword: ({ id }) => writes[Number(id)], savePassword: item => { if (item.id) writes[Number(item.id)] = item; else writes.push(item); } };
  h.manager.init({ browserPersonalData: repository, lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "login", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/login" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents; const event = { sender: contents, senderFrame: contents.mainFrame };
  const submission = { origin: "https://example.com", documentUrl: "https://example.com/login", username: "fixture-user", password: "first-secret" };
  assert.equal(await h.manager.handlePasswordSubmission({ ...event, senderFrame: { url: contents.url } }, submission), false);
  assert.equal(await h.manager.handlePasswordSubmission(event, submission), false); assert.equal(writes.length, 0);
  response = 1;
  assert.equal(await h.manager.handlePasswordSubmission(event, submission), true);
  assert.equal(prompts.at(-1).message, "Save password?"); assert.equal(JSON.stringify(prompts).includes(submission.password), false);
  assert.equal(await h.manager.handlePasswordSubmission(event, submission), false); assert.equal(prompts.length, 2);
  assert.equal(await h.manager.handlePasswordSubmission(event, { ...submission, password: "updated-secret" }), true);
  assert.equal(prompts.at(-1).message, "Update password?"); assert.equal(writes[0].password, "updated-secret");
  assert.equal(await h.manager.handlePasswordSubmission(event, { ...submission, username: "", oldPassword: "updated-secret", password: "replacement-secret" }), true);
  assert.equal(writes[0].username, "fixture-user"); assert.equal(writes[0].password, "replacement-secret");
  assert.equal(JSON.stringify(prompts).includes("updated-secret"), false);
});

test("page menu commands preserve native target ownership, find results and keyboard requests", async () => {
  const { manager, views, events } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "page-actions", conversation_id: "page-owner", window_instance_id: "fixture-window" };
  await manager.create({ ...target, url: "https://example.com/" });
  const contents = views[0].webContents;
  assert.equal((await manager.runPageCommand({ ...target, action: "zoom", zoomFactor: 1.25 })).zoomFactor, 1.25);
  const found = await manager.runPageCommand({ ...target, action: "find", text: "example", forward: false, findNext: true });
  assert.equal(found.requestId, 1);
  assert.deepEqual(structuredClone(contents.findRequests[0]), { text: "example", options: { forward: false, findNext: true } });
  contents.emit("found-in-page", {}, { requestId: 1, matches: 3, activeMatchOrdinal: 2, finalUpdate: true });
  assert.equal(events.at(-1).type, "find-result");
  assert.equal(events.at(-1).conversationId, "page-owner");
  assert.equal(events.at(-1).findResult.matches, 3);
  await manager.runPageCommand({ ...target, action: "find", text: "" });
  assert.equal(contents.findStopped, "clearSelection");
  let prevented = 0;
  contents.emit("before-input-event", { preventDefault() { prevented++; } }, { key: "f", type: "keyDown", control: true });
  assert.equal(events.at(-1).type, "find-request");
  contents.emit("before-input-event", { preventDefault() { prevented++; } }, { key: "p", type: "keyDown", control: true });
  assert.equal(events.at(-1).type, "print-request");
  assert.equal(prevented, 2);
  assert.equal((await manager.runPageCommand({ ...target, action: "print" })).success, true);
  await assert.rejects(manager.runPageCommand({ ...target, conversation_id: "other-owner", action: "zoom" }), /another conversation/);
  const site = await manager.runPageCommand({ ...target, action: "site-info" });
  assert.equal(site.origin, "https://example.com"); assert.equal(site.secure, true); assert.equal(site.cookieCount, 2);
});

test("application-menu page commands require the presented owner and use Source zoom and reload semantics", async () => {
  const { manager, views, events, ownerWindow } = browserHarness();
  const changed = [];
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }], onMenuStateChanged: window => changed.push(window) });
  const target = { id: "menu-page", conversation_id: "menu-owner", window_instance_id: "fixture-window" };
  await manager.create({ ...target, url: "https://example.com/" });
  const contents = views[0].webContents;
  manager.activate(target.id, target.conversation_id, target.window_instance_id);
  assert.equal(manager.canReloadVisiblePage(ownerWindow, contents), false);
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "open-find"), false);
  manager.setBounds({ ...target, x: 50, y: 100, width: 800, height: 600 });
  assert.equal(manager.canReloadVisiblePage(ownerWindow, contents), true);
  assert.equal(manager.canReloadVisiblePage({}, contents), false);
  assert.equal(changed.length, 1);
  manager.setBounds({ ...target, x: 50, y: 100, width: 700, height: 600 });
  assert.equal(changed.length, 1);
  assert.equal(manager.runMenuCommand({}, contents, "open-find"), false);
  assert.equal(manager.runMenuCommand(ownerWindow, { id: 7000 }, "open-find"), false);
  manager.runMenuCommand(ownerWindow, contents, "open-find");
  assert.equal(events.at(-1).type, "find-request");
  manager.runMenuCommand(ownerWindow, contents, "find-next");
  assert.equal(events.at(-1).findDirection, "next");
  manager.runMenuCommand(ownerWindow, contents, "find-previous");
  assert.equal(events.at(-1).findDirection, "previous");
  assert.equal(events.at(-1).conversationId, target.conversation_id);
  contents.zoomFactor = 1.17;
  manager.runMenuCommand(ownerWindow, contents, "zoom-in");
  assert.equal(contents.zoomFactor, 1.25);
  assert.equal(events.at(-1).zoomFactor, 1.25);
  manager.runMenuCommand(ownerWindow, contents, "zoom-out");
  assert.equal(contents.zoomFactor, 1.1);
  manager.runMenuCommand(ownerWindow, contents, "zoom-out");
  assert.equal(contents.zoomFactor, 1);
  let reloads = 0, hardReloads = 0;
  contents.reload = () => { reloads++; };
  contents.reloadIgnoringCache = () => { hardReloads++; };
  manager.runMenuCommand(ownerWindow, { id: 7000 }, "reload");
  manager.runMenuCommand(ownerWindow, contents, "hard-reload");
  assert.equal(reloads, 1); assert.equal(hardReloads, 1);
  manager.setBounds({ ...target, x: 0, y: 0, width: 0, height: 0 });
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "reload"), false);
  manager.setBounds({ ...target, x: 50, y: 100, width: 700, height: 600 });
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "close"), true);
  assert.equal(events.at(-1).type, "closed");
  assert.equal(manager.listTargets(target.conversation_id, target.window_instance_id).length, 0);
  assert.equal(changed.length, 4);
});

test("queued layout after native close retires without accepting a different conversation owner", async () => {
  const { manager, views } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "closing-page", conversation_id: "closing-owner", window_instance_id: "fixture-window" };
  const bounds = { x: 50, y: 100, width: 700, height: 600 };
  await manager.create({ ...target, url: "https://example.com/" });
  manager.setBounds({ ...target, ...bounds });
  assert.throws(() => manager.setBounds({ ...target, ...bounds, conversation_id: "other-owner" }), /another conversation/);
  manager.close(target);
  assert.equal(views[0].webContents.isDestroyed(), true);
  assert.equal(manager.setBounds({ ...target, ...bounds }), false);
  assert.equal(manager.listTargets(target.conversation_id, target.window_instance_id).length, 0);
});

test("browser control preference survives reload and disables the agent while retaining manual browsing", async () => {
  const { manager } = browserHarness();
  const settingsFile = path.join(fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-browser-control-")), "settings.json");
  manager.init({ browserSettingsPath: settingsFile, lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const saved = manager.setBrowserSettings({ browserEnabled: false, deviceToolbarAlwaysOnNewTabs: true });
  assert.equal(saved.browserEnabled, false); assert.equal(saved.deviceToolbarAlwaysOnNewTabs, true);
  manager.init({ browserSettingsPath: `${settingsFile}.other` }); manager.init({ browserSettingsPath: settingsFile });
  assert.equal(manager.getBrowserSettings("").browserEnabled, false);
  assert.equal(manager.getBrowserSettings("").deviceToolbarAlwaysOnNewTabs, true);
  const target = { id: "manual", conversation_id: "manual-owner", window_instance_id: "fixture-window" };
  await manager.create({ ...target, url: "https://example.com/" });
  await assert.rejects(manager.executeControlCommand({ ...target, action: "discover" }), /disabled in Browser settings/);
  manager.setBrowserSettings({ browserEnabled: true });
  const result = await manager.executeControlCommand({ ...target, action: "discover" });
  assert.equal(result.ok, true);
});

test("native download lifecycle publishes the admitted file and removes only its history entry", async () => {
  const { manager, views, session, events } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  await manager.create({ id: "downloads", conversation_id: "download-owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  let savePath = ""; let received = 0;
  const item = Object.assign(new EventEmitter(), { getFilename: () => "report.txt", getURL: () => "https://example.com/report.txt", getTotalBytes: () => 8,
    getReceivedBytes: () => received, getSavePath: () => savePath, setSavePath(value) { savePath = value; }, isPaused: () => false, canResume: () => false });
  session.listeners.get("will-download")({ preventDefault() { assert.fail("default manual browser downloads must be admitted"); } }, item, views[0].webContents);
  const record = manager.listDownloads()[0];
  assert.equal(record.filename, path.basename(record.path)); assert.match(record.filename, /^report(?: \(\d+\))?\.txt$/); assert.equal(record.state, "progressing");
  assert.equal(events.at(-1)[0].id, record.id);
  received = 8; item.emit("updated", {}, "progressing"); item.emit("done", {}, "completed");
  assert.equal(manager.listDownloads()[0].receivedBytes, 8); assert.equal(manager.listDownloads()[0].state, "completed");
  fs.writeFileSync(savePath, "download");
  await manager.runDownloadAction({ action: "remove", id: record.id });
  assert.equal(manager.listDownloads().length, 0); assert.equal(fs.readFileSync(savePath, "utf8"), "download");
  fs.unlinkSync(savePath);
});

test("download location persists and drives automatic saves, save prompts, folder opening, and reset", async () => {
  const directory = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-download-location-"));
  const settingsFile = path.join(directory, "settings.json");
  let promptedPath;
  const { manager, views, session } = browserHarness({ showSaveDialogSync(_owner, options) { promptedPath = options.defaultPath; return path.join(directory, "chosen.txt"); } });
  manager.init({ browserSettingsPath: settingsFile, lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  manager.setBrowserSettings({ downloadDirectory: directory });
  manager.init({ browserSettingsPath: `${settingsFile}.other` });
  manager.init({ browserSettingsPath: settingsFile });
  assert.equal(manager.getBrowserSettings().downloadDirectory, directory);
  assert.equal(await manager.runDownloadAction({ action: "open-folder" }), directory);
  await manager.create({ id: "location", conversation_id: "download-owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  const items = [];
  const download = () => {
    let savedPath;
    const item = Object.assign(new EventEmitter(), { getFilename: () => "report.txt", getURL: () => "https://example.com/report.txt", getTotalBytes: () => 8,
      getReceivedBytes: () => 0, getSavePath: () => savedPath, setSavePath(value) { savedPath = value; }, isPaused: () => false, canResume: () => false });
    session.listeners.get("will-download")({ preventDefault() { assert.fail("download was unexpectedly blocked"); } }, item, views[0].webContents);
    items.push(item);
    return savedPath;
  };
  assert.equal(download(), path.join(directory, "report.txt"));
  manager.setBrowserSettings({ downloadPolicy: "ask" });
  assert.equal(download(), path.join(directory, "chosen.txt"));
  assert.equal(manager.listDownloads()[0].filename, "chosen.txt");
  assert.equal(promptedPath, path.join(directory, "report (1).txt"));
  items.at(-1).setSavePath(path.join(directory, "actual final name.txt"));
  items.at(-1).emit("updated", {}, "progressing");
  assert.equal(manager.listDownloads()[0].filename, "actual final name.txt");
  items.at(-1).emit("done", {}, "completed");
  assert.equal(manager.listDownloads()[0].filename, "actual final name.txt");
  manager.setBrowserSettings({ downloadDirectory: null, downloadPolicy: "allow" });
  assert.equal(manager.getBrowserSettings().downloadDirectory, null);
  assert.equal(await manager.runDownloadAction({ action: "open-folder" }), require("node:os").tmpdir());
});

test("live DownloadItem actions publish real state and clearing history retains the active owner", async () => {
  const { manager, views, session, events } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  await manager.create({ id: "download-controls", conversation_id: "download-owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  let paused = false; let savePath = ""; let nativeState = "progressing";
  const item = Object.assign(new EventEmitter(), {
    getFilename: () => "live.txt", getURL: () => "https://example.com/live.txt", getTotalBytes: () => 800,
    getReceivedBytes: () => 40, getSavePath: () => savePath, setSavePath(value) { savePath = value; },
    getState: () => nativeState, isPaused: () => paused, canResume: () => paused,
    pause() { paused = true; }, resume() { paused = false; }, cancel() { nativeState = "cancelled"; this.emit("done", {}, nativeState); },
  });
  session.listeners.get("will-download")({ preventDefault() { assert.fail("download was unexpectedly blocked"); } }, item, views[0].webContents);
  const id = manager.listDownloads()[0].id;
  assert.deepEqual(Array.from(manager.getActiveDownloadIds()), [id]);
  assert.equal(manager.listDownloads()[0].canPause, true);
  await assert.rejects(manager.runDownloadAction({ action: "remove", id }), /Cancel the active download/);
  await manager.runDownloadAction({ action: "pause", id });
  assert.equal(paused, true); assert.equal(manager.listDownloads()[0].state, "paused");
  assert.equal(manager.listDownloads()[0].canResume, true); assert.equal(events.at(-1)[0].state, "paused");
  assert.deepEqual(Array.from(manager.getActiveDownloadIds()), [id]);
  item.emit("updated", {}, "progressing");
  assert.equal(manager.listDownloads()[0].state, "paused");
  await manager.clearBrowsingData(["downloads"]);
  assert.equal(manager.listDownloads()[0].id, id);
  await manager.runDownloadAction({ action: "resume", id });
  assert.equal(paused, false); assert.equal(manager.listDownloads()[0].state, "progressing");
  await manager.runDownloadAction({ action: "cancel", id });
  assert.equal(manager.listDownloads()[0].state, "cancelled"); assert.equal(manager.listDownloads()[0].canCancel, false);
  assert.equal(manager.getActiveDownloadIds().length, 0);
  await assert.rejects(manager.runDownloadAction({ action: "resume", id }), /no longer active/);
  await manager.clearBrowsingData(["downloads"]);
  assert.equal(manager.listDownloads().length, 0);
});

test("persisted unfinished downloads have no fictitious live owner after app restart", () => {
  const directory = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-download-restart-"));
  const settingsFile = path.join(directory, "browser-settings.json");
  const downloadsFile = path.join(directory, "browser-downloads.json");
  fs.writeFileSync(downloadsFile, JSON.stringify([{ id: "stopped", filename: "old.txt", path: path.join(directory, "old.txt"), state: "paused" }]));
  const { manager } = browserHarness();
  manager.init({ browserSettingsPath: settingsFile });
  assert.equal(manager.listDownloads()[0].state, "interrupted");
  assert.equal(manager.listDownloads()[0].canCancel, false); assert.equal(manager.listDownloads()[0].canResume, false);
  assert.equal(manager.getActiveDownloadIds().length, 0);
  fs.unlinkSync(downloadsFile); fs.rmdirSync(directory);
});

test("same-name downloads reserve live paths before disk creation and release them on the actual done event", async (t) => {
  const directory = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-download-reservations-"));
  t.after(() => {
    for (const name of ["settings.json", "browser-downloads.json", "report.txt"]) fs.unlinkSync(path.join(directory, name));
    fs.rmdirSync(directory);
  });
  fs.writeFileSync(path.join(directory, "report.txt"), "pre-existing file");
  const { manager, views, session } = browserHarness();
  manager.init({ browserSettingsPath: path.join(directory, "settings.json"), lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  manager.setBrowserSettings({ downloadDirectory: directory });
  await manager.create({ id: "reserve", conversation_id: "owner", window_instance_id: "fixture-window", url: "https://example.com/" });
  const admit = () => {
    let savePath = "";
    const item = Object.assign(new EventEmitter(), { getFilename: () => "report.txt", getURL: () => "https://example.com/report.txt", getTotalBytes: () => 8,
      getReceivedBytes: () => 0, getSavePath: () => savePath, setSavePath(value) { savePath = value; }, isPaused: () => false, canResume: () => false });
    session.listeners.get("will-download")({ preventDefault() { assert.fail("download unexpectedly blocked"); } }, item, views[0].webContents);
    return item;
  };
  const first = admit(), second = admit();
  assert.equal(first.getSavePath(), path.join(directory, "report (1).txt"));
  assert.equal(second.getSavePath(), path.join(directory, "report (2).txt"));
  assert.equal(fs.existsSync(first.getSavePath()), false);
  assert.equal(manager.getActiveDownloadIds().length, 2);
  first.emit("done", {}, "completed");
  const third = admit();
  assert.equal(third.getSavePath(), first.getSavePath());
  assert.equal(manager.listDownloads()[0].filename, "report (1).txt");
  assert.equal(fs.readFileSync(path.join(directory, "report.txt"), "utf8"), "pre-existing file");
  second.emit("done", {}, "cancelled"); third.emit("done", {}, "cancelled");
  assert.equal(manager.getActiveDownloadIds().length, 0);
});

test("browser locale reaches Save and Update without rejecting a normal post-submit navigation", async () => {
  const writes = [], prompts = [];
  let response, locale = "zh-CN";
  const h = browserHarness({ showMessageBox: async (_window, options) => { prompts.push(options); return new Promise(resolve => { response = resolve; }); } });
  const repository = { listPasswords: () => ({ available: true, items: writes.map(item => ({ id: item.id, origin: item.origin, username: item.username })) }),
    getPassword: ({ id }) => writes.find(item => item.id === id), savePassword(item) { if (item.id) Object.assign(writes.find(record => record.id === item.id), item); else writes.push({ ...item, id: "account" }); } };
  h.manager.init({ getLocale: () => locale, browserPersonalData: repository, lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "locale-login", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/login" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents;
  const submission = { origin: "https://example.com", documentUrl: contents.mainFrame.url, username: "fixture-user", password: "secret-one" };
  const pending = h.manager.handlePasswordSubmission({ sender: contents, senderFrame: contents.mainFrame }, submission);
  assert.equal(prompts[0].message, "保存密码？"); assert.deepEqual(Array.from(prompts[0].buttons), ["暂不", "保存"]);
  contents.url = contents.mainFrame.url = "https://example.com/post-login";
  contents.emit("did-navigate", {}, contents.url);
  response({ response: 1 });
  assert.equal(await pending, true); assert.equal(writes[0].origin, "https://example.com");
  locale = "en-US";
  const update = h.manager.handlePasswordSubmission({ sender: contents, senderFrame: contents.mainFrame }, { ...submission, documentUrl: contents.url, password: "secret-two" });
  assert.equal(prompts[1].message, "Update password?"); response({ response: 1 });
  assert.equal(await update, true); assert.equal(writes[0].password, "secret-two");
  assert.equal(JSON.stringify(prompts).includes("secret-one"), false); assert.equal(JSON.stringify(prompts).includes("secret-two"), false);
});

test("individual browsing data actions clear their native category without resetting website permissions", async () => {
  const { manager, session } = browserHarness();
  let historyClears = 0;
  manager.init({ browserHistory: { clear() { historyClears++; } } });
  manager.setBrowserSettings({ origin: "https://example.com", permission: "notifications", allowed: true });
  await manager.clearBrowsingData(["cookies"]);
  assert.deepEqual([...session.clearedStorage[0].storages], ["cookies"]);
  assert.equal(session.clearCacheCalls, 0); assert.equal(historyClears, 0);
  await manager.clearBrowsingData(["siteData", "cache", "history"]);
  assert.equal(session.clearedStorage[1].storages.includes("cookies"), false);
  assert.equal(session.clearCacheCalls, 1); assert.equal(historyClears, 1);
  assert.deepEqual([...manager.getBrowserSettings("https://example.com").permissions], ["notifications"]);
});

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
  const policyDefaults = { camera: "ask", microphone: "ask", geolocation: "ask", notifications: "ask", "clipboard-read": "ask", mediaKeySystem: "allow", sound: "allow", images: "allow", javascript: "allow", usb: "ask" };
  assert.deepEqual(firstChanges.at(-1), { browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", downloadDirectory: null,
    sitePermissions: { "https://a.example": ["notifications"], "https://b.example": ["geolocation"] },
    sitePolicy: { defaults: policyDefaults, devices: [], origins: { "https://a.example": { notifications: "allow" }, "https://b.example": { geolocation: "allow" } } } });
  assert.deepEqual(firstChanges.at(-1), JSON.parse(fs.readFileSync(path.join(root, "settings.json"), "utf8")));
  await manager.create({ id: "clear-a", url: "https://a.example/one", conversation_id: "settings-owner", window_instance_id: "fixture-window" });
  await manager.clearSiteData({ id: "clear-a", conversation_id: "settings-owner", window_instance_id: "fixture-window" });
  assert.equal(session.clearedOrigins[0], "https://a.example");
  const remaining = { browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", downloadDirectory: null,
    sitePermissions: { "https://b.example": ["geolocation"] }, sitePolicy: { defaults: policyDefaults, devices: [], origins: { "https://b.example": { geolocation: "allow" } } } };
  assert.deepEqual(secondChanges.at(-1), remaining);
  stop();
  const firstCount = firstChanges.length;
  await manager.clearBrowsingData();
  assert.equal(firstChanges.length, firstCount);
  assert.equal(session.clearedOrigins.at(-1), "all");
  assert.equal(session.clearCacheCalls, 1);
  assert.deepEqual(secondChanges.at(-1), remaining);
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

test("device emulation follows the centred visual frame through host zoom, hidden resizing and exit", async () => {
  const { manager, views, ownerWindow } = browserHarness();
  ownerWindow.webContents.getZoomFactor = () => 1.3;
  ownerWindow.getContentBounds = () => ({ width: 2000, height: 1600 });
  const target = { id: "device-page", conversation_id: "device-owner", window_instance_id: "fixture-window" };
  await manager.create({ ...target, url: "about:blank" });
  manager.activate(target.id, target.conversation_id, target.window_instance_id);
  manager.setBounds({ ...target, x: 100, y: 80, width: 390, height: 600, viewport: { width: 390, height: 600, mobile: false, scale: 1 } });
  const view = views[0];
  assert.deepEqual({ ...view.bounds }, { x: 130, y: 104, width: 507, height: 780 });
  assert.equal(view.visible, true);
  assert.equal(view.webContents.emulation.scale, 1.3);
  assert.deepEqual({ ...view.webContents.emulation.viewSize }, { width: 390, height: 600 });
  manager.setBounds({ ...target, x: 100, y: 80, width: 0, height: 0, viewport: { width: 500, height: 800, mobile: false, scale: 0.75 } });
  assert.equal(view.visible, false);
  assert.equal(view.webContents.emulation.scale, 0.75 * 1.3);
  assert.deepEqual({ ...view.webContents.emulation.viewSize }, { width: 500, height: 800 });
  manager.setBounds({ ...target, x: 100, y: 80, width: 375, height: 600, viewport: { width: 500, height: 800, mobile: false, scale: 0.75 } });
  assert.equal(view.visible, true);
  assert.deepEqual({ ...view.bounds }, { x: 130, y: 104, width: 487, height: 780 });
  manager.setBounds({ ...target, x: 100, y: 80, width: 0, height: 0, viewport: null });
  assert.equal(view.webContents.emulation, null);
  manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 500, viewport: null });
  assert.equal(view.visible, true);
  assert.deepEqual({ ...view.bounds }, { x: 0, y: 0, width: 780, height: 650 });
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

test("native guest navigation shortcuts use the saved primary target and the actual original owner on Windows", async () => {
  const h = browserHarness({}, { platform: "win32" });
  let primaryNumberShortcutTarget = "tabs";
  h.manager.init({ getShortcutPreferences: () => ({ primaryNumberShortcutTarget }) });
  const target = { id: "numbers", conversation_id: "source-owner", window_instance_id: "fixture-window", url: "about:blank" };
  await h.manager.create(target);
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 });
  h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  const contents = h.views[0].webContents;
  let prevented = 0;
  const key = (change) => contents.emit("before-input-event", { preventDefault() { prevented++; } }, { key: "2", type: "keyDown", control: false, meta: false, alt: false, shift: false, ...change });
  key({ control: true }); key({ alt: true });
  primaryNumberShortcutTarget = "sidebar";
  key({ control: true }); key({ alt: true });
  assert.deepEqual(h.events.filter(event => event.type === "numbered-shortcut").map(event => ({ id: event.id, owner: event.conversationId, ...structuredClone(event.numberedShortcut) })), [
    { id: "numbers", owner: "source-owner", target: "tabs", index: 1 },
    { id: "numbers", owner: "source-owner", target: "sidebar", index: 1 },
    { id: "numbers", owner: "source-owner", target: "sidebar", index: 1 },
    { id: "numbers", owner: "source-owner", target: "tabs", index: 1 },
  ]);
  for (const input of [{ control: true, shift: true }, { control: true, alt: true }, { control: true, meta: true }, { key: "0", control: true }, { type: "keyUp", control: true }, { control: true, isComposing: true }, {}]) key(input);
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 0, height: 0 });
  key({ control: true });
  assert.equal(prevented, 4);
  assert.equal(h.events.filter(event => event.type === "numbered-shortcut").length, 4);
});

test("native guest navigation shortcuts use Command and Control as opposite targets on macOS", async () => {
  const h = browserHarness({}, { platform: "darwin" });
  h.manager.init({ getShortcutPreferences: () => ({ primaryNumberShortcutTarget: "tabs" }) });
  const target = { id: "mac-numbers", conversation_id: "mac-owner", window_instance_id: "fixture-window", url: "about:blank" };
  await h.manager.create(target); h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  let prevented = 0;
  for (const modifiers of [{ meta: true }, { control: true }, { alt: true }, { control: true, meta: true }]) {
    h.views[0].webContents.emit("before-input-event", { preventDefault() { prevented++; } }, { type: "keyDown", key: "9", ...modifiers });
  }
  assert.deepEqual(h.events.filter(event => event.type === "numbered-shortcut").map(event => structuredClone(event.numberedShortcut)), [{ target: "tabs", index: 8 }, { target: "sidebar", index: 8 }]);
  assert.equal(prevented, 2);
});

test("native guest navigation shortcuts give an installed application accelerator its original priority", async () => {
  const menu = { items: [{ enabled: true, submenu: { items: [{ enabled: true, accelerator: "CommandOrControl+2" }] } }] };
  const h = browserHarness({}, { platform: "win32", menu });
  h.manager.init({ getShortcutPreferences: () => ({ primaryNumberShortcutTarget: "tabs" }) });
  const target = { id: "bound-numbers", conversation_id: "source-owner", window_instance_id: "fixture-window", url: "about:blank" };
  await h.manager.create(target); h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  let prevented = 0;
  h.views[0].webContents.emit("before-input-event", { preventDefault() { prevented++; } }, { type: "keyDown", key: "2", control: true });
  assert.equal(prevented, 0);
  assert.equal(h.events.some(event => event.type === "numbered-shortcut"), false);
  menu.items[0].submenu.items[0].enabled = false;
  h.views[0].webContents.emit("before-input-event", { preventDefault() { prevented++; } }, { type: "keyDown", key: "2", control: true });
  assert.equal(prevented, 1);
});

test("native guest external link clicks consume only the exact saved modifier gesture", async () => {
  const h = browserHarness({}, { platform: "win32" });
  let externalBrowserLinkShortcut = "primary";
  h.manager.init({ getShortcutPreferences: () => ({ externalBrowserLinkShortcut }), lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "link-page", conversation_id: "original-owner", window_instance_id: "fixture-window", url: "https://example.com/source" };
  await h.manager.create(target); h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  const contents = h.views[0].webContents;
  const click = (modifiers) => h.manager.handleLinkClick({ sender: contents, senderFrame: contents.mainFrame }, { documentUrl: contents.url, url: "https://destination.example/exact-link", modifiers: { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...modifiers } });
  assert.equal(click({ ctrlKey: true }), true);
  for (const modifiers of [{ ctrlKey: true, shiftKey: true }, { ctrlKey: true, altKey: true }, { metaKey: true }, {}]) assert.equal(click(modifiers), false);
  externalBrowserLinkShortcut = "alt";
  assert.equal(click({ altKey: true }), true); assert.equal(click({ altKey: true, shiftKey: true }), false);
  externalBrowserLinkShortcut = "primaryShift";
  assert.equal(click({ ctrlKey: true, shiftKey: true }), true); assert.equal(click({ ctrlKey: true }), false);
  externalBrowserLinkShortcut = "unassigned";
  assert.equal(click({ ctrlKey: true }), false);
  assert.deepEqual(h.events.filter(event => event.type === "external-link-request").map(event => ({ id: event.id, owner: event.conversationId, url: event.requestedUrl })), Array.from({ length: 3 }, () => ({ id: "link-page", owner: "original-owner", url: "https://destination.example/exact-link" })));
});

test("native guest external link clicks reject old documents, other frames, hidden owners and unsupported targets", async () => {
  const h = browserHarness({}, { platform: "win32" });
  h.manager.init({ getShortcutPreferences: () => ({ externalBrowserLinkShortcut: "primary" }), lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "scoped-link", conversation_id: "original-owner", window_instance_id: "fixture-window", url: "https://example.com/source" };
  await h.manager.create(target); h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  const contents = h.views[0].webContents;
  const event = { sender: contents, senderFrame: contents.mainFrame };
  const request = { documentUrl: contents.url, url: "https://destination.example/clicked", modifiers: { ctrlKey: true, metaKey: false, altKey: false, shiftKey: false } };
  assert.equal(h.manager.handleLinkClick({ ...event, sender: { id: 999 } }, request), false);
  assert.equal(h.manager.handleLinkClick({ ...event, senderFrame: { url: contents.url } }, request), false);
  assert.equal(h.manager.handleLinkClick(event, { ...request, documentUrl: "https://example.com/previous" }), false);
  for (const url of ["file:///C:/secret.txt", "javascript:alert(1)", "mailto:someone@example.com", "about:blank"]) assert.equal(h.manager.handleLinkClick(event, { ...request, url }), false);
  contents.mainFrame.detached = true;
  assert.equal(h.manager.handleLinkClick(event, request), false); contents.mainFrame.detached = false;
  contents.mainFrame.url = "https://example.com/replaced";
  assert.equal(h.manager.handleLinkClick(event, request), false); contents.mainFrame.url = contents.url;
  const other = { id: "other-owner-page", conversation_id: "other-owner", window_instance_id: "fixture-window", url: "about:blank" };
  await h.manager.create(other); h.manager.setBounds({ ...other, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(other.id, other.conversation_id, other.window_instance_id);
  assert.equal(h.manager.handleLinkClick(event, request), false);
  h.manager.close(target);
  assert.equal(h.manager.handleLinkClick(event, request), false);
  assert.equal(h.events.some(event => event.type === "external-link-request"), false);
});

test("native guest external link clicks match Command on macOS without substituting Control", async () => {
  const h = browserHarness({}, { platform: "darwin" });
  h.manager.init({ getShortcutPreferences: () => ({ externalBrowserLinkShortcut: "primary" }), lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "mac-links", conversation_id: "original-owner", window_instance_id: "fixture-window", url: "https://example.com/source" };
  await h.manager.create(target); h.manager.setBounds({ ...target, x: 0, y: 0, width: 600, height: 400 }); h.manager.activate(target.id, target.conversation_id, target.window_instance_id);
  const contents = h.views[0].webContents;
  const request = { documentUrl: contents.url, url: "https://destination.example/clicked", modifiers: { ctrlKey: false, metaKey: true, altKey: false, shiftKey: false } };
  assert.equal(h.manager.handleLinkClick({ sender: contents, senderFrame: contents.mainFrame }, request), true);
  assert.equal(h.manager.handleLinkClick({ sender: contents, senderFrame: contents.mainFrame }, { ...request, modifiers: { ...request.modifiers, ctrlKey: true, metaKey: false } }), false);
});

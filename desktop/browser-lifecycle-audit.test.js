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
  const logs = [];
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
  function createNativeContents() {
    const contents = Object.assign(new EventEmitter(), {
      id: ++id, session, url: "", dead: false, navigationHistory: {},
      pickerCalls: [], stopCalls: 0,
      captureCalls: 0, zoomFactor: 1, findRequests: [], findStopped: false, loadCalls: [], loadOptions: [],
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
      setWindowOpenHandler(handler) { this.windowOpenHandler = handler; }, getURL() { return this.url; }, getTitle: () => "", isLoading: () => false,
      isDestroyed() { return this.dead; }, close() { this.dead = true; },
      async loadURL(url, loadOptions) {
        this.loadCalls.push(url); this.loadOptions.push(loadOptions);
        if (options.loadURL) await options.loadURL(this, url, loadOptions);
        this.url = url; this.mainFrame.url = url;
      },
    });
    contents.mainFrame = { url: "", detached: false, isDestroyed: () => false }; contents.mainFrame.top = contents.mainFrame;
    return contents;
  }
  class View {
    constructor(options) {
      assert.ok(!("webContents" in options) || options.webContents, "Electron's optional webContents must be omitted when no guest exists");
      const { webContents, webPreferences } = options;
      this.webPreferences = webPreferences;
      this.bounds = { x: 0, y: 0, width: 0, height: 0 };
      this.visible = false;
      this.webContents = webContents ?? createNativeContents();
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
    module: moduleValue, exports: moduleValue.exports, __dirname, URL, Buffer, AbortController, Date: options.Date ?? Date, setTimeout, clearTimeout,
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
    appendDesktopLog: message => logs.push(message),
    browserSettingsPath: path.join(fs.mkdtempSync(path.join(require("node:os").tmpdir(), "minicode-browser-audit-")), "settings.json"),
    lookupHostAddresses: () => new Promise((resolve) => { finishDns = resolve; }),
  });
  return { manager, views, events, logs, session, ownerWindow, createNativeContents,
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
  granted = undefined;
  session.permissionRequest(contents, "geolocation", value => { granted = value; }, { requestingUrl: "https://example.com/" });
  assert.equal(granted, undefined);
  manager.close({ id: "permissions", conversation_id: "permission-owner", window_instance_id: "fixture-window" });
  assert.equal(granted, false);
  assert.throws(() => manager.removeSitePermissions({ origin: "file:///local" }), /valid HTTP or HTTPS origin/);
});

function permissionDialog() {
  const prompts = [];
  return { prompts, showMessageBox(_window, options) {
    return new Promise(resolve => {
      prompts.push({ options, respond: resolve });
      options.signal.addEventListener("abort", () => resolve({ response: 0 }), { once: true });
    });
  } };
}

test("hidden guest permission requests keep their callback and resume the original prompt when its page is presented", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "pending-permission", conversation_id: "owner", window_instance_id: "fixture-window" };
  const other = { ...target, id: "other-page" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  await h.manager.create({ ...other, url: "https://other.example/" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 });
  h.manager.setBounds({ ...other, x: 0, y: 0, width: 400, height: 400 });
  const contents = h.views[0].webContents; const results = [];
  h.session.permissionRequest(contents, "geolocation", granted => results.push(granted), { requestingUrl: contents.url });
  assert.deepEqual(results, []); assert.equal(dialog.prompts.length, 0);
  h.manager.activate(target.id, "owner", "fixture-window");
  assert.equal(dialog.prompts.length, 1);
  h.manager.activate(other.id, "owner", "fixture-window");
  assert.equal(dialog.prompts[0].options.signal.aborted, true);
  await new Promise(setImmediate);
  assert.deepEqual(results, []); assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
  h.manager.activate(target.id, "owner", "fixture-window");
  assert.equal(dialog.prompts.length, 2);
  dialog.prompts[1].respond({ response: 1 }); await new Promise(setImmediate);
  assert.deepEqual(results, [true]);
  assert.equal(h.session.permissionCheck(contents, "geolocation", "https://example.com", {}), true);
  assert.equal(h.session.permissionCheck(h.views[1].webContents, "geolocation", "https://other.example", {}), false);
  assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
  h.manager.disposeAll();
});

test("guest permission prompts serialize and preserve an explicit policy changed while the native dialog is open", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "serial-permissions", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents; const results = [];
  h.session.permissionRequest(contents, "geolocation", granted => results.push(["location", granted]), { requestingUrl: contents.url });
  h.session.permissionRequest(contents, "media", granted => results.push(["camera", granted]), { requestingUrl: contents.url, mediaTypes: ["video"] });
  assert.equal(dialog.prompts.length, 1);
  dialog.prompts[0].respond({ response: 1 }); await new Promise(setImmediate);
  assert.equal(dialog.prompts.length, 2); assert.deepEqual(results, [["location", true]]);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "camera", setting: "block" });
  assert.equal(dialog.prompts[1].options.signal.aborted, true);
  assert.deepEqual(results, [["location", true], ["camera", false]]);
  dialog.prompts[1].respond({ response: 2 }); await new Promise(setImmediate);
  assert.deepEqual(results, [["location", true], ["camera", false]]);
  assert.equal(h.manager.getSitePolicy().sites[0].settings.camera, "block");
  h.manager.disposeAll();
});

test("one original permission choice settles duplicate queued requests without another prompt", async () => {
  for (const response of [0, 1, 2]) {
    const dialog = permissionDialog(); const h = browserHarness(dialog);
    h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
    const target = { id: "duplicate-permissions", conversation_id: "owner", window_instance_id: "fixture-window" };
    await h.manager.create({ ...target, url: "https://example.com/" });
    h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
    const contents = h.views[0].webContents; const results = [];
    for (const name of ["first", "second"]) h.session.permissionRequest(contents, "notifications", allowed => results.push([name, allowed]), { requestingUrl: contents.url });
    assert.equal(dialog.prompts.length, 1);
    dialog.prompts[0].respond({ response }); await new Promise(setImmediate);
    assert.deepEqual(results, [["first", response !== 0], ["second", response !== 0]]);
    assert.equal(dialog.prompts.length, 1);
    assert.equal(h.session.permissionCheck(contents, "notifications", "https://example.com", {}), response !== 0);
    if (response === 1) assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
    else assert.equal(h.manager.getSitePolicy().sites[0].settings.notifications, response === 2 ? "allow" : "block");
    h.manager.disposeAll();
    assert.equal(results.length, 2);
  }
});

test("a global site block settles visible and hidden original callbacks and cancels stale presentation", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const first = { id: "visible-policy", conversation_id: "first-owner", window_instance_id: "fixture-window" };
  const second = { id: "hidden-policy", conversation_id: "second-owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...first, url: "https://first.example/" });
  await h.manager.create({ ...second, url: "https://second.example/" });
  h.manager.setBounds({ ...first, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(first.id, first.conversation_id, "fixture-window");
  const results = [];
  for (const [index, view] of h.views.entries()) h.session.permissionRequest(view.webContents, "notifications", value => results.push([index, value]), { requestingUrl: view.webContents.url });
  assert.equal(dialog.prompts.length, 1);
  h.manager.setSitePolicy({ category: "notifications", setting: "block" });
  assert.deepEqual(results, [[0, false], [1, false]]);
  assert.equal(dialog.prompts[0].options.signal.aborted, true);
  dialog.prompts[0].respond({ response: 2 }); await new Promise(setImmediate);
  assert.deepEqual(results, [[0, false], [1, false]]);
  assert.equal(h.manager.getSitePolicy().defaults.notifications, "block");
  assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
  h.manager.activate(second.id, second.conversation_id, "fixture-window");
  assert.equal(dialog.prompts.length, 1);
  h.manager.disposeAll();
});

test("saved site grants resolve hidden pending requests for only their original origin", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  for (const [id, origin] of [["first", "https://first.example"], ["second", "https://second.example"]])
    await h.manager.create({ id, conversation_id: id, window_instance_id: "fixture-window", url: origin });
  const results = [];
  for (const [index, view] of h.views.entries()) h.session.permissionRequest(view.webContents, "geolocation", value => results.push([index, value]), { requestingUrl: view.webContents.url });
  h.manager.setBrowserSettings({ origin: "https://first.example", permission: "geolocation", allowed: true });
  assert.deepEqual(results, [[0, true]]);
  assert.equal(dialog.prompts.length, 0);
  h.manager.disposeAll();
  assert.deepEqual(results, [[0, true], [1, false]]);
});

test("mixed media requests retain the original combined decision until all grants or one block exist", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "mixed-media", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents; const results = [];
  h.session.permissionRequest(contents, "media", value => results.push(value), { requestingUrl: contents.url, mediaTypes: ["video", "audio"] });
  h.manager.setSitePolicy({ origin: "https://example.com", category: "camera", setting: "allow" });
  assert.deepEqual(results, []);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "microphone", setting: "block" });
  assert.deepEqual(results, [false]);
  assert.equal(dialog.prompts.length, 0);
  h.manager.disposeAll();
  assert.deepEqual(results, [false]);
});

test("resetting one site category and the whole site revokes actual temporary document grants", async () => {
  const dialog = permissionDialog(); const h = browserHarness(dialog);
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "reset-one-time", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  h.manager.setBounds({ ...target, x: 0, y: 0, width: 400, height: 400 }); h.manager.activate(target.id, "owner", "fixture-window");
  const contents = h.views[0].webContents;
  const first = new Promise(resolve => h.session.permissionRequest(contents, "media", resolve, { requestingUrl: contents.url, mediaTypes: ["video", "audio"] }));
  dialog.prompts[0].respond({ response: 1 }); assert.equal(await first, true); await new Promise(setImmediate);
  h.manager.setSitePolicy({ origin: "https://example.com", category: "camera", setting: "default" });
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "video" }), false);
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "audio" }), true);
  h.manager.removeSitePermissions({ origin: "https://example.com" });
  assert.equal(h.session.permissionCheck(contents, "media", "https://example.com", { mediaType: "audio" }), false);
  const next = new Promise(resolve => h.session.permissionRequest(contents, "media", resolve, { requestingUrl: contents.url, mediaTypes: ["audio"] }));
  assert.equal(dialog.prompts.length, 2);
  dialog.prompts[1].respond({ response: 0 }); assert.equal(await next, false);
  h.manager.disposeAll();
});

test("navigation, close and renderer exit settle only their original guest permission callbacks exactly once", async () => {
  for (const lifecycle of ["navigation", "close", "renderer-exit"]) {
    const dialog = permissionDialog(); const h = browserHarness(dialog);
    h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
    const target = { id: "retired-permission", conversation_id: "owner", window_instance_id: "fixture-window" };
    await h.manager.create({ ...target, url: "https://example.com/" });
    const contents = h.views[0].webContents; const results = [];
    h.session.permissionRequest(contents, "geolocation", granted => results.push(granted), { requestingUrl: contents.url });
    if (lifecycle === "navigation") contents.emit("did-navigate", {}, "https://example.com/next");
    else if (lifecycle === "close") h.manager.close(target);
    else contents.emit("render-process-gone", {}, { reason: "crashed" });
    assert.deepEqual(results, [false]); assert.equal(dialog.prompts.length, 0);
    h.manager.disposeAll(); await new Promise(setImmediate);
    assert.deepEqual(results, [false]); assert.deepEqual(structuredClone(h.manager.getSitePolicy().sites), []);
  }
});

test("native child windows adopt Chromium's original contents without replaying the destination or crossing owners", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "opener", conversation_id: "original-owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents;
  const postBody = { contentType: "application/x-www-form-urlencoded", data: [{ type: "rawData", bytes: Buffer.from("form=original") }] };
  const result = contents.windowOpenHandler({ url: "https://destination.example/submit", postBody });
  assert.equal(result.action, "allow"); assert.equal(typeof result.createWindow, "function");
  assert.equal(result.outlivesOpener, true);
  const preferences = result.overrideBrowserWindowOptions.webPreferences;
  assert.equal(preferences.preload, path.join(__dirname, "browser-password-preload.js"));
  assert.equal(preferences.nodeIntegration, false); assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.sandbox, true); assert.equal(preferences.partition, "persist:minicode-browser");
  const originalChild = h.createNativeContents();
  const child = result.createWindow({ webContents: originalChild, webPreferences: { openerId: contents.id, nodeIntegration: true, contextIsolation: false } });
  assert.equal(child, originalChild); assert.equal(child, h.views[1].webContents); assert.equal(child.loadCalls.length, 0);
  assert.equal(h.views[1].webPreferences.openerId, contents.id);
  assert.equal(h.views[1].webPreferences.nodeIntegration, false); assert.equal(h.views[1].webPreferences.contextIsolation, true);
  assert.equal(h.views[1].webPreferences.sandbox, true);
  assert.equal(h.manager.listTargets("original-owner", "fixture-window").length, 2);
  assert.equal(h.manager.listTargets("another-owner", "fixture-window").length, 0);
  const published = h.events.at(-1);
  assert.equal(published.conversationId, "original-owner"); assert.equal(published.type, "page");
  assert.equal(published.url, "https://destination.example/submit");
  assert.equal(published.active, true); assert.equal(published.insertToRightOfTabId, target.id);
  assert.equal(JSON.stringify(h.events).includes("form=original"), false);
  assert.equal(contents.windowOpenHandler({ url: "file:///C:/private" }).action, "deny");
  h.manager.disposeAll();
});

test("OpenURLFromTab starts a missing guest exactly once with its original referrer, POST bytes and multipart boundary", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "native-source", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://source.example/original" });
  h.manager.activate(target.id, "owner", "fixture-window");
  const source = h.views[0].webContents;
  const referrer = { url: source.getURL(), policy: "strict-origin-when-cross-origin" };
  for (const postBody of [undefined,
    { contentType: "application/x-www-form-urlencoded", data: [{ type: "rawData", bytes: Buffer.from("original=native+value") }] },
    { contentType: "multipart/form-data", boundary: "----OriginalBoundary", data: [
      { type: "rawData", bytes: Buffer.from("------OriginalBoundary\r\nContent-Disposition: form-data; name=original\r\n\r\nnative value\r\n") },
      { type: "file", filePath: "original-upload.txt", offset: 3, length: 11, modificationTime: 17 },
    ] },
  ]) {
    const destination = "https://destination.example/original";
    const result = source.windowOpenHandler({ url: destination, disposition: "background-tab", referrer, postBody });
    assert.equal(result.outlivesOpener, true);
    const child = result.createWindow({ webContents: undefined, webPreferences: result.overrideBrowserWindowOptions.webPreferences });
    assert.deepEqual(child.loadCalls, [destination]);
    assert.equal(child.loadOptions.length, 1); assert.equal(child.loadOptions[0].httpReferrer, referrer);
    if (postBody) {
      assert.equal(child.loadOptions[0].postData, postBody.data);
      assert.equal(child.loadOptions[0].extraHeaders, `content-type: ${postBody.contentType}${postBody.boundary ? `; boundary=${postBody.boundary}` : ""}`);
    } else {
      assert.equal("postData" in child.loadOptions[0], false); assert.equal("extraHeaders" in child.loadOptions[0], false);
    }
    assert.equal("openerId" in h.views.at(-1).webPreferences, false, "the browser-created page does not invent an opener");
    const published = h.events.at(-1);
    assert.equal(published.active, false); assert.equal(published.insertToRightOfTabId, target.id);
    assert.equal(h.manager.listTargets("owner", "fixture-window").find(page => page.active).id, target.id);
  }
  assert.equal(JSON.stringify(h.events).includes("original=native+value"), false);
  h.manager.disposeAll();
});

test("a browser-created child keeps native navigation failure observable and closing its source preserves the child", async () => {
  const failure = new Error("ERR_NAME_NOT_RESOLVED: original browser-created destination");
  const h = browserHarness({}, { loadURL: contents => { if (contents.id > 1) throw failure; } });
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "source", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://source.example/" });
  const child = h.views[0].webContents.windowOpenHandler({ url: "https://destination.example/", disposition: "background-tab",
    referrer: { url: "https://source.example/", policy: "default" } }).createWindow({ webContents: undefined, webPreferences: {} });
  child.emit("did-fail-load", {}, -105, failure.message, "https://destination.example/", true);
  await new Promise(setImmediate);
  assert.equal(h.events.at(-1).type, "error"); assert.equal(h.events.at(-1).error, failure.message);
  assert.ok(h.logs.some(line => line.includes(failure.message)));
  h.manager.close(target);
  assert.equal(child.isDestroyed(), false);
  assert.equal(h.manager.listTargets("owner", "fixture-window").length, 1);
  h.manager.disposeAll();
});

test("an adopted native child continues its original POST with its exact inherited private origin grant", async () => {
  let respond;
  const h = browserHarness({ showMessageBox: () => new Promise(resolve => { respond = resolve; }) });
  const url = "http://127.0.0.1:3031/";
  await h.manager.create({ id: "private-opener", conversation_id: "owner", window_instance_id: "fixture-window", url }, {
    navigationAuthorization: { kind: "owned_preview", url, conversation_id: "owner" },
  });
  const parent = h.views[0].webContents;
  const originalChild = h.createNativeContents();
  const child = parent.windowOpenHandler({ url: new URL("/submit", url).href }).createWindow({ webContents: originalChild, webPreferences: {} });
  const results = [];
  h.beforeRequest({ id: 21, webContentsId: originalChild.id, resourceType: "mainFrame", method: "POST", url: new URL("/submit", url).href }, result => results.push(result));
  await new Promise(setImmediate);
  assert.equal(child, originalChild);
  assert.deepEqual(structuredClone(results), [{ cancel: false }]);
  assert.equal(respond, undefined, "the already approved exact origin does not ask again");
  assert.deepEqual(child.loadCalls, []);
  h.beforeRequest({ id: 22, webContentsId: originalChild.id, resourceType: "mainFrame", method: "POST", url: "http://127.0.0.1:3032/submit" }, result => results.push(result));
  await new Promise(setImmediate);
  assert.equal(typeof respond, "function"); assert.equal(results.length, 1);
  respond({ response: 1 }); await new Promise(setImmediate);
  assert.deepEqual(structuredClone(results), [{ cancel: false }, { cancel: false }]);
  assert.deepEqual(child.loadCalls, []);
  h.manager.disposeAll();
});

test("private-network approval continues the original main-frame transaction and rejects a closed owner", async () => {
  for (const finish of ["allow", "cancel", "close"]) {
    let respond; const h = browserHarness({ showMessageBox: () => new Promise(resolve => { respond = resolve; }) });
    h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
    const target = { id: "private-form", conversation_id: "owner", window_instance_id: "fixture-window" };
    await h.manager.create({ ...target, url: "https://example.com/" });
    const contents = h.views[0].webContents;
    let blocked = false;
    contents.emit("will-navigate", { preventDefault() { blocked = true; } }, "http://127.0.0.1/submit");
    assert.equal(blocked, false);
    const results = [];
    h.beforeRequest({ id: 19, webContentsId: contents.id, resourceType: "mainFrame", method: "POST", url: "http://127.0.0.1/submit" }, decision => results.push(decision));
    await new Promise(setImmediate); assert.equal(typeof respond, "function"); assert.deepEqual(results, []);
    if (finish === "close") h.manager.close(target);
    respond({ response: finish === "cancel" ? 0 : 1 }); await new Promise(setImmediate);
    assert.deepEqual(structuredClone(results), [{ cancel: finish !== "allow" }]);
    assert.deepEqual(contents.loadCalls, ["https://example.com/"]);
    h.manager.disposeAll();
  }
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

test("native text history stays on the presented browser document and rejects another window", async () => {
  const { manager, views, ownerWindow } = browserHarness();
  manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "menu-undo", conversation_id: "menu-undo-owner", window_instance_id: "fixture-window" };
  await manager.create({ ...target, url: "https://example.com/" });
  const contents = views[0].webContents;
  const calls = [];
  contents.undo = () => calls.push("undo"); contents.redo = () => calls.push("redo");
  manager.activate(target.id, target.conversation_id, target.window_instance_id);
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "undo"), false);
  manager.setBounds({ ...target, x: 50, y: 100, width: 800, height: 600 });
  assert.equal(manager.runMenuCommand({}, contents, "undo"), false);
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "undo"), true);
  assert.equal(manager.runMenuCommand(ownerWindow, contents, "redo"), true);
  assert.deepEqual(calls, ["undo", "redo"]);
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

function attemptDownload(harness, contents, url) {
  let saved = "", cancelled = 0, prevented = false;
  const item = Object.assign(new EventEmitter(), { getFilename: () => "actor-owned.txt", getURL: () => url,
    getTotalBytes: () => 12, getReceivedBytes: () => 0, getSavePath: () => saved, setSavePath(value) { saved = value; },
    getState: () => "progressing", isPaused: () => false, canResume: () => false,
    hasUserGesture() { assert.fail("download admission cannot guess its actor from a gesture"); },
    cancel() { cancelled++; },
  });
  harness.session.listeners.get("will-download")({ preventDefault() { prevented = true; } }, item, contents);
  return { item, prevented, cancelled, saved };
}

test("authenticated agent downloads consume only an exact one-time URL and expose only the original owner's actual download", async () => {
  const h = browserHarness({ showSaveDialogSync() { assert.fail("an explicitly allowed agent download must not invoke the manual save-location prompt"); } });
  h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "download-actor", conversation_id: "download-owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents;
  const manual = attemptDownload(h, contents, "https://example.com/manual.txt");
  assert.equal(manual.prevented, false); assert.equal(h.manager.listDownloads()[0].isUserInitiated, true);
  h.manager.setBrowserSettings({ downloadPolicy: "ask" });
  bridge.init({ token: "download-backend-token", manager: h.manager });
  const endpoint = await bridge.start();
  const owner = { target_id: target.id, conversation_id: target.conversation_id, window_instance_id: "fixture-window",
    window_binding_token: "fixture-binding", agent_control_id: "actual-browser-turn", agent_turn_id: "original-turn" };
  const command = async payload => {
    const response = await fetch(`${endpoint}/v1/command`, { method: "POST", headers: { authorization: "Bearer download-backend-token" }, body: JSON.stringify({ ...owner, ...payload }) });
    const body = await response.json(); assert.equal(response.status, 200, JSON.stringify(body)); return body;
  };
  try {
    await command({ action: "get_url" });
    const unapproved = attemptDownload(h, contents, "https://example.com/requested.txt");
    assert.equal(unapproved.prevented, true); assert.equal(unapproved.cancelled, 1);
    const originalUrl = "https://example.com/requested.txt?literal=1";
    assert.equal((await command({ action: "allow_download", url: originalUrl })).value, originalUrl);
    assert.equal(attemptDownload(h, contents, "https://example.com/requested.txt?literal=2").prevented, true);
    const admitted = attemptDownload(h, contents, originalUrl);
    assert.equal(admitted.prevented, false); assert.equal(admitted.cancelled, 0);
    assert.equal(attemptDownload(h, contents, originalUrl).prevented, true);
    const record = (await command({ action: "list_downloads" })).value[0];
    assert.equal(record.url, originalUrl); assert.equal(record.path, admitted.saved);
    assert.equal(record.conversationId, "download-owner"); assert.equal(record.tabId, target.id); assert.equal(record.isUserInitiated, false);
    assert.equal((await command({ action: "list_downloads", conversation_id: "another-owner" })).value.length, 0);
    const unowned = attemptDownload(h, { id: 99999 }, originalUrl);
    assert.equal(unowned.prevented, true); assert.equal(unowned.cancelled, 1);
  } finally { await bridge.stop(); h.manager.disposeAll(); }
});

test("agent download grants expire at the Source boundary and an old release cannot clear another live owner", async () => {
  let now = 1000;
  class Clock extends Date { static now() { return now; } }
  const h = browserHarness({}, { Date: Clock }); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "download-ttl", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents;
  const first = { agentControl: { id: "first-resource-owner", turnId: "turn-first" } }, second = { agentControl: { id: "second-resource-owner", turnId: "turn-second" } };
  const grant = (url, control = first) => h.manager.executeControlCommand({ ...target, target_id: target.id, action: "allow_download", url }, control);
  const url = "https://example.com/exact.txt";
  await grant(url); now += 10000;
  assert.equal(attemptDownload(h, contents, url).prevented, false);
  await grant(url); now += 10001;
  assert.equal(attemptDownload(h, contents, url).prevented, true);
  await grant(url, second);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: { ...first.agentControl, turnId: "forged-turn" } }), 0);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: first.agentControl }), 1);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: first.agentControl }), 0);
  assert.equal(attemptDownload(h, contents, "https://example.com/not-granted.txt").prevented, true);
  assert.equal(attemptDownload(h, contents, url).prevented, false);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: second.agentControl }), 1);
  assert.equal(attemptDownload(h, contents, "https://example.com/manual-again.txt").prevented, false);
  h.manager.disposeAll();
});

test("known blob download grants stay exact and native child pages inherit control without borrowing a parent's allowance", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "blob-owner", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const control = { agentControl: { id: "blob-resource-owner", turnId: "blob-turn" } };
  const url = "blob:https://example.com/exact-object-url";
  await h.manager.executeControlCommand({ ...target, target_id: target.id, action: "allow_download", url }, control);
  const parent = h.views[0].webContents;
  const originalChild = h.createNativeContents();
  const child = parent.windowOpenHandler({ url: "https://example.com/child" }).createWindow({ webContents: originalChild, webPreferences: {} });
  assert.equal(child, originalChild);
  assert.equal(attemptDownload(h, child, url).prevented, true);
  assert.equal(attemptDownload(h, parent, "blob:https://example.com/another-object").prevented, true);
  assert.equal(attemptDownload(h, parent, url).prevented, false);
  await assert.rejects(h.manager.executeControlCommand({ ...target, target_id: target.id, action: "allow_download", url: "blob:https://other.example/object" }, control), /original page origin/);
  await assert.rejects(h.manager.executeControlCommand({ ...target, target_id: target.id, action: "allow_download", url }, {}), /authenticated agent control/);
  h.manager.disposeAll();
});

function blobDebugger(contents) {
  const debuggerApi = Object.assign(new EventEmitter(), { attached: false, attachCalls: 0, detachCalls: 0, methods: [],
    isAttached() { return this.attached; }, attach() { this.attached = true; this.attachCalls++; },
    detach() { this.attached = false; this.detachCalls++; this.emit("detach", {}, "detached"); },
    async sendCommand(method) { this.methods.push(method); return method === "Page.getFrameTree" ? { frameTree: { frame: { id: "actual-main-frame", url: contents.getURL() } } } : {}; },
  });
  contents.debugger = debuggerApi;
  contents.isDevToolsOpened = () => false;
  return debuggerApi;
}

test("unknown blob downloads require the actual original main-frame event before one exact allowance is produced", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "unknown-blob", target_id: "unknown-blob", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents, debuggerApi = blobDebugger(contents);
  const control = { agentControl: { id: "blob-main-frame-owner", turnId: "blob-turn" } };
  const register = extra => h.manager.executeControlCommand({ ...target, action: "set_blob_download_origin", origin: "https://example.com", ...extra }, control);
  const event = (url, frameId = "actual-main-frame", sessionId) => debuggerApi.emit("message", {}, "Page.downloadWillBegin", { url, frameId }, sessionId);
  await register({});
  assert.deepEqual(debuggerApi.methods, ["Page.enable", "Page.getFrameTree"]);
  const url = "blob:https://example.com/dynamic-object-url";
  event(url, "iframe"); assert.equal(attemptDownload(h, contents, url).prevented, true);
  event("blob:https://other.example/object"); assert.equal(attemptDownload(h, contents, "blob:https://other.example/object").prevented, true);
  event(url, "actual-main-frame", "nested-session"); assert.equal(attemptDownload(h, contents, url).prevented, true);
  event(url, "actual-main-frame", ""); assert.equal(attemptDownload(h, contents, url).prevented, false);
  assert.equal(attemptDownload(h, contents, url).prevented, true);
  const exact = "blob:https://example.com/expected-object";
  await register({ url: exact });
  event(url); assert.equal(attemptDownload(h, contents, url).prevented, true);
  event(exact); assert.equal(attemptDownload(h, contents, exact).prevented, false);
  assert.equal(debuggerApi.attachCalls, 1, "the second request uses the existing entry-owned debugger");
  await register({});
  await h.manager.executeControlCommand({ ...target, action: "allow_download", url: "https://example.com/exact.txt" }, control);
  event(url); assert.equal(attemptDownload(h, contents, url).prevented, true);
  assert.equal(attemptDownload(h, contents, "https://example.com/exact.txt").prevented, false);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: control.agentControl }), 1);
  assert.equal(debuggerApi.detachCalls, 1); assert.equal(debuggerApi.listenerCount("message"), 0);
  h.manager.disposeAll();
});

test("blob origin authorization never takes another debugger and follows only its current entry owner through navigation and close", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "blob-lifecycle", target_id: "blob-lifecycle", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents, debuggerApi = blobDebugger(contents);
  const first = { agentControl: { id: "first-blob-owner", turnId: "first-turn" } }, second = { agentControl: { id: "second-blob-owner", turnId: "second-turn" } };
  const command = (control, origin = "https://example.com") => h.manager.executeControlCommand({ ...target, action: "set_blob_download_origin", origin }, control);
  contents.isDevToolsOpened = () => true;
  await assert.rejects(command(first), /owned by another consumer/);
  contents.isDevToolsOpened = () => false; debuggerApi.attached = true;
  await assert.rejects(command(first), /owned by another consumer/);
  assert.equal(debuggerApi.attachCalls, 0); assert.equal(debuggerApi.detachCalls, 0);
  debuggerApi.attached = false;
  await command(first);
  await command(second);
  assert.equal(h.manager.releaseAgentControl({ ...target, control: first.agentControl }), 1);
  assert.equal(debuggerApi.detachCalls, 0);
  const url = "blob:https://example.com/second";
  debuggerApi.emit("message", {}, "Page.downloadWillBegin", { frameId: "actual-main-frame", url });
  assert.equal(attemptDownload(h, contents, url).prevented, false);
  await command(second);
  contents.url = "https://other.example/"; contents.mainFrame.url = contents.url;
  contents.emit("did-navigate", {}, "https://other.example/");
  debuggerApi.emit("message", {}, "Page.downloadWillBegin", { frameId: "actual-main-frame", url });
  assert.equal(attemptDownload(h, contents, url).prevented, true);
  await command(second, "https://other.example");
  await command(second, null);
  const clearedUrl = "blob:https://other.example/after-clear";
  debuggerApi.emit("message", {}, "Page.downloadWillBegin", { frameId: "actual-main-frame", url: clearedUrl });
  assert.equal(attemptDownload(h, contents, clearedUrl).prevented, true);
  h.manager.close(target);
  assert.equal(debuggerApi.detachCalls, 1); assert.equal(debuggerApi.listenerCount("message"), 0); assert.equal(debuggerApi.listenerCount("detach"), 0);
});

test("missing original frame facts cannot authorize a blob and a late debugger reply cannot attach permission to a changed document", async () => {
  const h = browserHarness(); h.manager.init({ lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }] });
  const target = { id: "blob-frame-facts", target_id: "blob-frame-facts", conversation_id: "owner", window_instance_id: "fixture-window" };
  await h.manager.create({ ...target, url: "https://example.com/" });
  const contents = h.views[0].webContents, debuggerApi = blobDebugger(contents);
  const control = { agentControl: { id: "original-blob-owner", turnId: "original-turn" } };
  debuggerApi.sendCommand = async () => ({});
  await assert.rejects(h.manager.executeControlCommand({ ...target, action: "set_blob_download_origin", origin: "https://example.com" }, control), /original browser main frame/);
  let respond;
  debuggerApi.sendCommand = async method => method === "Page.getFrameTree" ? new Promise(resolve => { respond = resolve; }) : {};
  const pending = h.manager.executeControlCommand({ ...target, action: "set_blob_download_origin", origin: "https://example.com" }, control);
  await new Promise(setImmediate);
  contents.emit("did-navigate", {}, "https://example.com/later");
  respond({ frameTree: { frame: { id: "actual-main-frame", url: "https://example.com/" } } });
  await assert.rejects(pending, /original browser main frame/);
  assert.equal(attemptDownload(h, contents, "blob:https://example.com/no-facts").prevented, true);
  h.manager.disposeAll(); assert.equal(debuggerApi.detachCalls, 1);
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
  const { manager, views, events, responseStarted, beforeRequest } = browserHarness({
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
    assert.equal(event.prevented, false);
    let redirectDecision;
    beforeRequest({ id: 73, webContentsId: contents.id, resourceType: "mainFrame", url: other }, value => { redirectDecision = value; });
    await new Promise(setImmediate);
    assert.deepEqual(structuredClone(redirectDecision), { cancel: true });
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
  const { manager, views, events, beforeRequest } = browserHarness({ showMessageBox() { dialogStarted(); return dialog; } });
  await manager.create({ id: "redirect", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const contents = views[0].webContents;
  const loads = [];
  const redirectDecisions = [];
  contents.loadURL = (url) => {
    loads.push(url);
    contents.emit("will-redirect", { preventDefault() {} }, "http://127.0.0.1:8000/admin", false, true);
    beforeRequest({ id: 81, webContentsId: contents.id, resourceType: "mainFrame", url: "http://127.0.0.1:8000/admin" }, value => redirectDecisions.push(value));
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
    assert.deepEqual(structuredClone(redirectDecisions), [{ cancel: true }]);
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

function captureParentHarness(script, withholdTermination = false) {
  const os = require("node:os");
  const { spawn } = require("node:child_process");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-capture-parent-audit-"));
  const signals = [];
  let run, options, profile, child, kill, closed, resolveReady;
  let stdout = "", stderr = "";
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const write = (channel, chunk) => {
    if (channel === "stdout") stdout += chunk; else stderr += chunk;
    if (stdout.includes("[capture] controlled child ready") && stderr.includes("controlled child diagnostic")) resolveReady();
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "browser-capture.test.js"), "utf8"), {
    __dirname, __filename: path.join(__dirname, "browser-capture.test.js"), setTimeout, clearTimeout,
    process: { env: {}, platform: process.platform, stdout: { write: chunk => write("stdout", chunk) }, stderr: { write: chunk => write("stderr", chunk) } },
    require(name) {
      if (name === "node:test") return { test(_name, value, callback) { options = value; run = callback; } };
      if (name === "electron") return "unused-electron-fixture";
      if (name === "node:os") return { ...os, tmpdir: () => root };
      if (name === "node:fs") return { ...fs, mkdtempSync(prefix) { profile = fs.mkdtempSync(prefix); return profile; } };
      if (name === "node:child_process") return { spawn() {
        child = spawn(process.execPath, ["-e", script], { cwd: __dirname, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
        kill = child.kill.bind(child);
        child.kill = signal => { signals.push(signal); return withholdTermination && signal === "SIGTERM" ? true : kill(signal); };
        closed = require("node:events").once(child, "close");
        return child;
      } };
      return require(name);
    },
  }, { filename: "browser-capture.test.js" });
  return { run: () => run(), ready, signals, get options() { return options; }, get child() { return child; },
    get profile() { return profile; }, get stdout() { return stdout; }, get stderr() { return stderr; },
    async cleanup() {
      if (child && child.exitCode === null && child.signalCode === null) kill("SIGKILL");
      if (closed) await closed;
      assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()));
      assert.ok(path.basename(root).startsWith("minicode-capture-parent-audit-"));
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

const captureChildOutput = "process.stdout.write('[capture] controlled child ready\\n'); process.stderr.write('controlled child diagnostic\\n'); ";

test("capture fixture forwards its real child diagnostics live and waits for successful stdio close", async () => {
  const h = captureParentHarness(captureChildOutput + "setTimeout(() => process.exit(0), 100);");
  try {
    const running = h.run();
    await h.ready;
    assert.match(h.stdout, /controlled child ready/);
    assert.match(h.stderr, /controlled child diagnostic/);
    assert.equal(h.child.exitCode, null);
    await running;
    assert.equal(h.options.timeout, 25000);
    assert.equal(fs.existsSync(h.profile), false);
    assert.deepEqual(h.signals, []);
  } finally { await h.cleanup(); }
});

test("capture fixture preserves the original failure and profile after the owned child closes", async () => {
  const h = captureParentHarness(captureChildOutput + "process.stderr.write('native fixture failure\\n'); process.exit(1);");
  try {
    await assert.rejects(h.run(), /native fixture failure/);
    assert.equal(fs.existsSync(h.profile), true);
    assert.ok(h.stderr.includes(`[capture] failed profile retained: ${h.profile}`));
    assert.deepEqual(h.signals, []);
  } finally { await h.cleanup(); }
});

test("capture fixture deadline reaps its original child when termination produces no close", { timeout: 25000 }, async () => {
  const h = captureParentHarness(captureChildOutput + "setInterval(() => {}, 1000);", true);
  try {
    const running = h.run();
    await h.ready;
    await assert.rejects(running, /Electron browser capture timed out after 20000ms/);
    assert.deepEqual(h.signals, ["SIGTERM", "SIGKILL"]);
    assert.equal(h.child.signalCode, "SIGKILL");
    assert.equal(h.child.stdout.closed, true);
    assert.equal(h.child.stderr.closed, true);
    assert.equal(fs.existsSync(h.profile), true);
    assert.ok(h.stderr.includes(`[capture] failed profile retained: ${h.profile}`));
  } finally { await h.cleanup(); }
});

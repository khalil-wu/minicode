"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const http = require("node:http");
const { EventEmitter } = require("node:events");

function loadProduction(file, electron) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, file), "utf8"), {
    module, exports: module.exports, Buffer, URL, console, process,
    setTimeout, clearTimeout,
    require(name) { return name === "electron" ? electron : require(name); },
  }, { filename: file });
  return module.exports;
}

test("deep link subscription precedes pending pull, deduplicates and respects cleanup", async () => {
  const ipc = new EventEmitter();
  const pulls = [];
  const record = { id: "cold", target: { kind: "conversation", conversationId: "A" } };
  let pending = record;
  ipc.sendSync = () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" });
  ipc.invoke = (channel, id) => {
    if (channel === "minicode:deepLink:pending") {
      assert.equal(ipc.listenerCount("minicode:deep-link"), 1);
      return new Promise((resolve) => pulls.push(resolve));
    }
    assert.equal(channel, "minicode:deepLink:ack");
    const accepted = pending?.id === id;
    if (accepted) pending = null;
    return Promise.resolve(accepted);
  };
  let runtime;
  const code = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  vm.runInNewContext(code, {
    require: () => ({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld(_key, value) { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: process.platform, arch: process.arch },
    window: {}, console,
  });
  const deliveries = [];
  const cancelEarly = runtime.desktop.onDeepLink((value) => deliveries.push(value.id));
  cancelEarly();
  pulls[0](record);
  await Promise.resolve();
  assert.deepEqual(deliveries, []);
  const cancel = runtime.desktop.onDeepLink((value) => deliveries.push(value.id));
  ipc.emit("minicode:deep-link", {}, record);
  pulls[1](record);
  await Promise.resolve();
  assert.deepEqual(deliveries, ["cold"]);
  assert.equal(await runtime.desktop.ackDeepLink("wrong"), false);
  assert.equal(await runtime.desktop.ackDeepLink("cold"), true);
  assert.equal(await runtime.desktop.ackDeepLink("cold"), false);
  cancel();
  assert.equal(ipc.listenerCount("minicode:deep-link"), 0);
});

function browserFixture() {
  const children = [];
  let id = 1;
  class Contents extends EventEmitter {
    constructor() {
      super(); this.id = id++; this.url = ""; this.dead = false;
      this.session = { on() {}, setPermissionCheckHandler() {}, setPermissionRequestHandler() {}, webRequest: {} };
    }
    setWindowOpenHandler() {}
    getURL() { return this.url; }
    getTitle() { return "fixture"; }
    isLoading() { return false; }
    isDestroyed() { return this.dead; }
    async loadURL(url) { this.url = url; }
    close() { this.dead = true; }
  }
  class View {
    constructor() { this.webContents = new Contents(); }
    getBounds() { return { width: 10, height: 10 }; }
    setBounds() {}
    setVisible() {}
  }
  const browser = loadProduction("embedded-browser-manager.js", {
    WebContentsView: View,
    app: { getPath() { throw new Error("No fixture settings path"); } },
    dialog: {},
  });
  browser.init({
    getMainWindow: () => ({
      isDestroyed: () => false, webContents: { send() {} },
      getContentBounds: () => ({ width: 800, height: 600 }),
      contentView: {
        addChildView(view) { children.push(view); },
        removeChildView(view) { children.splice(children.indexOf(view), 1); },
      },
    }),
    lookupHostAddresses: async () => [{ address: "93.184.216.34", family: 4 }],
  });
  return { browser, children };
}

test("rejected browser settings leave download authorization unchanged", () => {
  const { browser } = browserFixture();
  for (const invalid of [
    { origin: "invalid" },
    { origin: "https://example.com", permission: "unknown" },
    { permission: "notifications" },
  ]) {
    assert.throws(() => browser.setBrowserSettings({ downloadPolicy: "allow", ...invalid }));
    assert.equal(browser.getBrowserSettings().downloadPolicy, "block");
  }
});

test("concurrent tab creation cannot overwrite another owner's live view", async () => {
  const { browser, children } = browserFixture();
  const outcomes = await Promise.allSettled(["A", "B"].map((owner) => browser.create({
    id: "same", url: "https://example.com", conversation_id: owner,
  })));
  assert.equal(outcomes[0].status, "fulfilled");
  assert.equal(outcomes[1].status, "rejected");
  assert.match(outcomes[1].reason.message, /another conversation/);
  assert.equal(children.length, 1);
  assert.equal(browser.closeConversation("A"), 1);
  assert.equal(children.length, 0);
  browser.disposeAll();
});

test("latest same-owner creation supersedes the old request and keeps one registered view", async () => {
  const { browser, children } = browserFixture();
  const outcomes = await Promise.allSettled([1, 2].map(() => browser.create({ id: "same", url: "https://example.com", conversation_id: "A" })));
  assert.equal(outcomes[0].status, "rejected");
  assert.match(outcomes[0].reason.message, /navigation was cancelled/);
  assert.equal(outcomes[1].status, "fulfilled");
  assert.equal(children.length, 1);
  assert.equal(browser.listTargets("A").length, 1);
  const contents = children[0].webContents;
  browser.disposeAll();
  assert.equal(children.length, 0);
  assert.equal(contents.isDestroyed(), true);
});

test("IPv6 discovery retains valid URL brackets", async () => {
  const cdp = require("./cdp-bridge");
  const original = global.fetch;
  const urls = [];
  global.fetch = async (url) => {
    urls.push(new URL(url).hostname);
    return { ok: true, json: async () => url.endsWith("version") ? { Browser: "fixture" } : [] };
  };
  try {
    const result = await cdp.discoverChromeCdp("http://[::1]:9222");
    assert.equal(result.status, "connected");
    assert.equal(result.endpoint, "http://[::1]:9222");
    assert.deepEqual(urls, ["[::1]", "[::1]"]);
  } finally { global.fetch = original; }
});

test("discovery does not send redirected requests outside its endpoint", async () => {
  const cdp = require("./cdp-bridge");
  let hits = 0;
  const sink = http.createServer((_request, response) => { hits++; response.end("{}"); });
  await new Promise((resolve) => sink.listen(0, "127.0.0.2", resolve));
  const source = http.createServer((request, response) => {
    response.writeHead(302, { Location: `http://127.0.0.2:${sink.address().port}${request.url}` });
    response.end();
  });
  await new Promise((resolve) => source.listen(0, "127.0.0.1", resolve));
  try {
    const result = await cdp.discoverChromeCdp(`http://127.0.0.1:${source.address().port}`);
    assert.equal(result.status, "error");
    assert.equal(hits, 0);
  } finally {
    await Promise.all([new Promise((resolve) => source.close(resolve)), new Promise((resolve) => sink.close(resolve))]);
  }
});

test("Page.navigate errorText rejects before metadata or screenshot", async () => {
  const cdp = require("./cdp-bridge");
  const originalFetch = global.fetch;
  const originalSocket = global.WebSocket;
  const calls = [];
  class Socket extends EventTarget {
    constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); }
    send(data) {
      const message = JSON.parse(data); calls.push(message.method);
      queueMicrotask(() => {
        const event = new Event("message");
        event.data = JSON.stringify({ id: message.id, result: message.method === "Page.navigate" ? { errorText: "net::ERR_NAME_NOT_RESOLVED" } : {} });
        this.dispatchEvent(event);
      });
    }
    close() { this.dispatchEvent(new Event("close")); }
  }
  global.WebSocket = Socket;
  global.fetch = async (url) => ({ ok: true, json: async () => url.endsWith("version") ? {} : [{ id: "target", type: "page", webSocketDebuggerUrl: "ws://127.0.0.1:9222/page" }] });
  try {
    await assert.rejects(cdp.navigateChromeTarget("http://127.0.0.1:9222", "target", "https://missing.invalid"), /ERR_NAME_NOT_RESOLVED/);
    assert.equal(calls.includes("Page.captureScreenshot"), false);
    assert.equal(calls.includes("Runtime.evaluate"), false);
  } finally { global.fetch = originalFetch; global.WebSocket = originalSocket; }
});

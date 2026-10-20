"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const { registerMcpAppScheme, createMcpAppSandbox, MCP_APP_PARTITION, MCP_APP_SCHEME } = require("./mcp-app-sandbox");

function fixture() {
  const calls = [];
  const sandboxSession = {
    protocol: { handle(scheme, handle) { this.scheme = scheme; this.handleRequest = handle; } },
    webRequest: { onBeforeRequest(handler) { this.checkRequest = handler; } },
    setPermissionRequestHandler(handler) { this.permission = handler; },
    setPermissionCheckHandler(handler) { this.checkPermission = handler; },
    async clearData() { calls.push("sandbox.clearData"); },
  };
  const session = { fromPartition(partition) { assert.equal(partition, MCP_APP_PARTITION); return sandboxSession; } };
  const sandbox = createMcpAppSandbox({ session });
  const owner = new EventEmitter(); owner.id = 71;
  sandbox.attachOwner(owner);
  const frameUrl = `http://mcp-app-${"a".repeat(48)}.localhost:8799/mcp-app/frame/token_123`;
  return { sandbox, sandboxSession, calls, owner, frameUrl };
}

test("registers one secure host protocol before ready", () => {
  let schemes;
  registerMcpAppScheme({ registerSchemesAsPrivileged(value) { schemes = value; } });
  assert.deepEqual(schemes, [{ scheme: MCP_APP_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
});

test("clears the persistent MCP session without clearing the renderer or browser session", async () => {
  const { sandbox, calls } = fixture();
  await sandbox.clearData();
  assert.deepEqual(calls, ["sandbox.clearData"]);
  const fresh = fixture();
  await fresh.sandbox.clearData();
  assert.deepEqual(fresh.calls, ["sandbox.clearData"]);
});

test("issues a bounded host document containing only the admitted MCP frame", async () => {
  const { sandbox, sandboxSession, frameUrl } = fixture();
  const surface = sandbox.prepare(71, frameUrl, "http://127.0.0.1:8799");
  const response = sandboxSession.protocol.handleRequest({ url: surface.src });
  const html = await response.text();
  assert.match(html, /sandbox="allow-scripts allow-same-origin"/);
  assert.ok(html.includes(`src="${frameUrl}"`));
  assert.equal(surface.partition, MCP_APP_PARTITION);
  assert.match(response.headers.get("Content-Security-Policy"), /^default-src 'none'; style-src 'unsafe-inline'; frame-src http:\/\/mcp-app-/);
  assert.equal(sandboxSession.protocol.handleRequest({ url: `${MCP_APP_SCHEME}://other/${surface.id}` }).status, 410);
});

test("does not accept non-frame URLs, another backend port, query injection or credentials", () => {
  const { sandbox, frameUrl } = fixture();
  for (const url of ["https://example.com/", frameUrl.replace(":8799", ":8000"), `${frameUrl}?x=1`,
    frameUrl.replace("http://", "http://u:p@"), frameUrl.replace("/mcp-app/frame/", "/api/")]) {
    assert.throws(() => sandbox.prepare(71, url, "http://127.0.0.1:8799"), /connected local backend/);
  }
});

test("binds every webview to its actual window and replaces untrusted preferences", () => {
  const { sandbox, owner, frameUrl } = fixture();
  const surface = sandbox.prepare(71, frameUrl, "http://127.0.0.1:8799");
  let prevented = false;
  const event = { preventDefault() { prevented = true; } };
  const preferences = { preload: "C:/untrusted.js", nodeIntegration: true, webviewTag: true };
  owner.emit("will-attach-webview", event, preferences, { src: surface.src, partition: surface.partition });
  assert.equal(prevented, false);
  assert.match(preferences.preload, /mcp-app-preload\.js$/);
  assert.equal(preferences.nodeIntegration, false);
  assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.sandbox, true);
  assert.equal(preferences.webviewTag, false);
  owner.emit("will-attach-webview", event, {}, { src: frameUrl, partition: surface.partition });
  assert.equal(prevented, true);
  prevented = false;
  const other = new EventEmitter(); other.id = 72; sandbox.attachOwner(other);
  other.emit("will-attach-webview", event, {}, { src: surface.src, partition: surface.partition });
  assert.equal(prevented, true);
});

test("releases only the owning window's surface and destroys all of its admissions on close", () => {
  const { sandbox, sandboxSession, owner, frameUrl } = fixture();
  const first = sandbox.prepare(71, frameUrl, "http://127.0.0.1:8799");
  sandbox.release(72, first.id);
  assert.equal(sandboxSession.protocol.handleRequest({ url: first.src }).status, 200);
  sandbox.release(71, first.id);
  assert.equal(sandboxSession.protocol.handleRequest({ url: first.src }).status, 410);
  const second = sandbox.prepare(71, frameUrl, "http://127.0.0.1:8799");
  owner.emit("destroyed");
  assert.equal(sandboxSession.protocol.handleRequest({ url: second.src }).status, 410);
});

test("blocks programmatic top-level URL replacement as well as file and custom-protocol requests", () => {
  const { sandbox, sandboxSession, owner, frameUrl } = fixture();
  const surface = sandbox.prepare(71, frameUrl, "http://127.0.0.1:8799");
  const guest = new EventEmitter(); guest.id = 411; guest.setWindowOpenHandler = () => {};
  owner.emit("did-attach-webview", {}, guest);
  const check = (url, resourceType = "mainFrame") => {
    let outcome;
    sandboxSession.webRequest.checkRequest({ url, resourceType, webContentsId: guest.id }, value => { outcome = value; });
    return outcome;
  };
  assert.deepEqual(check(surface.src), { cancel: false });
  assert.deepEqual(check("https://example.com/"), { cancel: true });
  assert.deepEqual(check("file:///C:/private.txt", "subFrame"), { cancel: true });
  assert.deepEqual(check("minicode://task/private", "subFrame"), { cancel: true });
  assert.deepEqual(check(frameUrl, "subFrame"), { cancel: false });
  guest.emit("destroyed");
  assert.deepEqual(check(surface.src), { cancel: true });
});

test("preload relays only the child frame and keeps host-to-app messages inside that frame", () => {
  const received = [], sent = [];
  const frame = { src: "http://mcp-app-real.localhost:8799/mcp-app/frame/token", contentWindow: { postMessage(...args) { sent.push(args); } } };
  const events = {};
  let receive;
  vm.runInNewContext(readFileSync(require.resolve("./mcp-app-preload"), "utf8"), {
    require: () => ({ ipcRenderer: { sendToHost(...args) { received.push(args); }, on(_channel, handler) { receive = handler; } } }),
    document: { querySelector: () => frame }, window: { addEventListener(name, handler) { events[name] = handler; } }, URL,
  });
  const message = { jsonrpc: "2.0", method: "ui/initialize", id: 1 };
  events.message({ source: {}, origin: "http://mcp-app-real.localhost:8799", data: message });
  events.message({ source: frame.contentWindow, origin: "https://wrong.test", data: message });
  assert.equal(received.length, 0);
  events.message({ source: frame.contentWindow, origin: "http://mcp-app-real.localhost:8799", data: message });
  assert.deepEqual(received, [["minicode:mcpApp:message", message]]);
  receive({}, message);
  assert.deepEqual(sent, [[message, "http://mcp-app-real.localhost:8799"]]);
});

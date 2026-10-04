"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const http = require("node:http");
const { EventEmitter } = require("node:events");
const cdp = require("./cdp-bridge");
const bridge = require("./embedded-browser-bridge");

function loadModule(name, overrides = {}) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, name), "utf8"), {
    module, Buffer, URL, AbortController, Event, EventTarget, setTimeout, clearTimeout, console, process, queueMicrotask,
    require(request) {
      if (Object.hasOwn(overrides, request)) return overrides[request];
      return request.startsWith(".") ? require(path.resolve(__dirname, request)) : require(request);
    },
    ...overrides.globals,
  });
  return module.exports;
}

test("CDP IP rules keep public domains public and actual private IPv6 private", () => {
  for (const host of ["fda.gov", "ffmpeg.org", "fc2.com", "fe80.example"]) {
    assert.equal(cdp.assessBrowserNavigationPolicy(`https://${host}`).requiresPrivateNetworkApproval, false);
  }
  for (const host of ["[::1]", "[fd00::1]", "[fc00::1]", "[fe80::1]", "[::ffff:127.0.0.1]"]) {
    assert.equal(cdp.assessBrowserNavigationPolicy(`http://${host}`).requiresPrivateNetworkApproval, true);
  }
});

test("a closed CDP transport rejects subsequent requests before sending or scheduling their deadlines", async () => {
  let closedSends = 0;
  class Socket extends EventTarget {
    static OPEN = 1;
    constructor() {
      super(); this.readyState = 0;
      queueMicrotask(() => { this.readyState = 1; this.dispatchEvent(new Event("open")); });
    }
    send(raw) {
      if (this.readyState !== 1) { closedSends++; return; }
      const request = JSON.parse(raw);
      queueMicrotask(() => this.dispatchEvent(Object.assign(new Event("message"), { data: JSON.stringify({ id: request.id, result: {} }) })));
      if (request.method === "Page.bringToFront") queueMicrotask(() => this.close());
    }
    close() { if (this.readyState !== 3) { this.readyState = 3; this.dispatchEvent(new Event("close")); } }
  }
  const module = loadModule("cdp-bridge.js", { globals: { WebSocket: Socket } });
  await assert.rejects(module.captureScreenshotViaCdp("ws://127.0.0.1:9222/devtools/page-fixture"), /connection is not open/);
  assert.equal(closedSends, 0);
});

test("concurrent HTTP bridge start publishes one endpoint and serialization errors stay complete responses", async () => {
  bridge.init({ token: "fixture-token", manager: { async executeControlCommand() { return { ok: true, value: 1n }; } } });
  const endpoints = await Promise.all([bridge.start(), bridge.start()]);
  assert.ok(endpoints[0]); assert.equal(endpoints[0], endpoints[1]);
  try {
    const response = await fetch(`${endpoints[0]}/v1/command`, { method: "POST", headers: { authorization: "Bearer fixture-token" }, body: "{}" });
    assert.equal(response.status, 400);
    assert.equal((await response.json()).ok, false);
  } finally { await bridge.stop(); }
});

test("bridge retries a fresh server after a listen failure and closes startup interrupted by Stop", async () => {
  let creations = 0;
  let resolveListen;
  const servers = [];
  const fakeHttp = { createServer() {
    const server = new EventEmitter(); const attempt = ++creations;
    server.listening = false;
    server.listen = (_port, _host, callback) => queueMicrotask(() => {
      if (attempt === 1) server.emit("error", new Error("fixture listen failed"));
      else resolveListen = () => { server.listening = true; callback(); };
    });
    server.address = () => ({ port: 32123 });
    server.close = (callback) => { server.listening = false; queueMicrotask(callback); };
    server.closeAllConnections = () => {};
    servers.push(server); return server;
  } };
  const module = loadModule("embedded-browser-bridge.js", { "node:http": fakeHttp });
  await assert.rejects(module.start(), /fixture listen failed/);
  const starting = module.start();
  await new Promise((resolve) => queueMicrotask(resolve));
  const stopping = module.stop();
  resolveListen();
  await assert.rejects(starting, /stopping/);
  await stopping;
  assert.equal(servers.every((server) => !server.listening), true);
});

test("invalid UTF-8 HTTP command bodies never reach the browser manager", async () => {
  let calls = 0;
  bridge.init({ token: "fixture-token", manager: { async executeControlCommand() { calls++; return { ok: true }; } } });
  const endpoint = new URL(await bridge.start());
  try {
    const status = await new Promise((resolve, reject) => {
      const request = http.request({ hostname: endpoint.hostname, port: endpoint.port, path: "/v1/command", method: "POST", headers: { authorization: "Bearer fixture-token", "content-type": "application/json" } }, (response) => { response.resume(); response.on("end", () => resolve(response.statusCode)); });
      request.on("error", reject);
      request.end(Buffer.from([123, 34, 109, 97, 114, 107, 101, 114, 34, 58, 34, 255, 34, 125]));
    });
    assert.equal(status, 400); assert.equal(calls, 0);
  } finally { await bridge.stop(); }
});

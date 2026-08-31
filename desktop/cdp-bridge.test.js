"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  assessBrowserNavigationPolicy,
  navigateChromeTarget,
  validateWebSocketDebuggerUrl,
} = require("./cdp-bridge");

test("browser navigation policy marks public targets as safe to navigate", () => {
  assert.deepEqual(assessBrowserNavigationPolicy("example.com/docs"), {
    url: "http://example.com/docs",
    host: "example.com",
    risk: "public",
    requiresPrivateNetworkApproval: false,
  });
});

test("browser navigation policy requires approval for local and private targets", () => {
  assert.deepEqual(assessBrowserNavigationPolicy("http://127.0.0.1:5173"), {
    url: "http://127.0.0.1:5173",
    host: "127.0.0.1",
    risk: "private_or_local",
    requiresPrivateNetworkApproval: true,
  });
  assert.equal(
    assessBrowserNavigationPolicy("http://192.168.1.20").requiresPrivateNetworkApproval,
    true,
  );
  assert.equal(
    assessBrowserNavigationPolicy("http://100.64.0.1").requiresPrivateNetworkApproval,
    true,
  );
  assert.equal(
    assessBrowserNavigationPolicy("http://[::ffff:127.0.0.1]").requiresPrivateNetworkApproval,
    true,
  );
  assert.throws(
    () => assessBrowserNavigationPolicy("https://user:secret@example.com"),
    /must not contain credentials/,
  );
});

test("navigateChromeTarget rejects private targets unless the main process grants approval", async () => {
  await assert.rejects(
    () => navigateChromeTarget("http://127.0.0.1:9222", "target-id", "http://localhost:3000"),
    /requires approval/,
  );
});

test("CDP discovery accepts only same-host loopback websocket targets", () => {
  const endpoint = "http://127.0.0.1:9222";
  assert.equal(
    validateWebSocketDebuggerUrl("ws://127.0.0.1:9222/devtools/page-1", endpoint),
    "ws://127.0.0.1:9222/devtools/page-1",
  );
  assert.equal(validateWebSocketDebuggerUrl("ws://169.254.169.254:9222/devtools/page-1", endpoint), "");
  assert.equal(validateWebSocketDebuggerUrl("ws://127.0.0.1:9999/devtools/page-1", endpoint), "");
  assert.equal(validateWebSocketDebuggerUrl("http://127.0.0.1:9222/devtools/page-1", endpoint), "");
  assert.equal(validateWebSocketDebuggerUrl("ws://localhost:9222/devtools/page-1", endpoint), "");
});

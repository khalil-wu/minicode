"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { externalBackendHostContract, bridgeBindTarget, verifyExternalBackendHostContract } = require("./backend-host-contract");

test("external host startup requires an explicit private loopback bridge contract", () => {
  assert.throws(() => externalBackendHostContract({}), /same host-private.*ENDPOINT.*TOKEN/);
  assert.throws(() => externalBackendHostContract({ MINICODE_EMBEDDED_BROWSER_ENDPOINT: "http://127.0.0.1:9347" }), /same host-private/);
  for (const endpoint of ["invalid", "http://127.0.0.1:0", "https://127.0.0.1:9347", "http://example.com:9347", "http://127.0.0.1:9347/private", "http://token@127.0.0.1:9347"]) {
    assert.throws(() => bridgeBindTarget(endpoint), /loopback HTTP/);
  }
  assert.deepEqual(bridgeBindTarget("http://LOCALHOST:9347/"), { host: "localhost", port: 9347, endpoint: "http://localhost:9347" });
  assert.deepEqual(bridgeBindTarget("http://[::1]:9347/"), { host: "::1", port: 9347, endpoint: "http://[::1]:9347" });
  const raw = externalBackendHostContract({ MINICODE_EMBEDDED_BROWSER_ENDPOINT:"http://127.0.0.1:9347", MINICODE_EMBEDDED_BROWSER_TOKEN:" private-peer-token " });
  assert.equal(raw.token, " private-peer-token ");
});

test("main verifies the external producer's canonical private environment without exposing it", async () => {
  const contract = externalBackendHostContract({ MINICODE_EMBEDDED_BROWSER_ENDPOINT: "http://LOCALHOST:9347/", MINICODE_EMBEDDED_BROWSER_TOKEN: "private-peer-token" });
  assert.match(contract.fingerprint, /^[0-9a-f]{64}$/);
  let request;
  await verifyExternalBackendHostContract("http://127.0.0.1:8000/", "renderer-runtime-token", contract, async (url, options) => {
    request = { url, options }; return { ok: true, json: async () => ({ ready: true, desktop_bridge_fingerprint: contract.fingerprint }) };
  });
  assert.equal(request.url, "http://127.0.0.1:8000/readyz");
  assert.equal(request.options.headers["x-minicode-token"], "renderer-runtime-token");
  assert.equal(JSON.stringify(request).includes("private-peer-token"), false);
  await assert.rejects(verifyExternalBackendHostContract("http://127.0.0.1:8000", "runtime", contract,
    async () => ({ ok: true, json: async () => ({ ready: true }) })), /does not match.*host-private/);
  await assert.rejects(verifyExternalBackendHostContract("http://127.0.0.1:8000", "runtime", contract,
    async () => ({ ok:true, json:async () => ({ready:false, desktop_bridge_fingerprint:contract.fingerprint}) })), /does not report ready/);
});

test("a mismatched external backend reaches the existing startup recovery and retry rechecks the actual peer", async () => {
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8")
    .match(/async function attemptAppStartup\(source = "startup"\) \{[\s\S]*?\n\}/)[0];
  const calls = [];
  const env = { MINICODE_EMBEDDED_BROWSER_ENDPOINT:"http://127.0.0.1:9347", MINICODE_EMBEDDED_BROWSER_TOKEN:"private-peer-token" };
  const contract = externalBackendHostContract(env);
  let fingerprint = "another-native-host";
  const context = { startupRetryInFlight:false, rollbackPreparationInProgress:false, desktopCleanupFailed:false, MANAGE_BACKEND:false, RUNTIME_TOKEN:"runtime-token", EMBEDDED_BROWSER_TOKEN:contract.token,
    petManager: null, petInitialization: null, generalMaintenance: null, path,
    popoutWindow: { initialize() { calls.push("popout"); } },
    app: { isPackaged: false, getPath: () => "fixture-home" }, getAppRoot: () => "fixture-app",
    createPetManager: () => ({ initialize: async () => {}, restoreMini: async () => {} }),
    desktopPreferences: { getPreferences: () => ({}) }, startGeneralMaintenance: () => ({ dispose() {} }),
    BACKEND_STARTUP_TIMEOUT_MS:5000, process:{env}, appendDesktopLog() {},
    externalBackendHostContract:() => externalBackendHostContract(env),
    backendSidecar:{resetStopRequested() {}, async waitForBackendReady() { calls.push("ready"); }},
    embeddedBrowserBridge:{async start() { calls.push("bridge"); return contract.endpoint; }},
    resolveBackendRuntime:async () => ({apiBaseUrl:"http://127.0.0.1:8000"}),
    verifyExternalBackendHostContract:async (...args) => {
      calls.push("peer-proof"); await verifyExternalBackendHostContract(...args,
        async () => ({ok:true, json:async () => ({ready:true, desktop_bridge_fingerprint:fingerprint})}));
    },
    windowManager:{async createMainWindow() { calls.push("window"); }},
    serializeError:error => ({message:error.message}),
    createStartupFailureWindow:async error => { assert.match(error.message, /does not match.*host-private/); calls.push("error-surface"); },
    closeStartupFailureWindow:() => calls.push("close-error"),
  };
  const startup = vm.runInNewContext(`${source}\nattemptAppStartup;`, context);
  assert.equal(await startup(), false);
  assert.deepEqual(calls, ["bridge", "ready", "peer-proof", "error-surface"]);
  fingerprint = contract.fingerprint;
  assert.equal(await startup("startup-retry"), true);
  assert.deepEqual(calls.slice(4), ["bridge", "ready", "peer-proof", "window", "popout", "close-error"]);
});

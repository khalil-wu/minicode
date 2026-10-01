"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

test("real Electron cold argv, late subscription, ack and responsive cleanup", { timeout: 60000 }, async () => {
  let receive;
  const stages = [];
  const reported = new Promise((resolve) => { receive = resolve; });
  const html = `<!doctype html><title>MiniCode cold-link oracle</title><p>Cold startup verification</p><script>
  (async () => {
    const desktop = window.__MINICODE_RUNTIME__.desktop;
    const stage = value => fetch('/oracle-stage', { method:'POST', body:value });
    window.addEventListener('unhandledrejection', event => fetch('/oracle-result', { method:'POST', body:JSON.stringify({ error:String(event.reason), stack:event.reason?.stack }) }));
    await stage('renderer-ready');
    const deliveries = [];
    let staleDeliveries = 0;
    const earlyCleanup = desktop.onDeepLink(() => staleDeliveries++);
    earlyCleanup();
    await stage('early-subscription-cleaned');
    await new Promise(resolve => setTimeout(resolve, 1000));
    let finishCold;
    let finishWarm;
    const cold = new Promise(resolve => { finishCold = resolve; });
    const warm = new Promise(resolve => { finishWarm = resolve; });
    const cleanup = desktop.onDeepLink(async payload => {
      await stage('delivery:' + JSON.stringify(payload));
      deliveries.push(payload);
      const firstAck = await desktop.ackDeepLink(payload.id);
      const secondAck = await desktop.ackDeepLink(payload.id);
      await stage('acks:' + firstAck + ',' + secondAck);
      if (!firstAck || secondAck) throw new Error('ack ownership failed');
      (payload.target.conversationId === 'cold-oracle' ? finishCold : finishWarm)();
    });
    await cold;
    await stage('cold-complete');
    const opened = await desktop.openDeepLink('minicode://conversation/warm-oracle');
    if (!opened) throw new Error('Warm deep-link dispatch was rejected');
    await warm;
    await stage('warm-complete');
    cleanup();
    const noPending = [];
    const cleanupAgain = desktop.onDeepLink(payload => noPending.push(payload));
    await new Promise(resolve => setTimeout(resolve, 100));
    cleanupAgain();
    const sameOwner = await Promise.allSettled([1,2].map(() => desktop.embeddedBrowser.create({ id: 'oracle-tab', url: 'about:blank', conversation_id: 'oracle-owner' })));
    if (!sameOwner.some(result => result.status === 'fulfilled')) throw new Error('No concurrent create completed');
    for (const result of sameOwner) {
      if (result.status === 'rejected' && !String(result.reason).includes('ERR_ABORTED')) throw result.reason;
    }
    const tabsBefore = await desktop.embeddedBrowser.list({ conversation_id: 'oracle-owner' });
    const closed = await desktop.embeddedBrowser.closeConversation('oracle-owner');
    const tabsAfter = await desktop.embeddedBrowser.list({ conversation_id: 'oracle-owner' });
    let frames = 0;
    await new Promise(resolve => requestAnimationFrame(() => { frames++; resolve(); }));
    const result = { deliveries, staleDeliveries, noPending, sameOwner: sameOwner.map(result => ({ status:result.status, error:result.status === 'rejected' ? String(result.reason) : undefined })), tabsBefore: tabsBefore.length, closed, tabsAfter: tabsAfter.length, frames };
    await fetch('/oracle-result', { method:'POST', body:JSON.stringify(result) });
    await desktop.windowControls.close();
  })().catch(async error => {
    await fetch('/oracle-result', { method:'POST', body:JSON.stringify({ error:String(error), stack:error.stack }) });
    await window.__MINICODE_RUNTIME__.desktop.windowControls.close();
  });</script>`;
  const server = http.createServer((request, response) => {
    if (request.url === "/oracle-stage") {
      let body = "";
      request.on("data", chunk => { body += chunk; });
      request.on("end", () => { stages.push(body); response.end("ok"); });
    } else if (request.url === "/oracle-result") {
      let body = "";
      request.on("data", (chunk) => { body += chunk; });
      request.on("end", () => { receive(JSON.parse(body)); response.end("ok"); });
    } else if (request.url === "/readyz") {
      response.end("ready");
    } else {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(html);
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-cold-link-oracle-"));
  const env = { ...process.env, MINICODE_USER_DATA_DIR: userData, MINICODE_SKIP_BACKEND: "1", MINICODE_API_BASE_URL: origin, MINICODE_WS_BASE_URL: origin.replace("http:", "ws:"), MINICODE_FRONTEND_URL: origin, MINICODE_BACKEND_STARTUP_TIMEOUT_MS: "10000" };
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(require("electron"), [__dirname, "minicode://conversation/cold-oracle"], { env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (data) => { output += data; });
  child.stderr.on("data", (data) => { output += data; });
  const exited = new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", (code, signal) => resolve({ code, signal })); });
  let timer;
  const deadline = new Promise((_resolve, reject) => { timer = setTimeout(() => reject(new Error(`Electron oracle timed out: ${JSON.stringify(stages)} ${output}`)), 45000); });
  try {
    const result = await Promise.race([reported, exited.then((exit) => { throw new Error(`Electron exited before oracle: ${JSON.stringify(exit)} ${output}`); }), deadline]);
    assert.equal(result.error, undefined, JSON.stringify(result));
    assert.deepEqual(result.deliveries.map((item) => item.target.conversationId), ["cold-oracle", "warm-oracle"]);
    assert.notEqual(result.deliveries[0].id, result.deliveries[1].id);
    assert.equal(result.staleDeliveries, 0);
    assert.deepEqual(result.noPending, []);
    assert.equal(result.tabsBefore, 1);
    assert.equal(result.closed, 1);
    assert.equal(result.tabsAfter, 0);
    assert.equal(result.frames, 1);
    const exit = await Promise.race([exited, deadline]);
    assert.equal(exit.code, 0, output);
    console.log(JSON.stringify({ electronColdArgvReceipt: result, stages, exit, userData }));
  } finally {
    clearTimeout(timer);
    if (child.exitCode === null) {
      if (process.platform === "win32") {
        await new Promise((resolve) => spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" }).once("close", resolve));
      } else { child.kill("SIGKILL"); }
    }
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

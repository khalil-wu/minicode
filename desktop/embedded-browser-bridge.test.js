"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const net = require("node:net");
const bridge = require("./embedded-browser-bridge");

test("embedded browser bridge requires its token and forwards commands", async () => {
  const calls = [];
  bridge.init({
    token: "bridge-test-token",
    manager: { async executeControlCommand(payload) { calls.push(payload); return { ok: true, action: payload.action, targets: [] }; } },
  });
  const endpoint = await bridge.start();
  try {
    const denied = await fetch(`${endpoint}/v1/command`, { method: "POST", body: "{}" });
    assert.equal(denied.status, 401);
    const accepted = await fetch(`${endpoint}/v1/command`, {
      method: "POST",
      headers: { authorization: "Bearer bridge-test-token", "content-type": "application/json" },
      body: JSON.stringify({ action: "list_targets" }),
    });
    assert.equal(accepted.status, 200);
    assert.deepEqual(calls, [{ action: "list_targets" }]);
  } finally {
    await bridge.stop();
  }
});

test("stop closes an authenticated request with an unfinished body", async () => {
  let calls = 0;
  bridge.init({
    token: "bridge-test-token",
    manager: { async executeControlCommand() { calls += 1; return { ok: true }; } },
  });
  const endpoint = new URL(await bridge.start());
  const socket = net.createConnection({ host: endpoint.hostname, port: Number(endpoint.port) });
  try {
    await new Promise((resolve) => socket.once("connect", resolve));
    socket.write(
      "POST /v1/command HTTP/1.1\r\nHost: 127.0.0.1\r\n"
      + "Authorization: Bearer bridge-test-token\r\nContent-Length: 1000\r\n\r\n{",
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
    await bridge.stop();
    assert.equal(calls, 0, "an incomplete body is never admitted as a command");
  } finally {
    socket.destroy();
    await bridge.stop();
  }
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function command(endpoint, payload, path = "command") {
  return fetch(`${endpoint}/v1/${path}`, {
    method: "POST", headers: { authorization: "Bearer bridge-test-token", "content-type": "application/json" },
    body: JSON.stringify(payload),
  }).then((response) => response.json());
}

test("cancelled operation ids cannot start later or be reused under another owner", async () => {
  let calls = 0;
  bridge.init({ token: "bridge-test-token", manager: { async executeControlCommand() { calls++; return { ok: true }; } } });
  const endpoint = await bridge.start();
  const owner = { operation_id: "cancel-before-admission", conversation_id: "owner" };
  try {
    const cancelled = await command(endpoint, { ...owner, action: "cancel" }, "operation");
    assert.equal(cancelled.cleanup_receipt.completed, true);
    const refused = await command(endpoint, { ...owner, action: "navigate" });
    assert.equal(refused.ok, false);
    const otherOwner = await command(endpoint, { ...owner, conversation_id: "other", action: "status" }, "operation");
    assert.equal(otherOwner.ok, false);
    assert.equal(calls, 0);
  } finally { await bridge.stop(); }
});

for (const submitted of [false, true]) test(`cancel retains ${submitted ? "submitted" : "not yet submitted"} operation until its actual outcome`, async () => {
  const admitted = deferred(), finish = deferred();
  let effects = 0, calls = 0;
  bridge.init({ token: "bridge-test-token", manager: {
    async executeControlCommand(_payload, control) {
      calls++;
      if (submitted) control.onSubmitted();
      admitted.resolve();
      await finish.promise;
      // Submitted page JavaScript cannot be undone by AbortSignal. Before
      // submission, the real manager checks the signal and prevents the action.
      if (!submitted) { control.signal.throwIfAborted(); control.onSubmitted(); }
      effects++;
      return { ok: true };
    },
  } });
  const endpoint = await bridge.start();
  const owner = { operation_id: `operation-${submitted}`, conversation_id: "owner" };
  try {
    const original = command(endpoint, { ...owner, action: "evaluate" });
    await admitted.promise;
    const duplicate = await command(endpoint, { ...owner, action: "navigate" });
    assert.equal(duplicate.ok, false);
    assert.equal(duplicate.cleanup_receipt.pending, 1);
    assert.equal(calls, 1);
    const cancelled = await command(endpoint, { ...owner, action: "cancel" }, "operation");
    assert.equal(cancelled.cleanup_receipt.pending, 1);
    assert.equal(cancelled.cleanup_receipt.completed, false);
    assert.equal(cancelled.cleanup_receipt.retry_safe, false);
    assert.equal((await original).status, "cancelled");
    finish.resolve();
    const outcome = await command(endpoint, { ...owner, action: "wait" }, "operation");
    assert.equal(outcome.cleanup_receipt.pending, 0);
    assert.equal(outcome.cleanup_receipt.execution_outcome, submitted ? "completed" : "cancelled");
    assert.equal(effects, Number(submitted));
    assert.equal((await command(endpoint, { ...owner, action: "evaluate" })).ok, false);
    assert.equal(calls, 1);
  } finally { finish.resolve(); await bridge.stop(); }
});

test("bridge operation deadline returns an uncertain receipt while submitted work remains live", async () => {
  const finish = deferred();
  bridge.init({ token: "bridge-test-token", manager: {
    async executeControlCommand(_payload, control) {
      control.onSubmitted();
      await finish.promise;
      return { ok: true };
    },
  } });
  const endpoint = await bridge.start();
  const owner = { operation_id: "timeout-operation", conversation_id: "owner" };
  try {
    const result = await command(endpoint, { ...owner, action: "evaluate", operation_timeout_ms: 10 });
    assert.equal(result.status, "timeout");
    assert.equal(result.cleanup_receipt.pending, 1);
    assert.equal(result.cleanup_receipt.submitted, true);
    finish.resolve();
    const observed = await command(endpoint, { ...owner, action: "wait" }, "operation");
    assert.equal(observed.cleanup_receipt.pending, 0);
  } finally { finish.resolve(); await bridge.stop(); }
});

test("stopping bridge closes receipt waits without claiming submitted work has stopped", async () => {
  const admitted = deferred(), finish = deferred();
  bridge.init({ token: "bridge-test-token", manager: {
    async executeControlCommand(_payload, control) {
      control.onSubmitted(); admitted.resolve(); await finish.promise;
      return { ok: true };
    },
  } });
  const endpoint = await bridge.start();
  const owner = { operation_id: "stop-operation", conversation_id: "owner" };
  try {
    const original = command(endpoint, { ...owner, action: "evaluate" }).catch(() => null);
    await admitted.promise;
    const wait = command(endpoint, { ...owner, action: "wait" }, "operation").catch(() => null);
    await new Promise((resolve) => setTimeout(resolve, 20));
    await bridge.stop();
    await Promise.all([original, wait]);
  } finally { finish.resolve(); await bridge.stop(); }
});

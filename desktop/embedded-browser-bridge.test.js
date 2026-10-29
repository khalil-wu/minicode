"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const net = require("node:net");
const bridge = require("./embedded-browser-bridge");
bridge.init({ getWindowRecordByInstanceId: (instance) => instance === "fixture-window" ? {
  windowInstanceId: instance, windowOwnerId: "primary", windowBindingToken: "fixture-binding", window: { isDestroyed: () => false },
} : null });

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
      body: JSON.stringify({ action: "list_targets", conversation_id: "fixture-owner", window_instance_id: "fixture-window", window_binding_token: "fixture-binding" }),
    });
    assert.equal(accepted.status, 200);
    assert.deepEqual(calls, [{ action: "list_targets", conversation_id: "fixture-owner", window_instance_id: "fixture-window", window_binding_token: "fixture-binding" }]);
  } finally {
    await bridge.stop();
  }
});

test("authenticated health reports the actual browser control preference without admitting a page action", async () => {
  let enabled = false;
  bridge.init({ token: "bridge-test-token", manager: {
    getBrowserSettings() { return { browserEnabled: enabled }; },
    async executeControlCommand() { assert.fail("health must not execute a page action"); },
  } });
  const endpoint = await bridge.start();
  try {
    assert.equal((await fetch(`${endpoint}/v1/health`)).status, 401);
    const options = { headers: { authorization: "Bearer bridge-test-token" } };
    assert.equal((await (await fetch(`${endpoint}/v1/health`, options)).json()).browserEnabled, false);
    enabled = true;
    assert.equal((await (await fetch(`${endpoint}/v1/health`, options)).json()).browserEnabled, true);
  } finally { await bridge.stop(); }
});

test("an Agent payload cannot claim the trusted manual navigation control option", async () => {
  const controls = [];
  bridge.init({ token: "manual-spoof-test", manager: {
    async executeControlCommand(_payload, control) { controls.push(control); return { ok: true }; },
  } });
  const endpoint = await bridge.start();
  try {
    const response = await fetch(`${endpoint}/v1/command`, {
      method: "POST", headers: { authorization: "Bearer manual-spoof-test" },
      body: JSON.stringify({ action: "navigate", url: "https://example.com/", conversation_id: "actual-owner",
        window_instance_id: "fixture-window", window_binding_token: "fixture-binding", operation_id: "spoof-manual",
        agent_control_id: "original-agent", agent_turn_id: "original-turn", manualNavigation: true,
        control: { manualNavigation: true }, source: "manual" }),
    });
    assert.equal(response.status, 200); assert.equal(controls.length, 1);
    assert.equal(controls[0].manualNavigation, undefined);
    assert.deepEqual(controls[0].agentControl, { id: "original-agent", turnId: "original-turn" });
  } finally { await bridge.stop(); }
});

test("only the original token and window binding can release the captured agent browser control", async () => {
  const controls = [], releases = [];
  bridge.init({ token: "browser-owner-token", manager: {
    async executeControlCommand(_payload, control) { controls.push(control.agentControl); return { ok: true }; },
    releaseAgentControl(owner) { releases.push(owner); return 1; },
  } });
  const endpoint = await bridge.start();
  const owner = { conversation_id: "original-conversation", window_instance_id: "fixture-window", window_binding_token: "fixture-binding",
    agent_control_id: "captured-resource", agent_turn_id: "original-turn" };
  const post = (route, payload, token = "browser-owner-token") => fetch(`${endpoint}/v1/${route}`, {
    method: "POST", headers: { authorization: `Bearer ${token}` }, body: JSON.stringify(payload),
  });
  try {
    assert.equal((await post("command", { ...owner, action: "get_url" })).status, 200);
    assert.deepEqual(controls, [{ id: "captured-resource", turnId: "original-turn" }]);
    assert.equal((await post("session-control", { ...owner, action: "release" }, "renderer-token")).status, 401);
    assert.equal((await post("session-control", { ...owner, action: "release", window_binding_token: "other-window" })).status, 403);
    assert.equal(releases.length, 0);
    const released = await post("session-control", { ...owner, action: "release" });
    assert.equal(released.status, 200); assert.deepEqual(await released.json(), { ok: true, released: 1 });
    assert.deepEqual(releases, [{ conversation_id: "original-conversation", window_instance_id: "fixture-window",
      control: { id: "captured-resource", turnId: "original-turn" } }]);
  } finally { await bridge.stop(); }
});

test("agent control release waits for the original native operation and keeps its cleanup authority after window retirement", async () => {
  let alive = true;
  const started = deferred(), finished = deferred(), releases = [];
  const record = { windowInstanceId: "fixture-window", windowOwnerId: "primary", windowBindingToken: "fixture-binding", window: { isDestroyed: () => !alive } };
  bridge.init({ token: "browser-retirement-token", getWindowRecordByInstanceId: instance => alive && instance === "fixture-window" ? record : null,
    manager: { async executeControlCommand() { started.resolve(); return finished.promise; },
      releaseAgentControl(owner) { releases.push(owner); return 0; } } });
  const endpoint = await bridge.start();
  const owner = { operation_id: "native-original-operation", conversation_id: "original-owner", window_instance_id: "fixture-window", window_binding_token: "fixture-binding",
    agent_control_id: "original-browser-control", agent_turn_id: "original-turn" };
  const post = (route, payload) => fetch(`${endpoint}/v1/${route}`, { method: "POST", headers: { authorization: "Bearer browser-retirement-token" }, body: JSON.stringify(payload) });
  try {
    const operation = post("command", { ...owner, action: "evaluate", expression: "original native work" });
    await started.promise;
    alive = false;
    const releasing = post("session-control", { ...owner, action: "release" });
    await new Promise(setImmediate);
    assert.equal(releases.length, 0);
    finished.resolve({ ok: true });
    assert.equal((await operation).status, 200);
    const response = await releasing;
    assert.equal(response.status, 200); assert.equal(releases.length, 1);
    assert.equal(releases[0].control.id, "original-browser-control");
  } finally {
    finished.resolve({ ok: true }); await bridge.stop();
    bridge.init({ getWindowRecordByInstanceId: instance => instance === "fixture-window" ? { ...record, window: { isDestroyed: () => false } } : null });
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

test("remote bridge secrets are scoped to their actual host and never authorize another host's window", async () => {
  const calls = [];
  const remoteRecord = { remoteHostId: "host-a", windowInstanceId: "remote-window", windowOwnerId: "remote-primary",
    windowBindingToken: "remote-binding", window: { isDestroyed: () => false } };
  bridge.init({ token: "local-private", getWindowRecordByInstanceId: id => id === "remote-window" ? remoteRecord : null,
    manager: { async executeControlCommand(payload) { calls.push(payload); return { ok: true }; } } });
  const first = { ready: { bridge_token: "host-a-private" } }, second = { ready: { bridge_token: "host-b-private" } };
  bridge.registerRemoteHost("host-a", first);
  bridge.registerRemoteHost("host-b", second);
  const endpoint = await bridge.start();
  const request = token => fetch(`${endpoint}/v1/command`, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ action: "list_targets", window_instance_id: "remote-window", window_binding_token: "remote-binding", conversation_id: "remote-conversation" }) });
  try {
    assert.equal((await request("local-private")).status, 403);
    assert.equal((await request("host-b-private")).status, 403);
    assert.equal((await request("host-a-private")).status, 200);
    assert.equal(calls.length, 1);
  } finally {
    bridge.unregisterRemoteHost("host-a", first);
    bridge.unregisterRemoteHost("host-b", second);
    await bridge.stop();
    bridge.init({ token: "bridge-test-token", getWindowRecordByInstanceId: instance => instance === "fixture-window" ? {
      windowInstanceId: instance, windowOwnerId: "primary", windowBindingToken: "fixture-binding", window: { isDestroyed: () => false },
    } : null });
  }
});

function command(endpoint, payload, path = "command") {
  return fetch(`${endpoint}/v1/${path}`, {
    method: "POST", headers: { authorization: "Bearer bridge-test-token", "content-type": "application/json" },
    body: JSON.stringify({ window_instance_id: "fixture-window", window_binding_token: "fixture-binding", ...payload }),
  }).then((response) => response.json());
}

test("window bindings reject another instance's capability and expire new resource authority on close", async () => {
  const records = new Map([ ["window-a", { windowInstanceId: "window-a", windowOwnerId: "primary", windowBindingToken: "token-a", window: { isDestroyed: () => false } }],
    ["window-b", { windowInstanceId: "window-b", windowOwnerId: "secondary-1", windowBindingToken: "token-b", window: { isDestroyed: () => false } }] ]);
  let effects = 0;
  bridge.init({ token: "bridge-test-token", getWindowRecordByInstanceId: (instance) => records.get(instance),
    manager: { async executeControlCommand() { effects++; return { ok: true }; } } });
  const endpoint = await bridge.start();
  const send = (payload, route = "command", authorization = "bridge-test-token") => fetch(`${endpoint}/v1/${route}`, {
    method: "POST", headers: { authorization: `Bearer ${authorization}` }, body: JSON.stringify(payload),
  });
  const b = { window_instance_id: "window-b", window_owner_id: "secondary-1", window_binding_token: "token-b" };
  try {
    const valid = await send(b, "window-binding");
    assert.equal(valid.status, 200);
    assert.deepEqual(await valid.json(), { ok: true, window_instance_id: "window-b", window_owner_id: "secondary-1" });
    assert.equal((await send(b, "window-binding", "shared-renderer-runtime-token")).status, 401);
    assert.equal((await send({ ...b, window_binding_token: "token-a" }, "window-binding")).status, 403);
    assert.equal((await send({ ...b, window_owner_id: "primary" }, "window-binding")).status, 403);
    assert.equal((await send({ ...b, window_binding_token: "token-a", action: "evaluate", conversation_id: "owner" })).status, 403);
    assert.equal(effects, 0);
    const operation = { ...b, action: "evaluate", conversation_id: "owner", operation_id: "captured-b-operation" };
    assert.equal((await send(operation)).status, 200);
    records.delete("window-b");
    assert.equal((await send(b, "window-binding")).status, 403);
    assert.equal((await send({ ...operation, operation_id: "late-b-operation" })).status, 403);
    const status = await send({ ...operation, action: "status" }, "operation");
    assert.equal(status.status, 200);
    assert.equal((await status.json()).cleanup_receipt.completed, true);
    assert.equal((await send({ ...operation, action: "status", window_binding_token: "token-a" }, "operation")).status, 403);
    records.set("window-c", { windowInstanceId: "window-c", windowOwnerId: "secondary-1", windowBindingToken: "token-c", window: { isDestroyed: () => false } });
    assert.equal((await send({ ...b, window_instance_id: "window-c" }, "window-binding")).status, 403);
    assert.equal(effects, 1);
  } finally {
    await bridge.stop();
    bridge.init({ getWindowRecordByInstanceId: (instance) => instance === "fixture-window" ? {
      windowInstanceId: instance, windowOwnerId: "primary", windowBindingToken: "fixture-binding", window: { isDestroyed: () => false },
    } : null });
  }
});

test("owned-preview authority is bound to its exact operation, owner, and preview origin", async () => {
  let calls = 0;
  bridge.init({ token: "bridge-test-token", manager: { async executeControlCommand() { calls++; return { ok: true }; } } });
  const endpoint = await bridge.start();
  const url = "http://127.0.0.1:5173/preview-token/index.html";
  const base = {
    kind: "owned_preview", url, preview_url: url, preview_id: "preview", session_id: "session",
    conversation_id: "owner", permission_mode: "bypass", operation_id: "bound-preview",
  };
  try {
    for (const patch of [{ conversation_id: "other" }, { operation_id: "other" },
      { url: "http://127.0.0.1:8000/admin" }, { preview_url: "http://127.0.0.1:8000/admin" },
      { kind: "bypass" }, { session_id: "" }, { preview_id: "" }]) {
      const result = await command(endpoint, { action: "navigate", url, conversation_id: "owner", operation_id: "bound-preview",
        navigation_authorization: { ...base, ...patch } });
      assert.equal(result.ok, false);
    }
    const missingOperation = await command(endpoint, { action: "navigate", url, conversation_id: "owner",
      navigation_authorization: { ...base, operation_id: undefined } });
    assert.equal(missingOperation.ok, false);
    assert.equal(calls, 0);
  } finally { await bridge.stop(); }
});

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

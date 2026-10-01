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

"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
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

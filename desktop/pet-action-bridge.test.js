"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createPetActionBridge } = require("./pet-action-bridge");

test("pet responses wait for the exact task window's backend receipt", async () => {
  const sent = [];
  const window = { isDestroyed: () => false, webContents: { send: (channel, request) => sent.push({ channel, request }) } };
  const bridge = createPetActionBridge(owner => owner === "owner" ? window : null);
  let accepted = false;
  const response = bridge.request("owner", "conversation", "decision").then(() => { accepted = true; });
  assert.equal(accepted, false);
  const request = sent[0].request;
  assert.equal(request.taskId, "conversation");
  assert.throws(() => bridge.complete("other-window", { id: request.id }), /does not belong/);
  bridge.complete("owner", { id: request.id });
  await response;
  assert.equal(accepted, true);
});

test("real refusal and window closure reject the Mini action", async () => {
  const sent = [];
  const bridge = createPetActionBridge(() => ({ isDestroyed: () => false, webContents: { send: (_channel, request) => sent.push(request) } }));
  const refused = bridge.request("owner", "conversation", "decision");
  bridge.complete("owner", { id: sent[0].id, error: "Original approval was replaced" });
  await assert.rejects(refused, /Original approval was replaced/);
  const closed = bridge.request("owner", "conversation", "decision");
  bridge.release("owner");
  await assert.rejects(closed, /window closed/);
});

test("a native send failure rejects its receipt and does not strand a later request", async () => {
  const sent = [];
  let unavailable = true;
  const bridge = createPetActionBridge(() => ({ isDestroyed: () => false, webContents: { send(_channel, request) {
    if (unavailable) throw new Error("Actual renderer was destroyed");
    sent.push(request);
  } } }));
  await assert.rejects(bridge.request("owner", "chat", "allow"), /Actual renderer was destroyed/);
  unavailable = false;
  const followup = bridge.requestFollowup("owner", "chat", "Actual text");
  bridge.complete("owner", { id: sent[0].id });
  await followup;
  bridge.release("owner");
});

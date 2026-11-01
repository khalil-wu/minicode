"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { RealtimeVoiceManager } = require("./realtime-voice-manager");
const bridge = require("./embedded-browser-bridge");

function fixture() {
  const sent = [], waiters = [], records = [], connections = [];
  let focused = null;
  const add = (id, remoteHostId = null) => {
    const record = { windowInstanceId: id, windowOwnerId: id + "-owner", windowBindingToken: id + "-binding", remoteHostId };
    record.window = { destroyed: false, isDestroyed() { return this.destroyed; }, isFocused: () => focused === record,
      webContents: { send(channel, request) {
        sent.push({ record, channel, request });
        const index = waiters.findIndex(waiter => waiter.record === record && waiter.type === request.type);
        if (index >= 0) waiters.splice(index, 1)[0].resolve(request);
      } } };
    records.push(record);
    return record;
  };
  const original = add("window-A"); focused = original;
  const windows = {
    getMainWindows: () => records.filter(record => !record.window.destroyed).map(record => record.window),
    getWindowRecord: window => records.find(record => record.window === window),
    getFocusedMainWindow: () => focused?.window || original.window,
    focusMainWindow: window => { focused = windows.getWindowRecord(window); },
    async createNewWindow(options) {
      const record = add("created-" + records.length, options.remoteHostId);
      record.runtime = options.runtime;
      queueMicrotask(() => manager.ready(record, { connected: true }));
      return record.window;
    },
  };
  const manager = new RealtimeVoiceManager({ windowManager: windows, platform: "win32",
    getRemoteDesktop: () => ({ async connect(id) { connections.push(id); return { runtime: () => ({ host: id }) }; } }) });
  manager.ready(original, { connected: true });
  const claim = manager.claim(original, { threadId: "A" });
  manager.update(original, { claimId: claim.claimId, phase: "active" });
  const payload = (tool, args = {}, value = claim) => ({ tool, arguments: args, call_id: "model-request",
    claim_id: value.claimId, host_id: value.hostId, conversation_id: manager.active.threadId });
  const next = (record, type) => {
    const existing = sent.find(entry => entry.record === record && entry.request.type === type && !entry.read);
    if (existing) { existing.read = true; return Promise.resolve(existing.request); }
    return new Promise(resolve => waiters.push({ record, type, resolve(request) {
      sent.find(entry => entry.request === request).read = true; resolve(request);
    } }));
  };
  const reply = (record, request, value, error) => manager.reply(record, { ...request, value, error });
  return { manager, original, claim, records, connections, sent, next, reply, payload, add,
    focus: record => { focused = record; } };
}

test("capture requires the exact live claim and actual foreground before and after the renderer reply", { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  await assert.rejects(f.manager.executeControl({ ...f.payload("capture_screen_context"), conversation_id: "B" }, f.original, signal), /original active task/);
  f.focus(null);
  await assert.rejects(f.manager.executeControl(f.payload("capture_screen_context"), f.original, signal), /foreground/);
  f.focus(f.original);
  const capture = f.manager.executeControl(f.payload("capture_screen_context"), f.original, signal);
  const request = await f.next(f.original, "capture");
  const page = { page: "settings", rightSidebarVisible: false, rightPanelTabs: [] };
  assert.throws(() => f.reply(f.original, { ...request, callId: "wrong-request" }, page), /original request/);
  const foreign = f.add("foreign");
  assert.throws(() => f.reply(foreign, request, page), /original request/);
  f.focus(null);
  f.reply(f.original, request, page);
  await assert.rejects(capture, /foreground page changed/);
  f.focus(f.original);
  const actual = f.manager.executeControl(f.payload("capture_screen_context"), f.original, signal);
  f.reply(f.original, await f.next(f.original, "capture"), page);
  assert.deepEqual(await actual, page);
});

for (const action of ["stop", "cancel", "window"]) test(`capture rejects late results after actual ${action}`, { timeout: 5000 }, async () => {
  const f = fixture(), abort = new AbortController();
  const capture = f.manager.executeControl(f.payload("capture_screen_context"), f.original, abort.signal);
  const checked = assert.rejects(capture, /ended|cancelled|available/);
  const request = await f.next(f.original, "capture");
  if (action === "stop") f.manager.update(f.original, { claimId: f.claim.claimId, phase: "stopping" });
  if (action === "cancel") abort.abort(new Error("Original request cancelled"));
  if (action === "window") f.manager.releaseWindow(f.original.windowInstanceId);
  await checked;
  assert.equal(f.reply(f.original, request, { page: "thread" }), false);
  assert.equal(f.manager.requests.size, 0);
});

test("a retired claim cannot release the replacement call", () => {
  const f = fixture();
  f.manager.release(f.original, { claimId: f.claim.claimId });
  const replacement = f.manager.claim(f.original, { threadId: "B" });
  assert.throws(() => f.manager.release(f.original, { claimId: f.claim.claimId }), /no longer active/);
  assert.equal(f.manager.active.claimId, replacement.claimId);
});

async function finishTransfer(f, promise, record, threadId) {
  const prepare = await f.next(record, "prepare");
  f.reply(record, prepare, { prepared: true, capability: { model: "actual-target-model" } });
  const stop = await f.next(f.manager.active.record, "stop");
  const old = f.manager.active;
  f.manager.update(old.record, { claimId: old.claimId, phase: "stopping" });
  f.manager.release(old.record, { claimId: old.claimId });
  f.reply(old.record, stop, { stopped: true, mediaReleased: true });
  const start = await f.next(record, "start");
  assert.equal(start.threadId, threadId);
  assert.deepEqual(start.capability, { model: "actual-target-model" });
  const targetClaim = f.manager.claim(record, { threadId, transferId: start.transferId });
  f.manager.update(record, { claimId: targetClaim.claimId, phase: "active" });
  f.reply(record, start, { started: true });
  assert.deepEqual(await promise, { transferred: true, hostId: record.remoteHostId || "local", threadId });
  return { targetClaim, start };
}

test("transfer waits for media/stop and actual target start; return restores the original locator", { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  let complete = false;
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B", context: "Actual handoff" }), f.original, signal)
    .then(value => { complete = true; return value; });
  const prepare = await f.next(f.original, "prepare");
  assert.equal(f.manager.active.threadId, "A"); assert.equal(complete, false);
  f.reply(f.original, prepare, { prepared: true, capability: {} });
  const stop = await f.next(f.original, "stop");
  assert.equal(f.sent.some(entry => entry.request.type === "start"), false);
  f.manager.release(f.original, { claimId: f.claim.claimId });
  f.reply(f.original, stop, { stopped: true, mediaReleased: true });
  const start = await f.next(f.original, "start");
  assert.equal(start.context, "Actual handoff"); assert.equal(complete, false);
  const targetClaim = f.manager.claim(f.original, { threadId: "B", transferId: start.transferId });
  assert.equal(complete, false);
  f.manager.update(f.original, { claimId: targetClaim.claimId, phase: "active" });
  f.reply(f.original, start, { started: true });
  assert.deepEqual(await transfer, { transferred: true, hostId: "local", threadId: "B" });
  const returning = f.manager.executeControl(f.payload("transfer_voice_call", { return: true }, targetClaim), f.original, signal);
  await finishTransfer(f, returning, f.original, "A");
  assert.equal(f.manager.active.previous, null);
});

for (const failure of ["prepare", "stop", "start"]) test(`real ${failure} failure never becomes transferred success`, { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B" }), f.original, signal);
  const checked = assert.rejects(transfer, /Actual .* failure|no media release/);
  const prepare = await f.next(f.original, "prepare");
  f.reply(f.original, prepare, failure === "prepare" ? undefined : { prepared: true, capability: {} }, failure === "prepare" ? "Actual prepare failure" : undefined);
  if (failure !== "prepare") {
    const stop = await f.next(f.original, "stop");
    if (failure === "stop") f.reply(f.original, stop, { stopped: false, mediaReleased: true });
    else {
      f.manager.release(f.original, { claimId: f.claim.claimId });
      f.reply(f.original, stop, { stopped: true, mediaReleased: true });
      const start = await f.next(f.original, "start");
      f.reply(f.original, start, undefined, "Actual start failure");
    }
  }
  await checked;
  assert.equal(f.manager.transfer, null);
  if (failure === "prepare") assert.equal(f.manager.active.claimId, f.claim.claimId);
  if (failure === "stop") assert.equal(f.sent.some(entry => entry.request.type === "start"), false);
});

test("a start reply alone cannot fabricate a target active receipt", { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B" }), f.original, signal);
  const checked = assert.rejects(transfer, /did not start/);
  f.reply(f.original, await f.next(f.original, "prepare"), { prepared: true, capability: {} });
  const stop = await f.next(f.original, "stop");
  f.manager.release(f.original, { claimId: f.claim.claimId });
  f.reply(f.original, stop, { stopped: true, mediaReleased: true });
  f.reply(f.original, await f.next(f.original, "start"), { started: true });
  await checked;
});

test("cross-host transfer uses the connected host runtime and target's real renderer", { timeout: 5000 }, async () => {
  const f = fixture();
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "remote-task", hostId: "ssh-actual" }), f.original, new AbortController().signal);
  await new Promise(setImmediate);
  const target = f.records[1];
  assert.deepEqual(f.connections, ["ssh-actual"]);
  assert.deepEqual(target.runtime, { host: "ssh-actual" });
  await finishTransfer(f, transfer, target, "remote-task");
  assert.equal(f.manager.active.hostId, "ssh-actual");
});

test("cancelled transfer never starts its prepared destination", { timeout: 5000 }, async () => {
  const f = fixture(), abort = new AbortController();
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B" }), f.original, abort.signal);
  const checked = assert.rejects(transfer, /cancelled/);
  await f.next(f.original, "prepare");
  abort.abort(new Error("Original request cancelled"));
  await checked;
  assert.equal(f.manager.active.claimId, f.claim.claimId);
  assert.equal(f.sent.some(entry => entry.request.type === "stop" || entry.request.type === "start"), false);
});

test("starting transfer revokes pending capture and blocks new capture throughout target preflight", { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  const capture = f.manager.executeControl(f.payload("capture_screen_context"), f.original, signal);
  const revoked = assert.rejects(capture, /before screen context arrived/);
  await f.next(f.original, "capture");
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B" }), f.original, signal);
  const failed = assert.rejects(transfer, /actual preflight rejected/);
  await revoked;
  const prepare = await f.next(f.original, "prepare");
  await assert.rejects(f.manager.executeControl(f.payload("capture_screen_context"), f.original, signal), /during voice transfer/);
  f.reply(f.original, prepare, undefined, "actual preflight rejected");
  await failed;
});

test("original window retirement cancels a transfer waiting for the target renderer to become ready", { timeout: 5000 }, async () => {
  const f = fixture(), target = f.add("not-ready-target", "ssh-pending");
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "remote-task", hostId: "ssh-pending" }), f.original, new AbortController().signal);
  const checked = assert.rejects(transfer, /no longer available/);
  await new Promise(setImmediate);
  assert.equal(f.manager.readyWaiters.size, 1);
  f.manager.releaseWindow(f.original.windowInstanceId);
  await checked;
  f.manager.ready(target, { connected: true });
  assert.equal(f.manager.readyWaiters.size, 0);
  assert.equal(f.sent.some(entry => entry.request.type === "prepare"), false);
});

test("a native renderer send error leaves no pending request behind", async () => {
  const f = fixture();
  f.original.window.webContents.send = () => { throw new Error("Actual renderer gone"); };
  await assert.rejects(f.manager.executeControl(f.payload("capture_screen_context"), f.original, new AbortController().signal), /Actual renderer gone/);
  assert.equal(f.manager.requests.size, 0);
});

test("voice control uses the authenticated original bridge window and remote-host token", { timeout: 5000 }, async () => {
  const f = fixture();
  const remote = f.add("remote", "ssh-real");
  const calls = [];
  bridge.init({ token: "local-private", getWindowRecordByInstanceId: id => f.records.find(record => record.windowInstanceId === id),
    voiceManager: { executeControl(payload, record, signal) { signal.throwIfAborted(); calls.push({ payload, record }); return { page: "home" }; } } });
  const host = { ready: { bridge_token: "remote-private" } };
  bridge.registerRemoteHost("ssh-real", host);
  const endpoint = await bridge.start();
  const request = (token, binding = remote.windowBindingToken) => fetch(endpoint + "/v1/voice-control", { method: "POST",
    headers: { authorization: "Bearer " + token }, body: JSON.stringify({ window_instance_id: remote.windowInstanceId,
      window_owner_id: remote.windowOwnerId, window_binding_token: binding, tool: "capture_screen_context" }) });
  try {
    assert.equal((await request("renderer-token")).status, 401);
    assert.equal((await request("local-private")).status, 403);
    assert.equal((await request("remote-private", "other-window")).status, 403);
    assert.equal(calls.length, 0);
    const response = await request("remote-private");
    assert.deepEqual(await response.json(), { ok: true, value: { page: "home" } });
    assert.equal(calls[0].record, remote);
  } finally { bridge.unregisterRemoteHost("ssh-real", host); await bridge.stop(); }
});

test("disconnecting the actual HTTP request cancels its native renderer request", { timeout: 5000 }, async () => {
  const f = fixture(), abort = new AbortController();
  bridge.init({ token: "original-native", voiceManager: f.manager,
    getWindowRecordByInstanceId: id => f.records.find(record => record.windowInstanceId === id) });
  const endpoint = await bridge.start();
  try {
    const response = fetch(endpoint + "/v1/voice-control", { method: "POST", signal: abort.signal,
      headers: { authorization: "Bearer original-native" }, body: JSON.stringify({ ...f.payload("capture_screen_context"),
        window_instance_id: f.original.windowInstanceId, window_owner_id: f.original.windowOwnerId, window_binding_token: f.original.windowBindingToken }) });
    const rejected = assert.rejects(response, /abort/i);
    const capture = await f.next(f.original, "capture");
    abort.abort();
    const cancel = await f.next(f.original, "cancel");
    await rejected;
    assert.equal(cancel.requestId, capture.requestId);
    assert.equal(f.manager.requests.size, 0);
    assert.equal(f.reply(f.original, capture, { page: "thread" }), false);
  } finally { await bridge.stop(); }
});

test("real preload and declared main-frame IPC reach native claim, request, reply and cleanup", async () => {
  const f = fixture(), handlers = new Map(), listeners = new Map();
  f.manager.release(f.original, { claimId: f.claim.claimId });
  const contents = f.original.window.webContents;
  contents.mainFrame = { url: "" };
  const event = { sender: contents, senderFrame: contents.mainFrame };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), { module: moduleBox, process, console,
    require: name => name === "electron" ? { ipcMain: { on() {}, handle: (channel, handler) => handlers.set(channel, handler) } } : require(name) });
  moduleBox.exports.init({ getWindowForSender: sender => sender === contents ? f.original.window : null,
    getWindowRecord: window => f.records.find(record => record.window === window), realtimeVoiceManager: f.manager });
  moduleBox.exports.registerIpcHandlers();
  let runtime;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), { require: () => ({
    ipcRenderer: { on: (channel, listener) => listeners.set(channel, listener), removeListener: channel => listeners.delete(channel), sendSync: () => ({}),
      invoke: async (channel, ...args) => handlers.get(channel)(event, ...args) },
    contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console });
  const voice = runtime.desktop.realtimeVoice;
  const original = await voice.claim({ threadId: "A" });
  await voice.update({ claimId: original.claimId, phase: "active" });
  await voice.ready({ connected: true });
  const remove = voice.onRequest(request => {
    assert.equal(request.callId, "actual-preload-model-call");
    void voice.reply({ ...request, value: { page: "settings", rightSidebarVisible: false, rightPanelTabs: [] } });
  });
  contents.send = (channel, request) => listeners.get(channel)({}, request);
  const page = await f.manager.executeControl({ ...f.payload("capture_screen_context", {}, original), call_id: "actual-preload-model-call" }, f.original, new AbortController().signal);
  assert.deepEqual(page, { page: "settings", rightSidebarVisible: false, rightPanelTabs: [] });
  assert.throws(() => handlers.get("minicode:realtimeVoice:claim")({ sender: contents, senderFrame: {} }, { threadId: "foreign" }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  remove();
  await voice.update({ claimId: original.claimId, phase: "stopping" });
  await voice.release({ claimId: original.claimId });
  assert.equal(f.manager.active, null);
});


test("transfer with handoff context refuses a pre-v3 destination before stopping the original", { timeout: 5000 }, async () => {
  const f = fixture(), signal = new AbortController().signal;
  const transfer = f.manager.executeControl(f.payload("transfer_voice_call", { threadId: "B", context: "Actual handoff" }), f.original, signal);
  const checked = assert.rejects(transfer, /cannot receive handoff context/);
  f.reply(f.original, await f.next(f.original, "prepare"), { prepared: true, capability: { version: "v2" } });
  await checked;
  assert.equal(f.manager.transfer, null);
  assert.equal(f.manager.active.claimId, f.claim.claimId);
});

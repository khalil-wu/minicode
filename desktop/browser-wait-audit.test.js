const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

function harness(found) {
  let clock = 0;
  let evaluations = 0;
  const waits = [];
  const insertions = [];
  const session = { setPermissionCheckHandler() {}, setPermissionRequestHandler() {}, setDevicePermissionHandler() {}, on() {}, webRequest: { onHeadersReceived() {} } };
  class View {
    constructor() {
      this.webContents = Object.assign(new EventEmitter(), {
        id: 1, session, url: "", navigationHistory: {},
        setWindowOpenHandler() {}, getURL() { return this.url; }, getTitle: () => "", isLoading: () => false,
        isDestroyed: () => false, close() {}, async loadURL(url) { this.url = url; },
        async executeJavaScript() { evaluations++; return typeof found === "function" ? found() : found; },
        async insertText(value) { insertions.push(value); },
      });
    }
    getBounds() { return { x: 0, y: 0, width: 0, height: 0 }; }
    setBounds() {}
    setVisible() {}
  }
  const ownerWindow = {
    isDestroyed: () => false, webContents: { send() {}, getZoomFactor: () => 1 },
    getContentBounds: () => ({ width: 1000, height: 800 }),
    contentView: { addChildView() {}, removeChildView() {} },
  };
  const moduleValue = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "embedded-browser-manager.js"), "utf8"), {
    module: moduleValue, exports: moduleValue.exports, __dirname, URL, Buffer, Date: { now: () => clock }, clearTimeout,
    setTimeout(callback, delay) { waits.push(delay); clock += delay; callback(); },
    require(name) { return name === "electron" ? { WebContentsView: View, app: {}, dialog: {} } : require(name); },
  });
  const manager = moduleValue.exports;
  manager.init({ getWindowByInstanceId: () => ownerWindow, browserSettingsPath: path.join(__dirname, ".nonexistent-wait-audit-settings.json") });
  return { manager, waits, insertions, evaluations: () => evaluations };
}

for (const found of [true, false]) test(`zero selector timeout checks the ${found ? "present" : "missing"} element once`, async () => {
  const { manager, waits, evaluations } = harness(found);
  await manager.create({ id: "target", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const result = await manager.executeControlCommand({ action: "wait_for_element", target_id: "target", conversation_id: "owner", selector: "#fixture", timeout_ms: 0, window_instance_id: "fixture-window" });
  assert.equal(result.ok, found);
  assert.equal(evaluations(), 1);
  assert.deepEqual(waits, []);
  manager.disposeAll();
});

test("selector polling does not add a full interval past its deadline", async () => {
  const { manager, waits } = harness(false);
  await manager.create({ id: "target", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const result = await manager.executeControlCommand({ action: "wait_for_element", target_id: "target", conversation_id: "owner", selector: "#fixture", timeout_ms: 25, window_instance_id: "fixture-window" });
  assert.equal(result.ok, false);
  assert.deepEqual(waits, [25]);
  manager.disposeAll();
});

test("malformed external selector timeout rejects instead of polling forever", async () => {
  const { manager, waits } = harness(false);
  await manager.create({ id: "target", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  await assert.rejects(manager.executeControlCommand({ action: "wait_for_element", target_id: "target", conversation_id: "owner", selector: "#fixture", timeout_ms: "not-a-number", window_instance_id: "fixture-window" }), /finite number/);
  assert.deepEqual(waits, []);
  manager.disposeAll();
});

test("a cancelled selector-focus does not submit the following text insertion", async () => {
  let completeFocus;
  const focus = new Promise((resolve) => { completeFocus = resolve; });
  const { manager, insertions } = harness(() => focus);
  await manager.create({ id: "target", conversation_id: "owner", url: "about:blank", window_instance_id: "fixture-window" });
  const controller = new AbortController();
  const operation = manager.executeControlCommand({ action: "type", target_id: "target", conversation_id: "owner", selector: "#fixture", text: "pending text", window_instance_id: "fixture-window" }, { signal: controller.signal });
  controller.abort(new Error("cancelled fixture"));
  completeFocus({ ok: true });
  await assert.rejects(operation, /cancelled fixture/);
  assert.deepEqual(insertions, []);
  manager.disposeAll();
});

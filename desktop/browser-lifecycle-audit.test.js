const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

function browserHarness() {
  let id = 0;
  let finishDns;
  const views = [];
  const session = { setPermissionCheckHandler() {}, setPermissionRequestHandler() {}, on() {}, webRequest: {} };
  class View {
    constructor() {
      this.bounds = { x: 0, y: 0, width: 0, height: 0 };
      this.visible = false;
      this.webContents = Object.assign(new EventEmitter(), {
        id: ++id, session, url: "", dead: false, navigationHistory: {},
        pickerCalls: [],
        focus() { this.pickerCalls.push("focus"); },
        async executeJavaScript(expression) { this.pickerCalls.push(expression); return null; },
        setWindowOpenHandler() {}, getURL() { return this.url; }, getTitle: () => "", isLoading: () => false,
        isDestroyed() { return this.dead; }, close() { this.dead = true; }, async loadURL(url) { this.url = url; },
      });
      views.push(this);
    }
    getBounds() { return this.bounds; }
    setBounds(value) { this.bounds = value; }
    setVisible(value) { this.visible = value; }
  }
  const ownerWindow = {
    isDestroyed: () => false, webContents: { send() {}, getZoomFactor: () => 1 },
    getContentBounds: () => ({ width: 1000, height: 800 }),
    contentView: { addChildView() {}, removeChildView() {} },
  };
  const moduleValue = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "embedded-browser-manager.js"), "utf8"), {
    module: moduleValue, exports: moduleValue.exports, URL, Buffer, setTimeout, clearTimeout,
    require(name) { return name === "electron" ? { WebContentsView: View, app: {}, dialog: {} } : require(name); },
  });
  const manager = moduleValue.exports;
  manager.init({
    getMainWindow: () => ownerWindow,
    browserSettingsPath: path.join(__dirname, ".nonexistent-audit-settings.json"),
    lookupHostAddresses: () => new Promise((resolve) => { finishDns = resolve; }),
  });
  return { manager, views, resolveDns: () => finishDns([{ address: "93.184.216.34", family: 4 }]) };
}

test("background navigation preserves the selected owner's visible browser", async () => {
  const { manager, views } = browserHarness();
  await manager.create({ id: "a", conversation_id: "A", url: "about:blank" });
  await manager.create({ id: "b", conversation_id: "B", url: "about:blank" });
  manager.activate("b", "B");
  manager.setBounds({ id: "b", conversation_id: "B", x: 0, y: 0, width: 600, height: 400 });
  await manager.executeControlCommand({ action: "navigate", target_id: "a", conversation_id: "A", url: "about:blank" });
  assert.equal(views[0].visible, false);
  assert.equal(views[1].visible, true);
  await manager.executeControlCommand({ action: "navigate", target_id: "b", conversation_id: "B", url: "about:blank" });
  assert.equal(views[1].visible, true);
});

for (const scope of ["tab", "conversation", "all"]) test(`closing ${scope} cancels an unpublished navigation`, async () => {
  const { manager, views, resolveDns } = browserHarness();
  const pending = manager.create({ id: "pending", conversation_id: "A", url: "https://pending.example/" });
  const cancelled = assert.rejects(pending, /cancelled/);
  if (scope === "tab") manager.close({ id: "pending", conversation_id: "A" });
  else if (scope === "conversation") manager.closeConversation("A");
  else manager.disposeAll();
  resolveDns();
  await cancelled;
  assert.equal(views.length, 0);
  assert.equal(manager.listTargets("A").length, 0);
});

for (const action of ["pick_element", "pick_region"]) test(`${action} gives Escape to the selected native page before starting selection`, async () => {
  const { manager, views } = browserHarness();
  await manager.create({ id: "design", conversation_id: "A", url: "about:blank" });
  const result = await manager.executeControlCommand({ action, target_id: "design", conversation_id: "A" });
  assert.equal(views[0].webContents.pickerCalls[0], "focus");
  assert.match(views[0].webContents.pickerCalls[1], /Escape/);
  assert.equal(result.ok, true);
  assert.equal(result.value, null);
});

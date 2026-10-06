"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { popupApplicationMenu } = require("./menu-popup");

for (const key of ["file", "edit", "view", "help"]) {
  test(`${key} popup uses the existing submenu and settles after either selection or dismissal`, async () => {
    let options;
    let focused = 0;
    const window = { isDestroyed: () => false, focus: () => { focused += 1; } };
    const menu = { getMenuItemById(id) {
      assert.equal(id, `minicode-menu-${key}`);
      return { submenu: { popup(value) { options = value; } } };
    } };
    const popup = popupApplicationMenu(menu, key, window);
    let settled = false;
    void popup.then(() => { settled = true; });
    await Promise.resolve();
    assert.equal(settled, false);
    assert.equal(options.window, window);
    assert.deepEqual(Object.keys(options).sort(), ["callback", "window"]);
    options.callback();
    assert.equal(await popup, undefined);
    assert.equal(focused, 1);
  });
}

test("closing the owner window during a menu does not leave its popup promise pending", async () => {
  let close;
  let destroyed = false;
  const window = { isDestroyed: () => destroyed, focus: () => assert.fail("destroyed window was focused") };
  const popup = popupApplicationMenu({ getMenuItemById: () => ({ submenu: { popup: (options) => { close = options.callback; } } }) }, "file", window);
  destroyed = true;
  close();
  await popup;
});

test("a native popup failure rejects instead of leaving the renderer waiting", async () => {
  const error = new Error("native menu unavailable");
  const menu = { getMenuItemById: () => ({ submenu: { popup() { throw error; } } }) };
  await assert.rejects(popupApplicationMenu(menu, "file", {}), (failure) => failure === error);
});

test("menu IPC accepts only its enum from the trusted main frame", async () => {
  const handlers = new Map();
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console,
    require(name) {
      if (name === "electron") return { ipcMain: { on() {}, handle(channel, handler) { handlers.set(channel, handler); } } };
      return require(name);
    },
  });
  const mainFrame = { url: "file:///minicode/index.html" };
  const webContents = { mainFrame, getURL: () => mainFrame.url };
  const window = { webContents, isDestroyed: () => false };
  const calls = [];
  moduleBox.exports.init({ getMainWindow: () => window, popupApplicationMenu: (key) => { calls.push(key); return Promise.resolve(); } });
  moduleBox.exports.registerIpcHandlers();
  const popup = handlers.get("minicode:menu:popup");
  const owned = { sender: webContents, senderFrame: mainFrame };
  assert.throws(() => popup({ sender: webContents, senderFrame: { url: mainFrame.url } }, "file"), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  for (const invalid of ["window", "unknown", "", null, { key: "file" }]) {
    assert.throws(() => popup(owned, invalid), /请选择文件、编辑、视图或帮助菜单/);
  }
  assert.equal(calls.length, 0);
  for (const key of ["file", "edit", "view", "help"]) await popup(owned, key, { x: 20, path: "ignored" });
  assert.deepEqual(calls, ["file", "edit", "view", "help"]);
});

test("preload forwards only the menu key and exposes no path or coordinate argument", async () => {
  let runtime;
  const calls = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({
      ipcRenderer: { sendSync: () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" }),
        on() {}, invoke: (...args) => { calls.push(args); return Promise.resolve(); } },
      contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } },
    }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  assert.equal(calls.length, 0);
  await runtime.desktop.menu.popup("edit", { x: 0, path: "ignored" });
  assert.deepEqual(calls, [["minicode:menu:popup", "edit"]]);
});

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
  const states = [];
  const executions = [];
  moduleBox.exports.init({ getWindowForSender: (sender) => sender === window.webContents ? window : null, getWindowRecord: () => ({ windowInstanceId: 'fixture-window' }), popupApplicationMenu: (key) => { calls.push(key); return Promise.resolve(); }, setApplicationMenuState: (owner, state) => { states.push({ owner, state }); }, executeApplicationMenuAction: (owner, action) => { executions.push({ owner, action }); return true; } });
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
  const setState = handlers.get("minicode:menu:setState");
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: false, newProjectEnabled: true, editorFocused: false, accelerators: { terminal: "CommandOrControl+Shift+Y" }, acceleratorBindings: { terminal: ["CommandOrControl+Shift+Y"] }, shortcutOverrides: ["terminal"], shortcutRecording: false };
  assert.throws(() => setState({ sender: webContents, senderFrame: { url: mainFrame.url } }, state), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  for (const invalid of [null, {}, { ...state, editorFocused: 1 }, { ...state, extra: true }]) {
    assert.throws(() => setState(owned, invalid), /capability fields/);
  }
  await setState(owned, state);
  assert.deepEqual(states, [{ owner: window, state }]);
  const execute = handlers.get("minicode:menu:execute");
  assert.throws(() => execute({ sender: webContents, senderFrame: { url: mainFrame.url } }, "terminal"), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.throws(() => execute(owned, { action: "terminal" }), /action id/);
  assert.equal(await execute(owned, "terminal"), true);
  assert.deepEqual(executions, [{ owner: window, action: "terminal" }]);
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
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: false, newProjectEnabled: true, editorFocused: false };
  await runtime.desktop.menu.setState(state);
  assert.deepEqual(calls.at(-1), ["minicode:menu:setState", state]);
  await runtime.desktop.menu.execute("terminal");
  assert.deepEqual(calls.at(-1), ["minicode:menu:execute", "terminal"]);
});

test("preload connects native menu actions to the existing renderer event consumers", () => {
  const handlers = new Map();
  const received = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({
      ipcRenderer: { sendSync: () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" }),
        on: (channel, listener) => { handlers.set(channel, listener); }, invoke() {} },
      contextBridge: { exposeInMainWorld() {} },
    }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, Event,
    window: { dispatchEvent: event => received.push(event.type) }, console,
  });
  const actions = ["new-project", "open-browser-tab", "focus-browser-address-bar", "toggle-file-tree", "toggle-bottom-panel", "toggle-pinned-summary", "toggle-side-panel", "find-in-thread", "find-next-in-thread", "find-previous-in-thread", "navigate-back", "navigate-forward", "previous-thread", "next-thread", "previous-tab", "next-tab", "close-tab", "open-shortcuts", "open-command-menu", "search-files", "search-chats"];
  for (const action of actions) handlers.get("minicode:menu:" + action)();
  assert.deepEqual(received, actions);
});

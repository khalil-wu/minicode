"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function applicationMenuHarness(locale = "en-US") {
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const start = source.indexOf("const applicationMenuStates = new WeakMap();");
  const end = source.indexOf("// Startup failure window", start);
  let menu;
  let updaterAvailable = false;
  const events = [];
  const links = [];
  const native = [];
  let handleBrowser = false;
  let browserVisible = false;
  let zoomLevel = 0;
  let about;
  let updateChecks = 0;
  const owner = {
    isDestroyed: () => false,
    webContents: { getZoomLevel: () => zoomLevel, setZoomLevel: (value) => { zoomLevel = value; }, setIgnoreMenuShortcuts: value => { owner.ignoreMenuShortcuts = value; } },
  };
  let focusedWindow = owner;
  const focusedContents = { id: 20 };
  const moduleValue = { exports: {} };
  vm.runInNewContext(source.slice(start, end) + "\nmodule.exports = { buildApplicationMenu, setApplicationMenuState, executeApplicationMenuAction };", {
    module: moduleValue, process: { platform: "win32" },
    dictationHotkeys: { async suspend() {} },
    app: {
      getLocale: () => locale, getName: () => "MiniCode", getVersion: () => "0.2.0",
      setAboutPanelOptions: (options) => { about = options; }, showAboutPanel: () => { about.shown = true; },
    },
    desktopPreferences: { getPreferences: () => ({ localeOverride: null }) },
    windowManager: { getFocusedMainWindow: () => focusedWindow },
    webContents: { getFocusedWebContents: () => focusedContents },
    sendWorkbenchMenuEvent: (channel, window) => { events.push({ channel, window }); },
    embeddedBrowserManager: {
      canReloadVisiblePage: () => browserVisible,
      runMenuCommand: (window, contents, action) => { native.push({ window, contents, action }); return handleBrowser; },
    },
    Menu: { buildFromTemplate: (template) => {
      const defaults = items => { for (const item of items) { item.enabled ??= true; if (item.submenu) defaults(item.submenu); } };
      defaults(template); return template;
    }, setApplicationMenu: (value) => { menu = value; },
      getApplicationMenu: () => ({ getMenuItemById(id) { const find = items => { for (const item of items) { if (item.id === id) return item; const nested = item.submenu && find(item.submenu); if (nested) return nested; } }; return find(menu); } }) },
    shell: { openExternal: (url) => { links.push(url); } },
    updater: { isAvailable: () => updaterAvailable, check: () => { updateChecks++; } },
  });
  moduleValue.exports.buildApplicationMenu(owner);
  return {
    ...moduleValue.exports, owner, events, links, native, focusedContents,
    get menu() { return menu; },
    get about() { return about; },
    get zoomLevel() { return zoomLevel; },
    get updateChecks() { return updateChecks; },
    setBrowser: (value) => { handleBrowser = value; browserVisible = value; },
    setFocusedWindow: (value) => { focusedWindow = value; },
    setUpdater: (value) => { updaterAvailable = value; },
  };
}

const visible = (items) => items.filter(item => item.visible !== false);
const titles = (items) => visible(items).map(item => item.type === "separator" ? "---" : item.label);
const submenu = (harness, key) => harness.menu.find(item => item.id === "minicode-menu-" + key).submenu;

test("Windows native menu follows the installed production groups without MiniCode-only controls", () => {
  const h = applicationMenuHarness();
  assert.deepEqual(Array.from(h.menu, item => item.label), ["File", "Edit", "View", "Help"]);
  assert.deepEqual(Array.from(titles(submenu(h, "file"))), ["New Chat", "New Temporary Chat", "New Project…", "---", "Close", "---", "Quit MiniCode"]);
  assert.equal(submenu(h, "file").find(item => item.label === "New Temporary Chat").enabled, false);
  assert.deepEqual(Array.from(titles(submenu(h, "edit"))), ["Undo", "Redo", "---", "Cut", "Copy", "Paste", "Delete", "---", "Select All", "---", "Settings…"]);
  assert.deepEqual(Array.from(titles(submenu(h, "view"))), ["Toggle Sidebar", "Toggle Pinned Summary", "Toggle navigation panel", "Toggle Changes Panel", "Browser", "---", "Find", "---", "Back", "Forward", "---", "Previous Sidebar Item", "Next Sidebar Item", "---", "Previous Tab", "Next Tab", "---", "Zoom In", "Zoom Out", "Actual Size", "---", "Toggle Full Screen"]);
  const browser = submenu(h, "view").find(item => item.label === "Browser");
  assert.deepEqual(Array.from(titles(browser.submenu)), ["Open Browser Tab", "Focus Browser Address Bar", "Reload Browser Page"]);
  const reload = browser.submenu.find(item => item.id === "minicode-shortcut-browserReload");
  assert.equal(reload.enabled, false);
  assert.notEqual(reload.visible, false);
  assert.equal(submenu(h, "view").some(item => ["Reload", "Force Reload", "Toggle Developer Tools", "Toggle Context Panel"].includes(item.label)), false);
});

test("queue history uses the existing Edit actions and keeps native text history on its original window", () => {
  const h = applicationMenuHarness();
  const calls = [];
  h.owner.webContents.undo = () => calls.push("owner-undo");
  h.owner.webContents.redo = () => calls.push("owner-redo");
  h.executeApplicationMenuAction(h.owner, "undoAppAction");
  assert.equal(h.events.at(-1).channel, "minicode:menu:undo-app-action");
  h.executeApplicationMenuAction(h.owner, "redoAppAction");
  assert.equal(h.events.at(-1).channel, "minicode:menu:redo-app-action");
  h.setBrowser(true);
  const count = h.events.length;
  h.executeApplicationMenuAction(h.owner, "undoAppAction");
  assert.equal(h.events.length, count);
  assert.equal(h.native.at(-1).action, "undo");
  h.executeApplicationMenuAction(h.owner, "nativeTextUndo");
  h.executeApplicationMenuAction(h.owner, "nativeTextRedo");
  assert.deepEqual(calls, ["owner-undo", "owner-redo"]);
});

test("editor menu extensions stay in the Code workspace while hidden native consumers keep their keyboard paths", () => {
  const h = applicationMenuHarness();
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: false, newProjectEnabled: true, editorFocused: false };
  h.setApplicationMenuState(h.owner, state);
  assert.equal(visible(submenu(h, "file")).some(item => ["New Window", "Open Folder…"].includes(item.label)), false);
  assert.equal(visible(submenu(h, "view")).some(item => ["Toggle Bottom Panel", "Open Terminal"].includes(item.label)), false);
  assert.equal(h.executeApplicationMenuAction(h.owner, "openFolder"), true);
  assert.equal(h.events.at(-1).channel, "minicode:menu:open-folder");
  assert.equal(h.executeApplicationMenuAction(h.owner, "terminal"), true);
  assert.equal(h.events.at(-1).channel, "minicode:shortcut:terminal");
  assert.equal(h.executeApplicationMenuAction(h.owner, "browserTab"), true);
  assert.equal(h.events.at(-1).channel, "minicode:menu:open-browser-tab");
  h.setApplicationMenuState(h.owner, { ...state, bottomPanelToggleEnabled: true });
  assert.equal(visible(submenu(h, "file")).some(item => item.label === "Open Folder…"), true);
  assert.equal(visible(submenu(h, "file")).some(item => item.label === "New Window"), false);
  assert.deepEqual(Array.from(visible(submenu(h, "view")).filter(item => ["Toggle Bottom Panel", "Open Terminal"].includes(item.label)), item => item.label), ["Toggle Bottom Panel", "Open Terminal"]);
});

test("native capabilities are window-owned and preserve the editor Save All chord", () => {
  const h = applicationMenuHarness("zh-CN");
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: true, newProjectEnabled: true, editorFocused: false };
  h.setApplicationMenuState(h.owner, state);
  assert.equal(submenu(h, "file").find(item => item.label === "新建项目…").enabled, true);
  assert.equal(submenu(h, "view").find(item => item.label === "打开终端").enabled, true);
  assert.equal(submenu(h, "view")[0].accelerator, "Ctrl+Shift+S");
  h.setApplicationMenuState(h.owner, { ...state, editorFocused: true });
  assert.equal(submenu(h, "view")[0].accelerator, "Ctrl+B");
  assert.equal(submenu(h, "view").some(item => item.accelerator === "Ctrl+Shift+S"), false);
  h.setApplicationMenuState({ webContents: { setIgnoreMenuShortcuts() {} } }, { ...state, terminalEnabled: false });
  assert.equal(submenu(h, "view").find(item => item.label === "打开终端").enabled, true);
});

test("each saved secondary binding replaces the registered native aliases", () => {
  const h = applicationMenuHarness();
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: true, newProjectEnabled: true, editorFocused: false,
    accelerators: { newConversation: "CommandOrControl+N" },
    acceleratorBindings: { newConversation: ["CommandOrControl+N", "CommandOrControl+Shift+Y", "CommandOrControl+Alt+Y"] },
    shortcutOverrides: ["newConversation"], shortcutRecording: false };
  h.setApplicationMenuState(h.owner, state);
  assert.equal(submenu(h, "file").find(item => item.id === "minicode-shortcut-newConversation").accelerator, "CommandOrControl+N");
  assert.equal(submenu(h, "file").some(item => item.accelerator === "Ctrl+Shift+O"), false);
  assert.deepEqual(Array.from(submenu(h, "file").filter(item => item.visible === false && item.accelerator), item => item.accelerator),
    ["CommandOrControl+Shift+Y", "CommandOrControl+Alt+Y"]);
  h.setApplicationMenuState(h.owner, { ...state, accelerators: { newConversation: "CommandOrControl+Alt+Y" },
    acceleratorBindings: { newConversation: ["CommandOrControl+Alt+Y"] } });
  assert.equal(submenu(h, "file").find(item => item.id === "minicode-shortcut-newConversation").accelerator, "CommandOrControl+Alt+Y");
  assert.equal(submenu(h, "file").some(item => item.accelerator === "CommandOrControl+Shift+Y"), false);
});

test("the saved keymap drives native registration, unbinding and aliases for only its window", () => {
  const h = applicationMenuHarness();
  const state = { terminalEnabled: true, bottomPanelToggleEnabled: true, newProjectEnabled: true, editorFocused: false,
    accelerators: { terminal: "CommandOrControl+Shift+Y", leftSidebar: "CommandOrControl+Shift+S", saveAllFiles: "CommandOrControl+Shift+S", newConversation: null },
    shortcutOverrides: ["terminal", "newConversation"], shortcutRecording: true };
  h.setApplicationMenuState(h.owner, state);
  assert.equal(h.owner.ignoreMenuShortcuts, true);
  assert.equal(submenu(h, "view").find(item => item.label === "Open Terminal").accelerator, "CommandOrControl+Shift+Y");
  assert.equal(submenu(h, "file").find(item => item.label === "New Chat").accelerator, undefined);
  assert.equal(submenu(h, "file").some(item => item.accelerator === "Ctrl+Shift+O"), false);
  h.setApplicationMenuState(h.owner, { ...state, editorFocused: true, shortcutRecording: false });
  assert.equal(h.owner.ignoreMenuShortcuts, false);
  assert.equal(submenu(h, "view")[0].accelerator, "Ctrl+B");
  h.setApplicationMenuState(h.owner, { ...state, accelerators: { ...state.accelerators, terminal: null, leftSidebar: null }, shortcutOverrides: ["terminal", "newConversation", "leftSidebar"], shortcutRecording: false });
  assert.equal(submenu(h, "view").find(item => item.label === "Open Terminal").accelerator, undefined);
  assert.equal(submenu(h, "view").some(item => item.accelerator === "Ctrl+B"), false);
  const background = { webContents: { setIgnoreMenuShortcuts(value) { this.ignored = value; } } };
  h.setApplicationMenuState(background, state);
  assert.equal(background.webContents.ignored, true);
  assert.equal(h.owner.ignoreMenuShortcuts, false);
  assert.equal(submenu(h, "view").find(item => item.label === "Open Terminal").accelerator, undefined);
});

test("menu clicks preserve their owner and native browser commands precede chat fallthrough", () => {
  const h = applicationMenuHarness();
  h.setFocusedWindow({ isDestroyed: () => false });
  submenu(h, "file").find(item => item.label === "New Chat").click();
  assert.equal(h.events[0].window, h.owner);
  const find = submenu(h, "view").find(item => item.label === "Find");
  find.click();
  assert.equal(h.events.at(-1).channel, "minicode:menu:find-in-thread");
  h.setBrowser(true);
  const eventsBefore = h.events.length;
  find.click();
  submenu(h, "file").find(item => item.label === "Close").click();
  assert.equal(h.events.length, eventsBefore);
  assert.deepEqual(h.native.slice(-2).map(item => item.action), ["open-find", "close"]);
  assert.equal(h.native.at(-1).window, h.owner);
  assert.equal(h.native.at(-1).contents, h.focusedContents);
  h.setBrowser(false);
  submenu(h, "file").find(item => item.label === "Close").click();
  assert.equal(h.events.at(-1).channel, "minicode:menu:close-tab");
  submenu(h, "view").find(item => item.label === "Back").click();
  assert.equal(h.events.at(-1).channel, "minicode:menu:navigate-back");
  submenu(h, "view").find(item => item.label === "Toggle Changes Panel").click();
  assert.equal(h.events.at(-1).channel, "minicode:menu:toggle-side-panel");
  assert.equal(h.events.at(-1).window, h.owner);
});

test("renderer keyboard execution reaches the owned native command and preserves disabled state", () => {
  const h = applicationMenuHarness();
  h.setApplicationMenuState(h.owner, { terminalEnabled: true, bottomPanelToggleEnabled: false, newProjectEnabled: true, editorFocused: false,
    accelerators: { terminal: "CommandOrControl+Shift+Y" }, shortcutOverrides: ["terminal"], shortcutRecording: false });
  assert.equal(h.executeApplicationMenuAction(h.owner, "terminal"), true);
  assert.equal(h.events.at(-1).channel, "minicode:shortcut:terminal");
  assert.equal(h.events.at(-1).window, h.owner);
  assert.equal(h.executeApplicationMenuAction(h.owner, "bottomPanel"), false);
  assert.throws(() => h.executeApplicationMenuAction(h.owner, "diagnostics.open"), /Unknown native menu command/);
  h.setBrowser(true);
  h.executeApplicationMenuAction(h.owner, "browserReload");
  assert.equal(h.native.at(-1).action, "reload");
});

test("chat accelerators reach their owning renderer through the real hidden menu and preload bridge", () => {
  const h = applicationMenuHarness();
  const listeners = new Map(), dispatched = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({ ipcRenderer: { on: (channel, callback) => listeners.set(channel, callback), sendSync: () => ({}) },
      contextBridge: { exposeInMainWorld() {} } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" },
    window: { dispatchEvent: event => dispatched.push(event.type) },
    Event: class { constructor(type) { this.type = type; } }, console,
  });
  h.setBrowser(true);
  h.setFocusedWindow({ isDestroyed: () => false });
  for (const [action, event, accelerator] of [
    ["renameThread", "rename-thread", "CommandOrControl+Alt+R"],
    ["toggleThreadPin", "toggle-thread-pin", "CommandOrControl+Alt+P"],
    ["archiveThread", "archive-thread", "CommandOrControl+Shift+A"],
    ["copyDeeplink", "copy-deeplink", "CommandOrControl+Alt+L"],
    ["copyWorkingDirectory", "copy-working-directory", "CommandOrControl+Shift+C"],
    ["openSideChat", "open-side-chat", "CommandOrControl+Alt+S"],
  ]) {
    const item = submenu(h, "file").find(item => item.id === "minicode-shortcut-" + action);
    assert.equal(item.visible, false);
    assert.equal(item.acceleratorWorksWhenHidden, true);
    assert.equal(item.accelerator, accelerator);
    assert.equal(h.executeApplicationMenuAction(h.owner, action), true);
    const delivery = h.events.at(-1);
    assert.equal(delivery.window, h.owner);
    assert.equal(delivery.channel, "minicode:menu:" + event);
    listeners.get(delivery.channel)({});
    assert.equal(dispatched.at(-1), event);
  }
  assert.equal(dispatched.length, 6);
  assert.equal(h.native.length, 0);
  h.setFocusedWindow(h.owner);
  h.setApplicationMenuState(h.owner, { accelerators: { renameThread: "CommandOrControl+Shift+Y" },
    acceleratorBindings: { renameThread: ["CommandOrControl+Shift+Y", "CommandOrControl+Alt+Y"] },
    shortcutOverrides: ["renameThread"], shortcutRecording: false });
  assert.equal(submenu(h, "file").find(item => item.id === "minicode-shortcut-renameThread").accelerator, "CommandOrControl+Shift+Y");
  assert.equal(submenu(h, "file").some(item => item.accelerator === "CommandOrControl+Alt+R"), false);
  assert.equal(submenu(h, "file").filter(item => item.accelerator === "CommandOrControl+Alt+Y").length, 1);
  h.setApplicationMenuState(h.owner, { accelerators: { renameThread: null }, acceleratorBindings: { renameThread: [] },
    shortcutOverrides: ["renameThread"], shortcutRecording: false });
  assert.equal(submenu(h, "file").find(item => item.id === "minicode-shortcut-renameThread").accelerator, undefined);
});

test("zoom targets the native page or the owner window, and help opens the actual Source destinations", () => {
  const h = applicationMenuHarness();
  const zoom = submenu(h, "view").find(item => item.label === "Zoom In");
  zoom.click();
  assert.equal(h.zoomLevel, .5);
  h.setBrowser(true);
  zoom.click();
  assert.equal(h.zoomLevel, .5);
  assert.equal(h.native.at(-1).action, "zoom-in");
  for (const item of submenu(h, "help")) if (["Documentation", "What's New", "Troubleshooting", "System Status"].includes(item.label)) item.click();
  assert.deepEqual(h.links, ["https://developers.openai.com/codex/app", "https://developers.openai.com/codex/changelog", "https://developers.openai.com/codex/app/troubleshooting", "https://status.openai.com"]);
  submenu(h, "help").find(item => item.label === "About MiniCode").click();
  assert.deepEqual(structuredClone(h.about), { applicationName: "MiniCode", applicationVersion: "0.2.0", shown: true });
  assert.equal(submenu(h, "help").some(item => item.label === "Check for Updates…"), false);
  h.setUpdater(true);
  h.buildApplicationMenu(h.owner);
  submenu(h, "help").find(item => item.label === "Check for Updates…").click();
  assert.equal(h.updateChecks, 1);
});

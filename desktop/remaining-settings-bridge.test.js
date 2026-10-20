"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { createBrowserPersonalData } = require("./browser-personal-data");

test("settings preload reaches native ownership and isolated data consumers", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-settings-bridge-"));
  t.after(() => { assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir())); fs.rmSync(directory, { recursive: true }); });
  const safeStorage = { isEncryptionAvailable: () => true,
    encryptString: text => Buffer.from(text).map(byte => byte ^ 0x57),
    decryptString: bytes => Buffer.from(bytes).map(byte => byte ^ 0x57).toString("utf8") };
  const personal = createBrowserPersonalData({ filePath: path.join(directory, "personal.enc"), safeStorage });
  const handlers = new Map();
  const listeners = new Map();
  const mainFrame = {};
  const contents = { id: 37, mainFrame };
  const window = { webContents: contents, isDestroyed: () => false };
  const event = { sender: contents, senderFrame: mainFrame };
  const calls = [];
  const fonts = [{ family: "Fixture Sans", faces: [{ family: "Fixture Sans", fullName: "Fixture Sans Regular", postscriptName: "FixtureSans-Regular", styleName: "Regular", weight: 400, style: "normal", isMonospaced: false }] }];
  const policy = { categories: [{ id: "camera", allowedSettings: ["ask", "allow", "block"], defaultSettings: ["ask", "block"] }],
    defaults: { camera: "ask" }, sites: [], devices: [{ id: "fixture-device", origin: "https://fixture.test", name: "Fixture USB" }] };
  const petState = { mutedConversationIds: ["fixture-chat"] };
  const sandbox = {
    prepare: (...args) => { calls.push(["prepare", ...args]); return { id: "owned-view", src: "minicode-mcp-host://view/owned-view", partition: "persist:minicode-mcp-apps" }; },
    release: (...args) => calls.push(["release", ...args]),
    clearData: () => { calls.push(["clearData"]); },
  };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console,
    require: name => name === "electron" ? { ipcMain: { on: (name, listener) => listeners.set(name, listener), handle: (name, handler) => handlers.set(name, handler) } }
      : name === "./system-settings" ? { getStartupStatus: () => true, openStartupSettings: () => calls.push(["startupSettings"]), getOpenSourceLicenses: () => "Fixture package license" }
      : name === "./system-fonts" ? { listFontFamilies: async () => { calls.push(["fontFamilies"]); return fonts; } } : require(name),
  });
  moduleBox.exports.init({ getWindowForSender: sender => sender === contents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "settings-owner" }),
    getRuntimeConfig: () => ({ apiBaseUrl: "http://127.0.0.1:8123", wsBaseUrl: "ws://127.0.0.1:8123" }),
    browserPersonalData: personal, getMcpAppSandbox: () => sandbox,
    getPetManager: async () => ({ setNotificationMuted: request => { petState.mutedConversationIds = request.muted ? [request.conversationId] : []; return petState; } }),
    embeddedBrowserManager: { listSitePermissions: () => [{ origin: "https://fixture.test", permissions: ["notifications"] }], removeSitePermissions: request => calls.push(["resetSite", request.origin]),
      getSitePolicy: () => { calls.push(["getSitePolicy"]); return policy; },
      setSitePolicy: request => { calls.push(["setSitePolicy", request.origin, request.category, request.setting]); policy.sites = [{ origin: request.origin, settings: { [request.category]: request.setting } }]; return policy; },
      removeSiteDevice: request => { calls.push(["removeSiteDevice", request.id]); policy.devices = policy.devices.filter(device => device.id !== request.id); return policy; },
      handlePasswordSubmission: async (sender, request) => { calls.push(["guestSubmitted", sender.sender.id, request.origin]); return false; },
    },
  });
  moduleBox.exports.registerIpcHandlers();
  let runtime;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({ ipcRenderer: { on() {}, removeListener() {}, sendSync: () => ({}),
      invoke: async (channel, ...args) => handlers.get(channel)(event, ...args) },
    contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  await runtime.desktop.browserPersonalData.savePassword({ origin: "https://fixture.test/login", username: "fixture-user", password: "fixture-password" });
  const listed = await runtime.desktop.browserPersonalData.listPasswords();
  assert.equal(listed.items[0].origin, "https://fixture.test");
  assert.equal("password" in listed.items[0], false);
  const selected = await runtime.desktop.browserPersonalData.getPassword({ id: listed.items[0].id });
  assert.equal(selected.password, "fixture-password");
  assert.equal(fs.readFileSync(path.join(directory, "personal.enc"), "utf8").includes("fixture-password"), false);
  assert.throws(() => handlers.get("minicode:browserPersonalData:getPassword")({ sender: contents, senderFrame: {} }, { id: listed.items[0].id }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  await runtime.desktop.browserPersonalData.setAutofillSettings({ passwordAutofillEnabled: false });
  assert.equal((await runtime.desktop.browserPersonalData.listPasswords()).passwordAutofillEnabled, false);
  await runtime.desktop.browserPersonalData.removePassword({ id: listed.items[0].id });
  assert.equal((await runtime.desktop.browserPersonalData.listPasswords()).items.length, 0);
  assert.equal((await runtime.desktop.embeddedBrowser.listSitePermissions())[0].origin, "https://fixture.test");
  await runtime.desktop.embeddedBrowser.removeSitePermissions({ origin: "https://fixture.test" });
  assert.equal((await runtime.desktop.embeddedBrowser.getSitePolicy()).categories[0].id, "camera");
  assert.deepEqual((await runtime.desktop.embeddedBrowser.setSitePolicy({ origin: "https://fixture.test", category: "camera", setting: "allow" })).sites,
    [{ origin: "https://fixture.test", settings: { camera: "allow" } }]);
  assert.deepEqual((await runtime.desktop.embeddedBrowser.removeSiteDevice({ id: "fixture-device" })).devices, []);
  assert.deepEqual(await runtime.desktop.windowControls.listFontFamilies(), fonts);
  for (const [channel, request] of [["minicode:embeddedBrowser:getSitePolicy"], ["minicode:embeddedBrowser:setSitePolicy", { origin: "https://fixture.test", category: "camera", setting: "allow" }],
    ["minicode:embeddedBrowser:removeSiteDevice", { id: "fixture-device" }], ["minicode:window:fontFamilies"]]) {
    assert.throws(() => handlers.get(channel)({ sender: contents, senderFrame: {} }, request), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  }
  assert.equal("browserPassword" in runtime.desktop, false);
  assert.equal("handlePasswordSubmission" in runtime.desktop.embeddedBrowser, false);
  assert.equal("capturePassword" in runtime.desktop.embeddedBrowser, false);
  const guestFrame = { url: "https://fixture.test/login" };
  const guest = { sender: { id: 91, mainFrame: guestFrame }, senderFrame: guestFrame };
  listeners.get("minicode:browserPassword:submitted")(guest, { origin: "https://fixture.test", documentUrl: guestFrame.url, username: "fixture-user", password: "fixture-submission-only" });
  await runtime.desktop.mcpAppSandbox.prepare("http://fixture-frame");
  await runtime.desktop.mcpAppSandbox.release("owned-view");
  await runtime.desktop.mcpAppSandbox.clearData();
  assert.equal(await runtime.desktop.preferences.getStartupStatus(), true);
  assert.equal(await runtime.desktop.preferences.getOpenSourceLicenses(), "Fixture package license");
  await runtime.desktop.preferences.openStartupSettings();
  assert.deepEqual((await runtime.desktop.pets.setNotificationMuted({ conversationId: "fixture-chat", muted: false })).mutedConversationIds, []);
  assert.throws(() => handlers.get("minicode:pets:setNotificationMuted")({ sender: contents, senderFrame: {} }, { conversationId: "fixture-chat", muted: false }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.deepEqual(calls, [["resetSite", "https://fixture.test"], ["getSitePolicy"], ["setSitePolicy", "https://fixture.test", "camera", "allow"], ["removeSiteDevice", "fixture-device"], ["fontFamilies"], ["guestSubmitted", 91, "https://fixture.test"],
    ["prepare", 37, "http://fixture-frame", "http://127.0.0.1:8123"], ["release", 37, "owned-view"], ["clearData"], ["startupSettings"]]);
});

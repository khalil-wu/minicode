"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const vm = require("node:vm");
const security = require("./security");
const { RemoteDesktop } = require("./remote-desktop");

async function settingsFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-registered-settings-"));
  t.after(() => {
    assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
    fs.rmSync(directory, { recursive: true });
  });
  const workspace = path.join(directory, "workspace");
  const settingsPath = path.join(directory, "profile", "settings.json");
  const dataPath = path.join(directory, "profile", "data");
  const profileHook = path.join(directory, "profile", "audit.config.toml");
  const pluginHook = path.join(directory, "installed", "fixture-plugin", ".minicode-plugin", "plugin.json");
  fs.mkdirSync(workspace);
  fs.mkdirSync(dataPath, { recursive: true });
  fs.mkdirSync(path.dirname(pluginHook), { recursive: true });
  for (const target of [settingsPath, profileHook, pluginHook]) fs.writeFileSync(target, "fixture");
  const hooks = new Map([["profile-key", profileHook], ["plugin-key", pluginHook]]);
  security.init({ initialRoots: [workspace], readOnlyRoots: [], userOutputRoots: [] });
  const requests = [];
  const opened = [];
  const state = { status: 200, detail: "Settings catalog rejected this action", shellError: "", shellException: null };
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://fixture");
    requests.push({ url, token: request.headers["x-minicode-token"], cache: request.headers["cache-control"] });
    response.setHeader("Content-Type", "application/json");
    if (request.headers["x-minicode-token"] !== "owned-runtime-token") {
      response.writeHead(401).end(JSON.stringify({ detail: "Wrong runtime token" }));
      return;
    }
    if (state.status !== 200) {
      response.writeHead(state.status).end(JSON.stringify({ detail: state.detail }));
      return;
    }
    const kind = url.searchParams.get("kind");
    const target = kind === "config" ? settingsPath : kind === "data" ? dataPath : hooks.get(url.searchParams.get("hook_key"));
    if (!target) response.writeHead(404).end(JSON.stringify({ detail: "Hook is no longer registered; reload its configuration." }));
    else response.end(JSON.stringify({ path: target }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const apiBaseUrl = `http://127.0.0.1:${server.address().port}`;
  const handlers = new Map();
  const mainFrame = {};
  const webContents = { mainFrame };
  const window = { webContents, isDestroyed: () => false };
  const record = { windowInstanceId: "settings-window" };
  const sender = { event: { sender: webContents, senderFrame: mainFrame } };
  const runtimeOwners = [];
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console, URL, fetch,
    require: name => name === "electron" ? {
      ipcMain: { on() {}, handle: (name, handler) => handlers.set(name, handler) },
      shell: { openPath: async target => {
        opened.push(target);
        if (state.shellException) throw state.shellException;
        return state.shellError;
      } },
    } : require(name),
  });
  moduleBox.exports.init({
    getWindowForSender: contents => contents === webContents ? window : null,
    getWindowRecord: owner => { assert.equal(owner, window); return record; },
    getRemoteDesktop: () => ({ invokes: RemoteDesktop.prototype.invokes }),
    getRuntimeConfig: owner => {
      runtimeOwners.push(owner);
      return { apiBaseUrl, runtimeToken: "owned-runtime-token" };
    },
  });
  moduleBox.exports.registerIpcHandlers();
  let runtime;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({
      ipcRenderer: { on() {}, removeListener() {}, sendSync: () => ({}),
        invoke: async (channel, ...args) => handlers.get(channel)(sender.event, ...args) },
      contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } },
    }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  return { directory, workspace, settingsPath, dataPath, profileHook, pluginHook, hooks, requests, opened,
    state, sender, record, window, runtimeOwners, open: request => runtime.desktop.settings.openTarget(request) };
}

test("real preload opens registered profile and installed settings without granting generic roots or accepting renderer paths", async t => {
  const fixture = await settingsFixture(t);
  const originalRoots = [...security.getTrustedWorkspaceRoots()];
  const cases = [
    [{ kind: "config", workspaceRoot: fixture.workspace }, fixture.settingsPath],
    [{ kind: "data" }, fixture.dataPath],
    [{ kind: "hook", hookKey: "profile-key", workspaceRoot: fixture.workspace }, fixture.profileHook],
    [{ kind: "hook", hookKey: "plugin-key" }, fixture.pluginHook],
  ];
  for (const [request, target] of cases) {
    assert.equal(security.isReadablePath(target), false);
    assert.equal(await fixture.open({ ...request, path: path.join(fixture.workspace, "forged.json"),
      apiBaseUrl: "http://untrusted.invalid", runtimeToken: "renderer-token", runtime: { runtimeToken: "forged" } }), true);
    const receipt = fixture.requests.at(-1);
    assert.equal(receipt.url.pathname, "/api/settings/open-target");
    assert.equal(receipt.token, "owned-runtime-token");
    assert.equal(receipt.url.searchParams.get("workspace_root"), request.workspaceRoot ?? "");
    assert.equal(receipt.url.searchParams.get("kind"), request.kind);
    assert.equal(receipt.url.searchParams.get("hook_key"), request.hookKey ?? null);
    assert.equal(receipt.url.searchParams.has("path"), false);
    assert.equal(receipt.url.searchParams.has("runtimeToken"), false);
    assert.equal(fixture.opened.at(-1), target);
    assert.equal(security.isReadablePath(target), false);
  }
  assert.deepEqual(fixture.runtimeOwners, cases.map(() => fixture.window));
  assert.deepEqual([...security.getTrustedWorkspaceRoots()], originalRoots);
  assert.throws(() => security.assertTrustedPath(fixture.settingsPath), /outside the trusted workspace/);
});

test("removed hook registration rejects through the preload without opening its old source", async t => {
  const fixture = await settingsFixture(t);
  fixture.hooks.delete("plugin-key");
  await assert.rejects(fixture.open({ kind: "hook", hookKey: "plugin-key" }), /Hook is no longer registered/);
  assert.equal(fixture.requests.length, 1);
  assert.deepEqual(fixture.opened, []);
});

test("the authenticated settings catalog's refusal reaches the preload caller", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.status = 403;
  await assert.rejects(fixture.open({ kind: "config" }), /Settings catalog rejected this action/);
  assert.deepEqual(fixture.opened, []);
});

test("a wrong frame cannot reach the settings catalog or shell", async t => {
  const fixture = await settingsFixture(t);
  fixture.sender.event = { ...fixture.sender.event, senderFrame: {} };
  await assert.rejects(fixture.open({ kind: "config" }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.deepEqual(fixture.requests, []);
  assert.deepEqual(fixture.opened, []);
  assert.deepEqual(fixture.runtimeOwners, []);
});

test("the real remote channel policy rejects local opening before any fetch or shell action", async t => {
  const fixture = await settingsFixture(t);
  assert.equal(RemoteDesktop.prototype.invokes("minicode:settings:openTarget"), false);
  fixture.record.remoteHostId = "connected-host";
  await assert.rejects(fixture.open({ kind: "data" }), /Remote settings paths cannot be opened/);
  assert.deepEqual(fixture.requests, []);
  assert.deepEqual(fixture.opened, []);
});

test("an OS shell error string is a rejection through the dedicated preload method", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.shellError = "Windows could not open the registered configuration";
  await assert.rejects(fixture.open({ kind: "config" }), /Windows could not open the registered configuration/);
  assert.deepEqual(fixture.opened, [fixture.settingsPath]);
});

test("an OS shell exception retains its actual error through the dedicated preload method", async t => {
  const fixture = await settingsFixture(t);
  const error = new Error("Shell bridge disconnected");
  fixture.state.shellException = error;
  await assert.rejects(fixture.open({ kind: "hook", hookKey: "profile-key" }), error);
  assert.deepEqual(fixture.opened, [fixture.profileHook]);
});

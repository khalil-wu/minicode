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
  const projectRoot = path.join(directory, "repository");
  const workspace = path.join(projectRoot, "child");
  const projectConfig = path.join(projectRoot, ".minicode", "config.toml");
  const projectScopeId = `project:${projectRoot}`;
  const settingsPath = path.join(directory, "profile", "settings.json");
  const dataPath = path.join(directory, "profile", "data");
  const profileHook = path.join(directory, "profile", "audit.config.toml");
  const managedConfig = path.join(directory, "managed-settings.json");
  const pluginHook = path.join(directory, "installed", "fixture-plugin", ".minicode-plugin", "plugin.json");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(path.dirname(projectConfig));
  fs.writeFileSync(projectConfig, "# authored project\n");
  fs.mkdirSync(dataPath, { recursive: true });
  fs.mkdirSync(path.dirname(pluginHook), { recursive: true });
  for (const target of [settingsPath, profileHook, pluginHook, managedConfig]) fs.writeFileSync(target, "fixture");
  const hooks = new Map([["profile-key", profileHook], ["plugin-key", pluginHook]]);
  const configs = new Map([["user", { path: profileHook }], ["managed", { path: managedConfig }],
    [projectScopeId, { path: projectConfig, workspace_root: projectRoot }]]);
  const importedFiles = new Map([["actual-import\0actual-item", { path: profileHook, workspace_root: "" }]]);
  security.init({ initialRoots: [workspace], readOnlyRoots: [], userOutputRoots: [] });
  const requests = [];
  const opened = [];
  const preferredOwners = [], preferredOpened = [];
  const state = { status: 200, detail: "Settings catalog rejected this action", shellError: "", shellException: null,
    fileTarget: "cursor", targetException: null };
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
    const target = kind === "config" ? url.searchParams.has("scope_id") ? configs.get(url.searchParams.get("scope_id")) : { path: settingsPath }
      : kind === "import-item" ? importedFiles.get(`${url.searchParams.get("import_id")}\0${url.searchParams.get("item_id")}`)
      : kind === "data" ? { path: dataPath } : hooks.has(url.searchParams.get("hook_key")) ? { path: hooks.get(url.searchParams.get("hook_key")) } : null;
    if (!target) response.writeHead(404).end(JSON.stringify({ detail: kind === "config" ? "Configuration scope is no longer available."
      : kind === "import-item" ? "This file is not registered in a successful import result." : "Hook is no longer registered; reload its configuration." }));
    else response.end(JSON.stringify(target));
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
    } : name === "./open-targets" ? { openInTarget: async (opener, target) => {
      preferredOpened.push({ id: opener.id, path: target });
      if (state.targetException) throw state.targetException;
    } } : require(name),
  });
  moduleBox.exports.init({
    getWindowForSender: contents => contents === webContents ? window : null,
    getWindowRecord: owner => { assert.equal(owner, window); return record; },
    getRemoteDesktop: () => ({ invokes: RemoteDesktop.prototype.invokes }),
    desktopPreferences: {
      preferredFileOpenTarget: owner => { preferredOwners.push(owner); return state.fileTarget; },
      fileOpenTarget: async id => [{ id: "cursor", kind: "editor" }, { id: "system", kind: "systemDefault" }].find(target => target.id === id),
    },
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
    managedConfig, projectRoot, projectConfig, projectScopeId, configs, importedFiles, preferredOwners, preferredOpened,
    state, sender, record, window, runtimeOwners, open: request => runtime.desktop.settings.openTarget(request) };
}

test("real preload opens registered profile and installed settings without granting generic roots or accepting renderer paths", async t => {
  const fixture = await settingsFixture(t);
  const originalRoots = [...security.getTrustedWorkspaceRoots()];
  const cases = [
    [{ kind: "config", workspaceRoot: fixture.workspace }, fixture.settingsPath],
    [{ kind: "config", scopeId: "user", workspaceRoot: fixture.workspace }, fixture.profileHook],
    [{ kind: "config", scopeId: "managed", workspaceRoot: fixture.workspace }, fixture.managedConfig],
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
    assert.equal(receipt.url.searchParams.get("scope_id"), request.scopeId ?? null);
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

test("project opening uses the catalog file and its workspace preference without granting its ancestor directory", async t => {
  const fixture = await settingsFixture(t);
  const roots = [...security.getTrustedWorkspaceRoots()];
  const original = fs.readFileSync(fixture.projectConfig, "utf8");
  assert.equal(security.isReadablePath(fixture.projectConfig), false);
  assert.equal(await fixture.open({ kind: "config", scopeId: fixture.projectScopeId, workspaceRoot: fixture.workspace,
    path: fixture.profileHook, targetId: "system", projectWorkspaceRoot: fixture.directory }), true);
  assert.deepEqual(fixture.preferredOwners, [fixture.projectRoot]);
  assert.deepEqual(fixture.preferredOpened, [{ id: "cursor", path: fixture.projectConfig }]);
  assert.deepEqual(fixture.opened, []);
  assert.equal(fs.readFileSync(fixture.projectConfig, "utf8"), original);
  assert.equal(fixture.requests[0].url.searchParams.get("workspace_root"), fixture.workspace);
  assert.equal(fixture.requests[0].url.searchParams.get("scope_id"), fixture.projectScopeId);
  assert.equal(fixture.requests[0].url.searchParams.has("path"), false);
  assert.equal(security.isReadablePath(fixture.projectConfig), false);
  assert.deepEqual([...security.getTrustedWorkspaceRoots()], roots);
});

test("a built-in project target returns only the registered file and owner for the existing editor consumer", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.fileTarget = "minicode";
  const opened = await fixture.open({ kind: "config", scopeId: fixture.projectScopeId, workspaceRoot: fixture.workspace,
    sessionId: "actual-editor-session", conversationId: "" });
  assert.equal(opened.openInEditor, true);
  assert.equal(opened.path, fixture.projectConfig);
  assert.equal(opened.workspaceRoot, fixture.projectRoot);
  assert.deepEqual(fixture.opened, []);
  assert.deepEqual(fixture.preferredOpened, []);
  assert.equal(security.isReadablePath(fixture.projectConfig), false);
  assert.equal(fixture.requests[0].url.searchParams.get("session_id"), "actual-editor-session");
  assert.equal(fixture.requests[0].url.searchParams.get("conversation_id"), "");
});

test("removed or arbitrary project identities cannot reach a file opener", async t => {
  const fixture = await settingsFixture(t);
  fixture.configs.delete(fixture.projectScopeId);
  for (const scopeId of [fixture.projectScopeId, `project:${fixture.directory}`]) {
    await assert.rejects(fixture.open({ kind: "config", scopeId, workspaceRoot: fixture.workspace }), /scope is no longer available/);
  }
  assert.deepEqual(fixture.preferredOwners, []);
  assert.deepEqual(fixture.preferredOpened, []);
  assert.deepEqual(fixture.opened, []);
});

test("a missing preferred application is an error rather than a different target", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.fileTarget = "removed-editor";
  await assert.rejects(fixture.open({ kind: "config", scopeId: fixture.projectScopeId, workspaceRoot: fixture.workspace }), /application is no longer installed/);
  assert.deepEqual(fixture.preferredOpened, []);
  assert.deepEqual(fixture.opened, []);
});

test("project opener failures retain the original error", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.targetException = new Error("Configured editor could not start");
  await assert.rejects(fixture.open({ kind: "config", scopeId: fixture.projectScopeId, workspaceRoot: fixture.workspace }), fixture.state.targetException);
  assert.deepEqual(fixture.preferredOpened, [{ id: "cursor", path: fixture.projectConfig }]);
  assert.deepEqual(fixture.opened, []);
});

test("an imported file uses only its actual ledger identity and the global saved file target", async t => {
  const fixture = await settingsFixture(t);
  const roots = [...security.getTrustedWorkspaceRoots()];
  assert.equal(await fixture.open({ kind: "import-item", importId: "actual-import", itemId: "actual-item",
    workspaceRoot: fixture.workspace, sessionId: "actual-editor-session", conversationId: "",
    path: fixture.settingsPath, targetId: "system", projectWorkspaceRoot: fixture.directory }), true);
  const receipt = fixture.requests[0].url;
  assert.equal(receipt.searchParams.get("kind"), "import-item");
  assert.equal(receipt.searchParams.get("import_id"), "actual-import");
  assert.equal(receipt.searchParams.get("item_id"), "actual-item");
  assert.equal(receipt.searchParams.get("session_id"), "actual-editor-session");
  assert.equal(receipt.searchParams.get("conversation_id"), "");
  assert.equal(receipt.searchParams.has("path"), false);
  assert.deepEqual(fixture.preferredOwners, [undefined]);
  assert.deepEqual(fixture.preferredOpened, [{ id: "cursor", path: fixture.profileHook }]);
  assert.deepEqual([...security.getTrustedWorkspaceRoots()], roots);
  assert.equal(security.isReadablePath(fixture.profileHook), false);
});

test("an imported built-in file returns the same registered editor receipt while removed identities fail", async t => {
  const fixture = await settingsFixture(t);
  fixture.state.fileTarget = "minicode";
  const request = { kind: "import-item", importId: "actual-import", itemId: "actual-item", workspaceRoot: fixture.workspace,
    sessionId: "actual-editor-session", conversationId: "" };
  const opened = await fixture.open(request);
  assert.equal(opened.openInEditor, true);
  assert.equal(opened.path, fixture.profileHook);
  assert.equal(opened.workspaceRoot, "");
  fixture.importedFiles.clear();
  await assert.rejects(fixture.open(request), /not registered in a successful import result/);
  assert.deepEqual(fixture.preferredOpened, []);
  assert.deepEqual(fixture.opened, []);
});

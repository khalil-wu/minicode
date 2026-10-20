"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const vm = require("node:vm");
const security = require("./security");

test("Skill reveal crosses the real preload and authenticated catalog check without granting a readable root", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-skill-reveal-"));
  const workspace = path.join(directory, "workspace");
  const skillPath = path.join(directory, "plugin", "SKILL.md");
  fs.mkdirSync(workspace);
  fs.mkdirSync(path.dirname(skillPath));
  fs.writeFileSync(skillPath, "---\nname: fixture\ndescription: Catalog fixture\n---\nOriginal body.\n");
  t.after(() => {
    assert.equal(path.dirname(path.resolve(directory)), path.resolve(os.tmpdir()));
    fs.rmSync(directory, { recursive: true });
  });
  security.init({ initialRoots: [workspace], readOnlyRoots: [], userOutputRoots: [] });
  const requests = [];
  let returnedPath = skillPath;
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://fixture");
    requests.push({ url, token: request.headers["x-minicode-token"] });
    assert.equal(url.pathname, "/api/skills/detail");
    assert.equal(request.headers["x-minicode-token"], "owned-runtime-token");
    assert.equal(url.searchParams.get("workspace_root"), workspace);
    response.setHeader("Content-Type", "application/json");
    if (url.searchParams.get("skill_path") !== skillPath) {
      response.statusCode = 404;
      response.end(JSON.stringify({ detail: "Skill is not registered" }));
    } else response.end(JSON.stringify({ path: returnedPath }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const apiBaseUrl = `http://127.0.0.1:${server.address().port}`;
  const handlers = new Map();
  const revealed = [];
  const mainFrame = {};
  const webContents = { mainFrame };
  const window = { webContents, isDestroyed: () => false };
  const record = { windowInstanceId: "skill-window" };
  const event = { sender: webContents, senderFrame: mainFrame };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console, URL, fetch,
    require: name => name === "electron" ? { ipcMain: { on() {}, handle: (name, handler) => handlers.set(name, handler) },
      shell: { showItemInFolder: target => revealed.push(target) } } : require(name),
  });
  moduleBox.exports.init({ getWindowForSender: sender => sender === webContents ? window : null,
    getWindowRecord: () => record, getRemoteDesktop: () => ({ invokes: () => false }),
    getRuntimeConfig: target => {
      assert.equal(target, window);
      return { apiBaseUrl, runtimeToken: "owned-runtime-token" };
    } });
  moduleBox.exports.registerIpcHandlers();
  let runtime;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({ ipcRenderer: { on() {}, removeListener() {}, sendSync: () => ({}),
      invoke: async (channel, ...args) => handlers.get(channel)(event, ...args) },
      contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } } }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  assert.equal(security.isReadablePath(skillPath), false);
  assert.equal(await runtime.desktop.skills.reveal({ path: skillPath, workspaceRoot: workspace,
    apiBaseUrl: "http://untrusted.invalid", runtimeToken: "renderer-token" }), true);
  assert.deepEqual(revealed, [skillPath]);
  assert.equal(requests.length, 1);
  assert.equal(security.isReadablePath(skillPath), false);
  await assert.rejects(runtime.desktop.skills.reveal({ path: path.join(directory, "other.md"), workspaceRoot: workspace }), /Skill is not registered/);
  returnedPath = path.join(directory, "wrong.md");
  await assert.rejects(runtime.desktop.skills.reveal({ path: skillPath, workspaceRoot: workspace }), /did not return the requested file/);
  assert.deepEqual(revealed, [skillPath]);
  const beforeRemote = requests.length;
  record.remoteHostId = "remote-host";
  await assert.rejects(runtime.desktop.skills.reveal({ path: skillPath, workspaceRoot: workspace }), /remote host/);
  assert.equal(requests.length, beforeRemote);
  assert.throws(() => handlers.get("minicode:skills:reveal")({ sender: webContents, senderFrame: {} }, { path: skillPath, workspaceRoot: workspace }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.deepEqual(revealed, [skillPath]);
});

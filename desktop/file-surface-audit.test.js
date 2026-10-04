"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const security = require("./security");

function fileIpcHarness() {
  const handlers = new Map();
  const trashed = [];
  const webContents = { mainFrame: {} };
  const win = { isDestroyed: () => false, webContents };
  const mod = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: mod, process, console,
    require(name) {
      if (name === "electron") return {
        ipcMain: { handle: (name, handler) => handlers.set(name, handler), on() {} },
        shell: { trashItem: async (target) => { trashed.push(target); } },
      };
      if (name === "./utils") return { ...require("./utils") };
      return require(name);
    },
  }, { filename: "ipc-handlers.js" });
  mod.exports.init({ getMainWindow: () => win });
  mod.exports.registerIpcHandlers();
  const event = { sender: webContents, senderFrame: webContents.mainFrame };
  return { handlers, event, trashed };
}

test("the desktop exposes file reads and trash while creation and editing use the backend", async () => {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-desktop-surface-"));
  const file = path.join(workspace, "note.txt");
  fs.writeFileSync(file, "desktop file read\n", "utf8");
  security.init({ initialRoots: [workspace], readOnlyRoots: [], userOutputRoots: [] });
  const { handlers, event, trashed } = fileIpcHarness();
  const result = await handlers.get("minicode:fs:readFile")(event, file);
  assert.equal(result.content, "desktop file read\n");
  assert.equal(result.path, file);
  assert.equal(result.read_only, false);
  assert.equal(typeof handlers.get("minicode:fs:deletePath"), "function");
  for (const operation of ["writeFile", "compareWriteFile", "createDirectory", "renamePath"]) {
    const channel = `minicode:fs:${operation}`;
    assert.equal(handlers.has(channel), false);
    assert.throws(() => security.assertIpcCapability(channel), /Undeclared IPC capability/);
  }
  assert.throws(() => handlers.get("minicode:fs:readFile")({ sender: {}, senderFrame: {} }, file), /Untrusted IPC sender/);
  const deletion = await handlers.get("minicode:fs:deletePath")(event, file, false, false);
  assert.equal(deletion.deleted, true);
  assert.deepEqual(trashed, [file]);
  assert.equal(fs.existsSync(file), true);
  fs.unlinkSync(file);
  fs.rmdirSync(workspace);
});

test("restored HTML and Markdown tabs read as project files before the file tree mounts", async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-first-read-"));
  const workspace = path.join(tempRoot, "workspace");
  const outside = path.join(tempRoot, "outside");
  fs.mkdirSync(workspace);
  fs.mkdirSync(outside);
  const trustedRootsFile = path.join(tempRoot, "trusted_workspaces.json");
  fs.writeFileSync(trustedRootsFile, JSON.stringify({ version: 1, roots: [workspace] }));
  const { handlers, event } = fileIpcHarness();
  for (const filename of ["index.html", "README.md"]) {
    const file = path.join(workspace, filename);
    fs.writeFileSync(file, "restored project content", "utf8");
    security.init({ initialRoots: [], userOutputRoots: [tempRoot], trustedRootsFile });
    assert.equal(security.isWithinTrustedWorkspace(file), false);
    const result = await handlers.get("minicode:fs:readFile")(event, file);
    assert.equal(result.content, "restored project content");
    assert.equal(result.read_only, false);
  }

  const unapproved = path.join(outside, "index.html");
  fs.writeFileSync(unapproved, "unapproved content", "utf8");
  await assert.rejects(handlers.get("minicode:fs:readFile")(event, unapproved), /outside the trusted workspace/);
  assert.equal(security.isWithinTrustedWorkspace(outside), false);
});

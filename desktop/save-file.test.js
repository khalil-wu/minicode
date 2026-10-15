"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("native Save as writes exact original bytes only after the owned dialog selects a destination", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-native-save-"));
  const handlers = new Map();
  const moduleBox = { exports: {} };
  let choice = { canceled: false, filePath: path.join(root, "selected.bin") };
  const dialogCalls = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console, Uint8Array,
    require(name) {
      if (name === "electron") return {
        ipcMain: { on() {}, handle(channel, handler) { handlers.set(channel, handler); } },
        app: { getPath: () => root },
        dialog: { showSaveDialog: async (owner, options) => { dialogCalls.push({ owner, options }); return choice; } },
      };
      return require(name);
    },
  });
  const mainFrame = { url: "file:///minicode/index.html" };
  const webContents = { mainFrame, getURL: () => mainFrame.url };
  const win = { webContents, isDestroyed: () => false };
  moduleBox.exports.init({ getWindowForSender: sender => sender === webContents ? win : null, getWindowRecord: () => ({ windowInstanceId: "native-save-owner" }) });
  moduleBox.exports.registerIpcHandlers();
  const save = handlers.get("minicode:saveFile");
  const owner = { sender: webContents, senderFrame: mainFrame };
  const bytes = new Uint8Array([0, 255, 13, 10, 65, 0]);
  assert.equal(await save(owner, "original.bin", bytes), true);
  assert.deepEqual(fs.readFileSync(choice.filePath), Buffer.from(bytes));
  assert.equal(dialogCalls[0].owner, win);
  assert.equal(dialogCalls[0].options.defaultPath, path.join(root, "original.bin"));
  const savedPath = choice.filePath;
  choice = { canceled: true, filePath: savedPath };
  assert.equal(await save(owner, "original.bin", new Uint8Array([99])), false);
  assert.deepEqual(fs.readFileSync(savedPath), Buffer.from(bytes));
  assert.throws(() => save({ sender: webContents, senderFrame: { url: mainFrame.url } }, "foreign.bin", bytes), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.equal(dialogCalls.length, 2);
  await assert.rejects(save(owner, "original.bin", "pretend bytes"), /original bytes/);
  choice = { canceled: false, filePath: root };
  await assert.rejects(save(owner, "original.bin", bytes));
});

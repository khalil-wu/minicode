"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { closeWindowsAfterDraftCommit } = require("./window-lifecycle");
const { RealtimeVoiceManager } = require("./realtime-voice-manager");

test("transition close waits for each real closed event after the existing draft barrier resumes", async () => {
  const events = [];
  const window = new EventEmitter();
  window.isDestroyed = () => false;
  window.close = () => { events.push("close-requested"); };
  let completed = false;
  const closing = closeWindowsAfterDraftCommit([window]).then(() => { completed = true; events.push("barrier-finished"); });
  await Promise.resolve();
  assert.equal(completed, false);
  events.push("draft-transaction-committed");
  window.emit("closed");
  await closing;
  assert.deepEqual(events, ["close-requested", "draft-transaction-committed", "barrier-finished"]);
});

test("actual main drain retains a failed closed-window operation and retries it before stopping the backend", async () => {
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const release = source.slice(source.indexOf("const windowCleanupTasks ="), source.indexOf("async function launchManagedBackend"));
  const drain = source.slice(source.indexOf("async function drainDesktopResources()"), source.indexOf("async function prepareAutomaticRollback()"));
  const events = [];
  let attempts = 0;
  const context = {
    module: { exports: {} }, Promise, RealtimeVoiceManager,
    remoteDesktop: null,
    dictationHotkeys: { async dispose() { events.push("dictation-stopped"); } },
    generalMaintenance: null, petManager: null, petInitialization: null,
    EMBEDDED_BROWSER_TOKEN: "owned-fixture-token",
    appendDesktopLog(message) { events.push(message); },
    windowManager: { clearWindowStateSaveTimer() {}, persistWindowState() {} },
    embeddedBrowserBridge: { releaseWindow() {}, async stop() { events.push("bridge-stopped"); } },
    embeddedBrowserManager: { releaseWindow() {}, disposeAll() {} },
    ptyManager: { async releaseWindow() { attempts++; if (attempts === 1) throw new Error("terminal has not reported exit"); events.push("window-pty-exited"); },
      async killAllSessions() { events.push("all-pty-exited"); } },
    async releaseEditorWindow() { events.push("editor-released"); },
    backendSidecar: { async stopBackendSidecar() { events.push("backend-exited"); }, getBackendProcess: () => null },
  };
  vm.runInNewContext(`${release}\n${drain}\nmodule.exports={releaseWindowResources,drainDesktopResources,windowCleanupTasks};`, context);
  const lifecycle = context.module.exports;
  const record = { windowOwnerId: "primary", windowInstanceId: "owned-window", runtime: {} };
  await assert.rejects(lifecycle.releaseWindowResources(record), /has not reported exit/);
  assert.equal(lifecycle.windowCleanupTasks.size, 1);
  await assert.rejects(lifecycle.drainDesktopResources(), /has not reported exit/);
  assert.equal(events.includes("backend-exited"), false);
  await lifecycle.drainDesktopResources();
  assert.equal(attempts, 2);
  assert.equal(lifecycle.windowCleanupTasks.size, 0);
  assert.ok(events.indexOf("window-pty-exited") < events.indexOf("backend-exited"));
  assert.ok(events.indexOf("dictation-stopped") < events.indexOf("backend-exited"));
});

test("actual rollback preparation orders committed closes, backend exit and trusted compatibility CLI", async () => {
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const prepare = source.slice(source.indexOf("async function prepareAutomaticRollback()"), source.indexOf('app.on("before-quit"'));
  const events = [];
  const context = { module: { exports: {} }, path, process: { platform: "win32", resourcesPath: "current-resources" },
    rollbackPreparationInProgress: false, PYTHON_COMMAND: "must-not-select-user-override",
    app: { isPackaged: true, getPath: () => "current-user-data" },
    backendSidecar: { requestBackendStop() { events.push("restarts-disabled"); } },
    windowManager: { getMainWindows: () => ["owned-window"] },
    async closeWindowsAfterDraftCommit() { events.push("drafts-committed-and-windows-closed"); },
    async drainDesktopResources() { events.push("all-owned-resources-exited"); },
    getAppRoot: () => "current-resources",
    updater: { async prepareRollbackData(options) { events.push("cli"); assert.equal(options.pythonCommand, path.join("current-resources", "python-runtime", "python.exe"));
      assert.equal(options.stateRoot, "current-user-data"); return { ok: true }; } },
  };
  vm.runInNewContext(`${prepare}\nmodule.exports=prepareAutomaticRollback;`, context);
  assert.equal((await context.module.exports()).ok, true);
  assert.deepEqual(events, ["restarts-disabled", "drafts-committed-and-windows-closed", "all-owned-resources-exited", "cli"]);
  assert.equal(context.rollbackPreparationInProgress, true);
});

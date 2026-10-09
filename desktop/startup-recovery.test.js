"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Execute the production orchestrator with its process/window dependencies.
const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8")
  .match(/async function restartManagedBackend\(reason\) \{[\s\S]*?\n\}/)[0];

function recovery({ mainWindow = null, failureWindow = {}, launchError } = {}) {
  const calls = [];
  let currentWindow = mainWindow;
  const previousRuntime = { apiBaseUrl: "http://127.0.0.1:8000" };
  const nextRuntime = { apiBaseUrl: "http://127.0.0.1:8001" };
  const context = {
    MANAGE_BACKEND: true,
    startupFailureWindow: failureWindow,
    appendDesktopLog: () => {},
    getBackendRuntimeConfig: () => previousRuntime,
    launchManagedBackend: async () => { calls.push("launch"); if (launchError) throw launchError; return nextRuntime; },
    publishBackendRuntimeChange: (previous, next) => {
      assert.equal(previous, previousRuntime);
      assert.equal(next, nextRuntime);
      calls.push("publish");
    },
    windowManager: {
      getMainWindow: () => currentWindow,
      createMainWindow: async () => { calls.push("create"); currentWindow = { ready: true }; },
    },
    closeStartupFailureWindow: () => {
      assert.ok(currentWindow?.ready);
      calls.push("close-error");
      context.startupFailureWindow = null;
    },
  };
  const restart = vm.runInNewContext(`${source}\nrestartManagedBackend;`, context);
  return { restart, calls, context, getWindow: () => currentWindow };
}

test("a ready restarted sidecar replaces the startup error surface with the main app", async () => {
  const audit = recovery();
  await audit.restart("startup import failure");
  assert.deepEqual(audit.calls, ["launch", "publish", "create", "close-error"]);
  assert.equal(audit.context.startupFailureWindow, null);
  assert.equal(audit.getWindow().ready, true);
});

test("restarting a backend preserves an existing main window and its unsent draft", async () => {
  const mainWindow = { unsentDraft: "Keep this draft" };
  const audit = recovery({ mainWindow });
  await audit.restart("process exit");
  assert.deepEqual(audit.calls, ["launch", "publish"]);
  assert.equal(audit.getWindow(), mainWindow);
  assert.equal(mainWindow.unsentDraft, "Keep this draft");
});

test("a failed restart retains the original failure surface and propagates the real error", async () => {
  const failureWindow = {};
  const launchError = new Error("Backend import failed");
  const audit = recovery({ failureWindow, launchError });
  await assert.rejects(audit.restart("process exit"), (error) => error === launchError);
  assert.deepEqual(audit.calls, ["launch"]);
  assert.equal(audit.context.startupFailureWindow, failureWindow);
  assert.equal(audit.getWindow(), null);
});

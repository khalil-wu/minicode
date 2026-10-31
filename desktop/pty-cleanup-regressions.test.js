"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

test("timed-out PTY termination retains its session, permits retry and blocks restart", async () => {
  let exitHandler;
  let killCalls = 0;
  let spawnCalls = 0;
  const fakePty = {
    pid: 123456,
    onData() {},
    onExit(fn) { exitHandler = fn; },
    kill() { killCalls += 1; },
    forceKill() { return this.kill(); },
  };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "pty-manager.js"), "utf8"), {
    module: moduleBox,
    require(name) {
      if (name === "node:child_process") return {
        spawn() {
          const proc = new EventEmitter();
          queueMicrotask(() => proc.emit("close", 0));
          return proc;
        },
      };
      return require(name);
    },
    process: { platform: "win32", env: {}, cwd: () => __dirname },
    setTimeout, clearTimeout, console,
  });
  const manager = moduleBox.exports;
  manager.init({
    pty: { spawn() { spawnCalls += 1; return fakePty; } },
    assertTrustedPath: (value) => value,
    killExitTimeoutMs: 1,
    forceKillExitTimeoutMs: 1,
    appendDesktopLog() {},
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });
  const session = manager.spawnSession(__dirname, "owned-fixture", "fixture-window");
  assert.equal(await manager.killSession(session.session_id, "other-owner", "fixture-window"), false);
  assert.equal(await manager.killSession(session.session_id, "owned-fixture", "other-window"), false);
  assert.equal(killCalls, 0);
  assert.equal(await manager.killSession(session.session_id, "owned-fixture", "fixture-window"), false);
  assert.notEqual(manager.snapshotSession(session.session_id, 10, "owned-fixture", "fixture-window"), null);
  assert.equal(await manager.restartSession(session.session_id, "owned-fixture", "fixture-window"), null);
  assert.equal(spawnCalls, 1);
  assert.equal(killCalls, 4);
  await assert.rejects(manager.killAllSessions(), /Terminal cleanup did not finish/);
  assert.equal(manager.listActiveSessions().length, 1);
  fakePty.kill = () => { throw new Error("Native PTY stop failure"); };
  await assert.rejects(manager.killSession(session.session_id, "owned-fixture", "fixture-window"), /Native PTY stop failure/);
  assert.notEqual(manager.snapshotSession(session.session_id, 10, "owned-fixture", "fixture-window"), null);
  fakePty.kill = () => exitHandler({ exitCode: 0 });
  await manager.killAllSessions();
  assert.equal(manager.snapshotSession(session.session_id, 10, "owned-fixture", "fixture-window"), null);
});

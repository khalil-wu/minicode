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
  });
  const session = manager.spawnSession(__dirname, "owned-fixture");
  assert.equal(await manager.killSession(session.session_id, "owned-fixture"), false);
  assert.notEqual(manager.snapshotSession(session.session_id, 10, "owned-fixture"), null);
  assert.equal(await manager.restartSession(session.session_id, "owned-fixture"), null);
  assert.equal(spawnCalls, 1);
  assert.equal(killCalls, 2);
  fakePty.kill = () => exitHandler({ exitCode: 0 });
  assert.equal(await manager.killSession(session.session_id, "owned-fixture"), true);
  assert.equal(manager.snapshotSession(session.session_id, 10, "owned-fixture"), null);
});

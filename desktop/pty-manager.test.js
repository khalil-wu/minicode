"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const manager = require("./pty-manager");

test("pty snapshots expose monotonic cursors for lossless renderer hydration", () => {
  let onData;
  let onExit;
  const sent = [];
  manager.init({
    pty: {
      spawn: () => ({
        pid: 42,
        process: "pwsh",
        write: () => {},
        resize: () => {},
        kill: () => {},
        onData: (callback) => { onData = callback; },
        onExit: (callback) => { onExit = callback; },
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => ({
      isDestroyed: () => false,
      webContents: { send: (channel, payload) => sent.push({ channel, payload }) },
    }),
  });

  const conversationId = "conv_terminal_owner";
  const spawned = manager.spawnSession("C:\\workspace", conversationId);
  onData("hello");
  onData(" world");

  assert.deepEqual(manager.listActiveSessions(), [{
    session_id: spawned.session_id,
    conversation_id: conversationId,
  }]);

  const snapshot = manager.snapshotSession(spawned.session_id, 7, conversationId);
  assert.equal(snapshot.output, "o world");
  assert.equal(snapshot.output_start_cursor, 4);
  assert.equal(snapshot.output_end_cursor, 11);
  assert.equal(snapshot.total_output_chars, 11);
  assert.deepEqual(sent.filter((entry) => entry.channel === "minicode:pty:data").map((entry) => entry.payload), [
    { sessionId: spawned.session_id, conversationId, data: "hello", startCursor: 0, endCursor: 5 },
    { sessionId: spawned.session_id, conversationId, data: " world", startCursor: 5, endCursor: 11 },
  ]);

  assert.deepEqual(manager.clearSession(spawned.session_id, "conv-other"), {
    cleared: false,
    outputCursor: 0,
  });
  assert.deepEqual(manager.clearSession(spawned.session_id, conversationId), {
    cleared: true,
    outputCursor: 11,
  });
  const cleared = manager.snapshotSession(spawned.session_id, undefined, conversationId);
  assert.equal(cleared.output, "");
  assert.equal(cleared.output_start_cursor, 11);
  assert.equal(cleared.output_end_cursor, 11);
  onData("next");
  const afterClear = manager.snapshotSession(spawned.session_id, undefined, conversationId);
  assert.equal(afterClear.output, "next");
  assert.equal(afterClear.output_start_cursor, 11);
  assert.equal(afterClear.output_end_cursor, 15);

  onExit({ exitCode: 0 });
  assert.deepEqual(manager.listActiveSessions(), []);
  const exited = manager.snapshotSession(spawned.session_id, undefined, conversationId);
  assert.equal(exited.is_alive, false);
  assert.equal(exited.exit_code, 0);
  assert.match(exited.output, /Process exited with code 0/);
  assert.deepEqual(sent.filter((entry) => entry.channel === "minicode:pty:exit").map((entry) => entry.payload), [
    {
      sessionId: spawned.session_id,
      conversationId,
      exitCode: 0,
      exitSignal: null,
      exitedAt: exited.exited_at,
    },
  ]);
  assert.equal(manager.listSessions(conversationId).some((session) => session.session_id === spawned.session_id), true);
  assert.equal(manager.acknowledgeExitedSession(spawned.session_id, conversationId), true);
  assert.equal(manager.snapshotSession(spawned.session_id, undefined, conversationId), null);
});

test("pty tombstones expire and stay bounded", () => {
  const exits = [];
  const originalNow = Date.now;
  let now = 1_000;
  Date.now = () => now;
  manager.init({
    pty: {
      spawn: () => ({
        pid: 43,
        process: "pwsh",
        write: () => {},
        resize: () => {},
        kill: () => {},
        onData: () => {},
        onExit: (callback) => exits.push(callback),
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => null,
  });
  try {
    const ids = [];
    for (let index = 0; index < 27; index += 1) {
      ids.push(manager.spawnSession("C:\\workspace", "conv_tombstones").session_id);
      now += 1;
      exits[index]({ exitCode: index });
    }
    assert.equal(manager.listSessions("conv_tombstones").length, 24);
    assert.equal(manager.snapshotSession(ids[0], undefined, "conv_tombstones"), null);
    assert.notEqual(manager.snapshotSession(ids.at(-1), undefined, "conv_tombstones"), null);

    now += 31 * 60 * 1000;
    manager.pruneExitedSessions(now);
    assert.equal(manager.listSessions("conv_tombstones").length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("large pty writes are chunked without truncating pasted input", async () => {
  const writes = [];
  manager.init({
    pty: {
      spawn: () => ({
        pid: 0,
        process: "pwsh",
        write: (value) => writes.push(value),
        resize: () => {},
        kill: () => {},
        onData: () => {},
        onExit: () => {},
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => null,
  });

  const conversationId = "conv_large_write";
  const spawned = manager.spawnSession("C:\\workspace", conversationId);
  const pasted = "x".repeat(20_000);
  assert.equal(manager.writeToSession(spawned.session_id, pasted, conversationId), true);
  assert.deepEqual(writes.map((value) => value.length), [8192, 8192, 3616]);
  assert.equal(writes.join(""), pasted);
  await manager.killConversation(conversationId);
});

test("pty ownership is fail-closed for list, snapshot, input, resize, kill, and ack", async () => {
  const writes = [];
  const resizes = [];
  const exits = [];
  manager.init({
    pty: {
      spawn: () => ({
        pid: 0,
        process: "pwsh",
        write: (value) => writes.push(value),
        resize: (cols, rows) => resizes.push([cols, rows]),
        kill: () => {},
        onData: () => {},
        onExit: (callback) => exits.push(callback),
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => null,
  });

  assert.throws(
    () => manager.spawnSession("C:\\workspace", ""),
    /conversation owner is required/i,
  );

  const owned = manager.spawnSession("C:\\workspace", "conv_owner_a");
  assert.equal(manager.listSessions("").length, 0);
  assert.equal(manager.listSessions("conv_owner_b").length, 0);
  assert.equal(manager.snapshotSession(owned.session_id, undefined, "conv_owner_b"), null);
  assert.equal(manager.writeToSession(owned.session_id, "blocked", "conv_owner_b"), false);
  manager.resizeSession(owned.session_id, 120, 40, "conv_owner_b");
  assert.deepEqual(writes, []);
  assert.deepEqual(resizes, []);
  assert.equal(await manager.killSession(owned.session_id, "conv_owner_b"), false);
  assert.equal(manager.listSessions("conv_owner_a").length, 1);

  exits[0]({ exitCode: 0 });
  assert.equal(manager.acknowledgeExitedSession(owned.session_id, "conv_owner_b"), false);
  assert.equal(manager.acknowledgeExitedSession(owned.session_id, "conv_owner_a"), true);
});

test("killConversation terminates only sessions owned by that conversation", async () => {
  const killed = [];
  let pid = 0;
  manager.init({
    pty: {
      spawn: () => {
        const current = pid++;
        return {
          pid: 0,
          process: "pwsh",
          write: () => {},
          resize: () => {},
          kill: () => killed.push(current),
          onData: () => {},
          onExit: () => {},
        };
      },
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => null,
  });

  manager.spawnSession("C:\\workspace", "conv_cleanup_a");
  manager.spawnSession("C:\\workspace", "conv_cleanup_a");
  manager.spawnSession("C:\\workspace", "conv_cleanup_b");

  assert.equal(await manager.killConversation("conv_cleanup_a"), 2);
  assert.deepEqual(killed, [0, 1]);
  assert.equal(manager.listSessions("conv_cleanup_a").length, 0);
  assert.equal(manager.listSessions("conv_cleanup_b").length, 1);

  assert.equal(await manager.killConversation("conv_cleanup_b"), 1);
  assert.deepEqual(killed, [0, 1, 2]);
});

test("restartSession atomically replaces an exited owned session", async () => {
  const exits = [];
  let pid = 40;
  manager.init({
    pty: {
      spawn: () => ({
        pid: pid++,
        process: "pwsh",
        write: () => {},
        resize: () => {},
        kill: () => {},
        onData: () => {},
        onExit: (callback) => exits.push(callback),
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getMainWindow: () => null,
  });

  const original = manager.spawnSession("C:\\workspace", "conv_restart");
  exits[0]({ exitCode: 0 });

  assert.equal(await manager.restartSession(original.session_id, "conv_other"), null);
  const replacement = await manager.restartSession(original.session_id, "conv_restart");

  assert.notEqual(replacement.session_id, original.session_id);
  assert.equal(replacement.cwd, "C:\\workspace");
  assert.deepEqual(
    manager.listSessions("conv_restart").map((session) => session.session_id),
    [replacement.session_id],
  );
});

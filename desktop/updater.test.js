"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  acceptUpdateActivitySnapshot,
  attemptAutomaticRollback,
  beginBootHealthCheck,
  buildUpdatePreflight,
  createRollbackBackup,
  executeInstallTransaction,
  expectedRollbackDirectory,
  normalizeUpdateActivitySnapshot,
  readHealthState,
  resolveRollbackExecutable,
  validatedFeedUrl,
  writeHealthState,
} = require("./updater");

function emptyActivity(revision = 1) {
  return {
    revision,
    rendererInstanceId: "renderer-test-instance",
    runtimeReady: true,
    activeTurns: [],
    sideChatStreams: [],
    pendingPrompts: [],
    uploadingAttachments: [],
    dirtyEditors: [],
    backgroundTasks: [],
  };
}

test("update feeds require HTTPS and reject embedded credentials", () => {
  assert.equal(validatedFeedUrl("https://updates.example.com/minicode/"), "https://updates.example.com/minicode");
  assert.throws(() => validatedFeedUrl("http://updates.example.com/minicode"), /must use HTTPS/);
  assert.throws(() => validatedFeedUrl("https://user:pass@updates.example.com/minicode"), /cannot contain credentials/);
});

test("update feed host allowlist is enforced when configured", () => {
  const previous = process.env.MINICODE_UPDATE_ALLOWED_HOSTS;
  process.env.MINICODE_UPDATE_ALLOWED_HOSTS = "updates.example.com";
  try {
    assert.equal(validatedFeedUrl("https://updates.example.com/minicode"), "https://updates.example.com/minicode");
    assert.throws(() => validatedFeedUrl("https://other.example.com/minicode"), /not allowlisted/);
  } finally {
    if (previous === undefined) delete process.env.MINICODE_UPDATE_ALLOWED_HOSTS;
    else process.env.MINICODE_UPDATE_ALLOWED_HOSTS = previous;
  }
});

test("update activity snapshots are strict, canonical, and monotonic", () => {
  const normalized = normalizeUpdateActivitySnapshot({
    ...emptyActivity(10),
    activeTurns: ["turn-b", "turn-a", "turn-a"],
  });
  assert.deepEqual(normalized.activeTurns, ["turn-a", "turn-b"]);
  assert.throws(
    () => normalizeUpdateActivitySnapshot({ ...emptyActivity(11), dirtyEditors: undefined }),
    /dirtyEditors must be an array/,
  );
  assert.throws(
    () => normalizeUpdateActivitySnapshot({ ...emptyActivity(11), pendingPrompts: [""] }),
    /invalid identifier/,
  );

  const accepted = acceptUpdateActivitySnapshot(null, emptyActivity(20));
  assert.equal(accepted.accepted, true);
  const stale = acceptUpdateActivitySnapshot(accepted.snapshot, {
    ...emptyActivity(19),
    dirtyEditors: ["C:\\workspace\\unsaved.ts"],
  });
  assert.equal(stale.accepted, false);
  assert.deepEqual(stale.snapshot, accepted.snapshot);
  const replacementRenderer = acceptUpdateActivitySnapshot(accepted.snapshot, {
    ...emptyActivity(1),
    rendererInstanceId: "renderer-reloaded-instance",
  });
  assert.equal(replacementRenderer.accepted, true);
});

test("update preflight fails closed for unknown or active work and fingerprints authoritative state", () => {
  const unknown = buildUpdatePreflight({ updateReady: true, readyVersion: "2.0.0" });
  assert.equal(unknown.allowed, false);
  assert.equal(unknown.checks.some((check) => check.code === "activity.unknown"), true);

  const restoring = buildUpdatePreflight({
    activity: { ...emptyActivity(1), runtimeReady: false },
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(restoring.allowed, false);
  assert.equal(restoring.checks.some((check) => check.code === "runtime.not_ready"), true);

  const blocked = buildUpdatePreflight({
    activity: {
      ...emptyActivity(2),
      activeTurns: ["conv-running"],
      pendingPrompts: ["approval-1"],
      dirtyEditors: ["C:\\workspace\\dirty.ts"],
    },
    activePtys: [{ session_id: "term-1", conversation_id: "conv-terminal" }],
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(blocked.allowed, false);
  assert.deepEqual(
    blocked.checks.filter((check) => check.severity === "blocking").map((check) => check.code),
    ["turn.running", "prompt.pending", "editor.dirty", "pty.running"],
  );

  const clear = buildUpdatePreflight({
    activity: emptyActivity(3),
    activePtys: [],
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(clear.allowed, true);
  const reordered = buildUpdatePreflight({
    activity: { ...emptyActivity(3), activeTurns: [] },
    activePtys: [],
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(reordered.fingerprint, clear.fingerprint);
  const newer = buildUpdatePreflight({
    activity: emptyActivity(4),
    activePtys: [],
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(newer.fingerprint, clear.fingerprint);
  const changedWork = buildUpdatePreflight({
    activity: { ...emptyActivity(5), dirtyEditors: ["C:\\workspace\\dirty.ts"] },
    activePtys: [],
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.notEqual(changedWork.fingerprint, clear.fingerprint);

  const staleActivity = buildUpdatePreflight({
    activity: emptyActivity(6),
    activityStale: true,
    updateReady: true,
    readyVersion: "2.0.0",
  });
  assert.equal(staleActivity.allowed, false);
  assert.equal(staleActivity.checks.some((check) => check.code === "activity.stale"), true);
});

test("update install transaction rechecks the fingerprint after backup", async () => {
  const initial = buildUpdatePreflight({
    activity: emptyActivity(10),
    updateReady: true,
    readyVersion: "2.0.0",
  });
  const changed = buildUpdatePreflight({
    activity: { ...emptyActivity(11), activeTurns: ["conv-started-during-backup"] },
    updateReady: true,
    readyVersion: "2.0.0",
  });
  let readCount = 0;
  let committed = false;
  const result = await executeInstallTransaction({
    fingerprint: initial.fingerprint,
    readPreflight: () => (readCount++ === 0 ? initial : changed),
    createBackup: async () => ({ backupDirectory: "backup", executableRelativePath: "MiniCode.exe" }),
    commitInstall: async () => { committed = true; },
  });
  assert.equal(result.installed, false);
  assert.equal(result.reason, "preflight_changed");
  assert.equal(committed, false);
});

test("update install transaction commits only after two matching preflights", async () => {
  const current = buildUpdatePreflight({
    activity: emptyActivity(15),
    updateReady: true,
    readyVersion: "2.0.0",
  });
  const events = [];
  const result = await executeInstallTransaction({
    fingerprint: current.fingerprint,
    readPreflight: () => {
      events.push("preflight");
      return current;
    },
    createBackup: async () => {
      events.push("backup");
      return { backupDirectory: "backup", executableRelativePath: "MiniCode.exe" };
    },
    commitInstall: async () => { events.push("commit"); },
  });
  assert.deepEqual(result, { installed: true });
  assert.deepEqual(events, ["preflight", "backup", "preflight", "commit"]);
});

test("update boot health requires recovery after a second unconfirmed launch", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-update-health-"));
  const app = { getPath: () => root, getVersion: () => "2.0.0" };
  const file = path.join(root, "update-health.json");
  writeHealthState(file, {
    status: "installing",
    previous_version: "1.9.0",
    pending_version: "2.0.0",
    boot_attempts: 0,
  });

  assert.equal(beginBootHealthCheck(app).status, "booting");
  assert.equal(beginBootHealthCheck(app).status, "recovery_required");
  assert.equal(readHealthState(file).boot_attempts, 2);
});

test("rollback backup copies the complete installation tree", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-update-backup-"));
  const installRoot = path.join(root, "install");
  const userData = path.join(root, "user-data");
  const executable = path.join(installRoot, "MiniCode.exe");
  fs.mkdirSync(path.join(installRoot, "resources", "app"), { recursive: true });
  fs.writeFileSync(executable, "old executable", "utf8");
  fs.writeFileSync(path.join(installRoot, "resources", "app", "package.json"), "old resources", "utf8");
  const app = {
    getPath(name) {
      return name === "exe" ? executable : userData;
    },
    getVersion: () => "1.9.0",
  };
  beginBootHealthCheck(app);

  const backup = await createRollbackBackup(app);

  assert.equal(
    fs.readFileSync(path.join(backup.backupDirectory, "MiniCode.exe"), "utf8"),
    "old executable",
  );
  assert.equal(
    fs.readFileSync(path.join(backup.backupDirectory, "resources", "app", "package.json"), "utf8"),
    "old resources",
  );
});

test("rollback backup rejects an incomplete installation copy", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-update-incomplete-"));
  const installRoot = path.join(root, "install");
  const userData = path.join(root, "user-data");
  const missingExecutable = path.join(installRoot, "MiniCode.exe");
  fs.mkdirSync(installRoot, { recursive: true });
  fs.writeFileSync(path.join(installRoot, "resources.pak"), "resource", "utf8");
  const app = {
    getPath(name) {
      return name === "exe" ? missingExecutable : userData;
    },
    getVersion: () => "1.9.1",
  };
  beginBootHealthCheck(app);

  await assert.rejects(
    createRollbackBackup(app),
    /Rollback backup is incomplete: executable is missing/,
  );
});

test("rollback executable is accepted only from the expected version directory", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-update-path-"));
  const userData = path.join(root, "user-data");
  const app = {
    getPath(name) {
      return name === "userData" ? userData : path.join(root, "current", "MiniCode.exe");
    },
  };
  const expectedDirectory = expectedRollbackDirectory(app, "1.8.0");
  const expectedExecutable = path.join(expectedDirectory, "MiniCode.exe");
  fs.mkdirSync(expectedDirectory, { recursive: true });
  fs.writeFileSync(expectedExecutable, "old executable", "utf8");

  assert.equal(resolveRollbackExecutable(app, {
    previous_version: "1.8.0",
    rollback_backup_dir: expectedDirectory,
    rollback_executable_relative: "MiniCode.exe",
  }), expectedExecutable);
  assert.equal(resolveRollbackExecutable(app, {
    previous_version: "1.8.0",
    rollback_backup_dir: path.join(root, "attacker-controlled"),
    rollback_executable_relative: "MiniCode.exe",
  }), "");
  assert.equal(resolveRollbackExecutable(app, {
    previous_version: "1.8.0",
    rollback_backup_dir: expectedDirectory,
    rollback_executable_relative: path.join("..", "MiniCode.exe"),
  }), "");
});

test("automatic rollback launches the backed-up executable and exits the failed version", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-update-launch-"));
  const userData = path.join(root, "user-data");
  const app = {
    getPath(name) {
      if (name === "userData") return userData;
      if (name === "exe") return path.join(root, "current", path.basename(process.execPath));
      throw new Error(`unexpected app path: ${name}`);
    },
    getVersion: () => "2.0.0",
    releaseSingleInstanceLockCalled: false,
    quitCalled: false,
    releaseSingleInstanceLock() {
      this.releaseSingleInstanceLockCalled = true;
    },
    quit() {
      this.quitCalled = true;
    },
  };
  beginBootHealthCheck(app);
  const backupDirectory = expectedRollbackDirectory(app, "1.9.0");
  const executableName = path.basename(process.execPath);
  const backupExecutable = path.join(backupDirectory, executableName);
  fs.mkdirSync(backupDirectory, { recursive: true });
  fs.copyFileSync(process.execPath, backupExecutable);

  const launched = await attemptAutomaticRollback(app, {
    status: "recovery_required",
    previous_version: "1.9.0",
    pending_version: "2.0.0",
    rollback_backup_dir: backupDirectory,
    rollback_executable_relative: executableName,
  });

  assert.equal(launched, true);
  assert.equal(app.releaseSingleInstanceLockCalled, true);
  assert.equal(app.quitCalled, true);
});

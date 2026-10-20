"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function fixture(preferences) {
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "general-maintenance.js"), "utf8"), { module: moduleBox, process: { platform: "win32" } });
  let timestamp = 0, idle = 0, timer;
  const runs = [], errors = [];
  const worker = moduleBox.exports.startGeneralMaintenance({ getPreferences: () => preferences,
    getIdleSeconds: () => idle, now: () => timestamp,
    schedule(callback, interval) { assert.equal(interval, 60000); timer = callback; return 1; },
    cancel(value) { assert.equal(value, 1); timer = null; },
    runMaintenance: async request => { runs.push(request); }, onError: error => errors.push(error) });
  return { worker, runs, errors, advance(value) { timestamp = value; }, idle(value) { idle = value; }, get timer() { return timer; } };
}

test("the disabled default never archives or compresses even when the desktop is idle", async () => {
  const fixtureValue = fixture({ autoArchiveAfterDays: 0, localHistoryCompression: false });
  fixtureValue.idle(9000);
  await fixtureValue.worker.check();
  assert.equal(fixtureValue.runs.length, 0);
  fixtureValue.worker.dispose();
  assert.equal(fixtureValue.timer, null);
});

test("Source startup compression is independent of the archive idle threshold and cadence", async () => {
  const preferences = { autoArchiveAfterDays: 7, localHistoryCompression: true };
  const fixtureValue = fixture(preferences);
  fixtureValue.idle(899);
  await fixtureValue.worker.check();
  assert.deepEqual(JSON.parse(JSON.stringify(fixtureValue.runs)), [{ auto_archive_after_days: 0, compress_local_history: true }]);
  fixtureValue.idle(900);
  await fixtureValue.worker.check();
  assert.deepEqual(JSON.parse(JSON.stringify(fixtureValue.runs)), [
    { auto_archive_after_days: 0, compress_local_history: true }, { auto_archive_after_days: 7, compress_local_history: false }]);
  fixtureValue.advance(899999);
  await fixtureValue.worker.check();
  assert.equal(fixtureValue.runs.length, 2);
  fixtureValue.advance(900000);
  await fixtureValue.worker.check();
  assert.equal(fixtureValue.runs[2].compress_local_history, false);
  preferences.autoArchiveAfterDays = 0;
  fixtureValue.advance(1800000);
  await fixtureValue.worker.check();
  assert.equal(fixtureValue.runs.length, 3);
  fixtureValue.worker.dispose();
});

test("changing compression after startup does not compress until the next app start", async () => {
  const preferences = { autoArchiveAfterDays: 0, localHistoryCompression: false };
  const fixtureValue = fixture(preferences);
  preferences.localHistoryCompression = true;
  fixtureValue.idle(900);
  await fixtureValue.worker.check();
  assert.equal(fixtureValue.runs.length, 0);
  fixtureValue.worker.dispose();
});

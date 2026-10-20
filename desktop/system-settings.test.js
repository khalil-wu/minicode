"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");

function api(platform = "win32") {
  const box = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "system-settings.js"), "utf8"), {
    module: box, process: { platform }, require,
  });
  return box.exports;
}

test("login startup reads the actual packaged native status and development builds report unavailable", () => {
  const service = api();
  let calls = 0;
  const app = { isPackaged: true, getLoginItemSettings: () => { calls++; return { executableWillLaunchAtLogin: true }; } };
  assert.equal(service.getStartupStatus(app), true);
  assert.equal(calls, 1);
  assert.equal(service.getStartupStatus({ ...app, isPackaged: false }), null);
  assert.equal(api("linux").getStartupStatus(app), null);
  assert.equal(calls, 1);
  const failure = new Error("native startup status unavailable");
  assert.throws(() => service.getStartupStatus({ isPackaged: true, getLoginItemSettings() { throw failure; } }), error => error === failure);
});

test("the existing Windows Startup Apps URI is the only native destination", async () => {
  const opened = [];
  await api().openStartupSettings({ openExternal: async uri => opened.push(uri) });
  assert.deepEqual(opened, ["ms-settings:startupapps"]);
  assert.throws(() => api("linux").openStartupSettings({}), /Windows/);
});

test("the license reader returns the actual packaged notice and surfaces a missing artifact", t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-notices-test-"));
  t.after(() => { assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir())); fs.rmSync(root, { recursive: true }); });
  const service = api();
  assert.throws(() => service.getOpenSourceLicenses({ getAppPath: () => root }), { code: "ENOENT" });
  fs.writeFileSync(path.join(root, "THIRD_PARTY_NOTICES.txt"), "Original third-party license\n许可原文", "utf8");
  assert.equal(service.getOpenSourceLicenses({ getAppPath: () => root }), "Original third-party license\n许可原文");
});

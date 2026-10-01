"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const vm = require("node:vm");

function fixture(overrides = {}, badHash = false) {
  const root = path.resolve(__dirname, "..", "windows-sandbox-runtime");
  const native = path.resolve(root, "..", "native-windows-sandbox");
  function sources(dir) {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const file = path.join(dir, entry.name);
      return entry.isDirectory() ? sources(file)
        : entry.name.endsWith(".rs") || entry.name === "Cargo.toml" ? [file] : [];
    });
  }
  const inputHash = crypto.createHash("sha256");
  for (const file of [path.join(__dirname, "patch-windows-sandbox-source.py"), ...sources(native).sort()]) {
    inputHash.update(path.basename(file)); inputHash.update(Buffer.from([0]));
    inputHash.update(fs.readFileSync(file)); inputHash.update(Buffer.from([0]));
  }
  const files = {};
  for (const name of ["codex.exe", "codex-command-runner.exe", "codex-windows-sandbox-setup.exe"]) {
    files[name] = crypto.createHash("sha256").update(name).digest("hex");
  }
  if (badHash) files["codex.exe"] = "wrong";
  const manifest = {
    identity: "minicode", upstream_version: "0.158.0-alpha.2.1", patch_version: 3,
    reported_version: "minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3",
    owner_namespace: "windows-sid+canonical-home-v3", input_sha256: inputHash.digest("hex"),
    files, ...overrides,
  };
  const fakeFs = {
    ...fs,
    readFileSync(file, encoding) {
      if (file === path.join(root, "runtime.json")) return JSON.stringify(manifest);
      if (path.dirname(file) === root) return Buffer.from(path.basename(file));
      return fs.readFileSync(file, encoding);
    },
  };
  const context = { module: { exports: {} }, __dirname, Buffer, require(name) { return name === "node:fs" ? fakeFs : require(name); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "verify-windows-sandbox-runtime.js"), "utf8"), context);
  return context.module.exports;
}

test("accepts coherent owner-v3 manifest and all three executable hashes", async () => {
  await fixture()({ electronPlatformName: "win32" });
});
test("rejects legacy global-account runtime", async () => {
  await assert.rejects(fixture({ patch_version: 2, offline_account: "MiniCodeSbxOffline", online_account: "MiniCodeSbxOnline" })({ electronPlatformName: "win32" }), /MiniCode-owned/);
});
test("rejects stale source inputs", async () => {
  await assert.rejects(fixture({ input_sha256: "stale" })({ electronPlatformName: "win32" }), /different native source inputs/);
});
test("rejects a swapped executable even with valid owner manifest", async () => {
  await assert.rejects(fixture({}, true)({ electronPlatformName: "win32" }), /hash mismatch/);
});

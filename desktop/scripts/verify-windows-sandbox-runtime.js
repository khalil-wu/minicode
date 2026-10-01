"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

module.exports = async function verifyWindowsSandboxRuntime(context) {
  if (context.electronPlatformName !== "win32") return;

  const root = path.resolve(__dirname, "..", "windows-sandbox-runtime");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(root, "runtime.json"), "utf8").replace(/^\uFEFF/, ""),
  );
  if (
    manifest.identity !== "minicode"
    || manifest.upstream_version !== "0.158.0-alpha.2.1"
    || manifest.patch_version !== 3
    || manifest.owner_namespace !== "windows-sid+canonical-home-v3"
    || manifest.reported_version !== "minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3"
    || manifest.offline_account !== undefined
    || manifest.online_account !== undefined
  ) {
    throw new Error("Windows package requires the MiniCode-owned sandbox runtime");
  }

  const nativeRoot = path.join(root, "..", "native-windows-sandbox");
  function sourceFiles(directory) {
    return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const file = path.join(directory, entry.name);
      return entry.isDirectory() ? sourceFiles(file)
        : entry.name.endsWith(".rs") || entry.name === "Cargo.toml" ? [file] : [];
    });
  }
  const inputs = [path.join(__dirname, "patch-windows-sandbox-source.py"), ...sourceFiles(nativeRoot).sort()];
  const inputHash = crypto.createHash("sha256");
  for (const file of inputs) {
    inputHash.update(path.basename(file));
    inputHash.update(Buffer.from([0]));
    inputHash.update(fs.readFileSync(file));
    inputHash.update(Buffer.from([0]));
  }
  if (manifest.input_sha256 !== inputHash.digest("hex")) {
    throw new Error("Windows sandbox runtime was built from different native source inputs");
  }

  for (const name of [
    "codex.exe",
    "codex-command-runner.exe",
    "codex-windows-sandbox-setup.exe",
  ]) {
    const actual = crypto
      .createHash("sha256")
      .update(fs.readFileSync(path.join(root, name)))
      .digest("hex");
    if (actual !== manifest.files[name]) {
      throw new Error(`Windows sandbox runtime hash mismatch: ${name}`);
    }
  }
};

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const security = require("./security");

test("sandboxed renderer preload only imports Electron's supported bridge", () => {
  const preloadSource = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const imports = Array.from(
    preloadSource.matchAll(/require\((['"])([^'"]+)\1\)/g),
    (match) => match[2],
  );

  assert.deepEqual(imports, ["electron"]);
});

test("IPC capability manifest permits declared channels and rejects unknown channels", () => {
  assert.equal(security.assertIpcCapability("minicode:window:minimize"), "minicode:window:minimize");
  assert.equal(security.assertIpcCapability("minicode:embeddedBrowser:list"), "minicode:embeddedBrowser:list");
  assert.throws(
    () => security.assertIpcCapability("minicode:unknown"),
    (error) => error?.code === "ERR_UNDECLARED_IPC_CAPABILITY",
  );
});

test("IPC capability manifest covers every registered invoke handler", () => {
  const handlersSource = fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8");
  const registeredChannels = Array.from(
    handlersSource.matchAll(/ipcMain\.handle\("([^"]+)"/g),
    (match) => match[1],
  );

  assert.deepEqual(
    registeredChannels.filter((channel) => !security.IPC_CAPABILITIES.has(channel)),
    [],
  );
});

test("restored workspace trust only accepts a root from the main-process ledger", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-security-"));
  const trusted = path.join(tempRoot, "trusted");
  const rejected = path.join(tempRoot, "rejected");
  const dataDir = path.join(tempRoot, "data");
  fs.mkdirSync(trusted);
  fs.mkdirSync(rejected);
  fs.mkdirSync(dataDir);
  const trustedRootsFile = path.join(dataDir, "trusted_workspaces.json");
  fs.writeFileSync(trustedRootsFile, JSON.stringify({ version: 1, roots: [trusted] }));
  security.init({ initialRoots: new Set(), trustedRootsFile });

  assert.equal(security.restoreTrustedWorkspaceRoot(rejected), "");
  assert.equal(security.restoreTrustedWorkspaceRoot(trusted), fs.realpathSync.native(trusted));
  assert.equal(security.isWithinTrustedWorkspace(path.join(trusted, "file.txt")), true);
});

test("workspace trust checks fail closed when an existing path cannot be canonicalized", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-realpath-denied-"));
  const trusted = path.join(tempRoot, "trusted");
  const inaccessible = path.join(tempRoot, "inaccessible");
  fs.mkdirSync(trusted);
  fs.mkdirSync(inaccessible);
  security.init({ initialRoots: new Set([trusted]) });

  const originalRealpath = fs.realpathSync.native;
  fs.realpathSync.native = (candidate, ...args) => {
    if (path.resolve(candidate) === path.resolve(inaccessible)) {
      const error = new Error("operation not permitted");
      error.code = "EPERM";
      throw error;
    }
    return originalRealpath(candidate, ...args);
  };
  try {
    assert.equal(security.isWithinTrustedWorkspace(inaccessible), false);
    assert.equal(security.rememberTrustedWorkspaceRoot(inaccessible), "");
  } finally {
    fs.realpathSync.native = originalRealpath;
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("native workspace approval is persisted for a later desktop launch", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-ledger-"));
  const workspace = path.join(tempRoot, "workspace");
  const dataDir = path.join(tempRoot, "data");
  fs.mkdirSync(workspace);
  fs.mkdirSync(dataDir);
  const trustedRootsFile = path.join(dataDir, "trusted_workspaces.json");
  security.init({ initialRoots: new Set(), trustedRootsFile });

  assert.equal(security.rememberTrustedWorkspaceRoot(workspace), fs.realpathSync.native(workspace));
  security.init({ initialRoots: new Set(), trustedRootsFile });
  assert.equal(security.isWithinTrustedWorkspace(path.join(workspace, "before-restore.txt")), false);
  assert.equal(security.restoreTrustedWorkspaceRoot(workspace), fs.realpathSync.native(workspace));
});

test("legacy active workspace is migrated and can be restored without activating other history", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-migration-"));
  const workspace = path.join(tempRoot, "workspace");
  fs.mkdirSync(workspace);
  const trustedRootsFile = path.join(tempRoot, "data", "trusted_workspaces.json");

  security.init({ initialRoots: new Set(), trustedRootsFile });
  assert.equal(security.rememberTrustedWorkspaceRoot(workspace), fs.realpathSync.native(workspace));
  security.init({ initialRoots: new Set(), trustedRootsFile });

  assert.equal(security.isWithinTrustedWorkspace(path.join(workspace, "before-restore.txt")), false);
  assert.equal(security.restoreTrustedWorkspaceRoot(workspace), fs.realpathSync.native(workspace));
  assert.equal(security.isWithinTrustedWorkspace(path.join(workspace, "after-restore.txt")), true);
});

test("trusted path checks reject junction escapes", (t) => {
  if (process.platform !== "win32") {
    t.skip("junction behavior is Windows-specific");
    return;
  }
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-junction-"));
  const workspace = path.join(tempRoot, "workspace");
  const outside = path.join(tempRoot, "outside");
  const junction = path.join(workspace, "escape");
  fs.mkdirSync(workspace);
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, "secret.txt"), "secret");
  fs.symlinkSync(outside, junction, "junction");
  security.init({ initialRoots: new Set([workspace]) });

  assert.throws(
    () => security.assertTrustedPath(path.join(junction, "secret.txt")),
    /outside the trusted workspace/,
  );
});

test("workspace safety blocks Windows system folders from non-C drives", () => {
  const original = {
    SystemRoot: process.env.SystemRoot,
    ProgramFiles: process.env.ProgramFiles,
    ProgramData: process.env.ProgramData,
    SystemDrive: process.env.SystemDrive,
  };
  try {
    process.env.SystemRoot = "D:\\Windows";
    process.env.ProgramFiles = "E:\\Apps";
    process.env.ProgramData = "F:\\ProgramData";
    process.env.SystemDrive = "G:";

    assert.equal(security.isSafeWorkspacePath("D:\\Windows\\System32\\drivers"), false);
    assert.equal(security.isSafeWorkspacePath("E:\\Apps\\Vendor"), false);
    assert.equal(security.isSafeWorkspacePath("F:\\ProgramData\\Vendor"), false);
    assert.equal(security.isSafeWorkspacePath("G:\\Recovery\\Logs"), false);
    assert.equal(security.isSafeWorkspacePath("E:\\Projects\\MiniCode"), true);
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test("MiniCode tool-result storage is readable but never writable workspace state", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-readonly-"));
  const workspace = path.join(tempRoot, "workspace");
  const toolResults = path.join(tempRoot, "user-data", "data", "tool-results");
  const resultFile = path.join(toolResults, "mc_web_fetch_example.txt");
  const siblingFile = path.join(tempRoot, "user-data", "data", "settings.json");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(toolResults, { recursive: true });
  fs.writeFileSync(resultFile, "persisted result", "utf8");
  fs.writeFileSync(siblingFile, "secret", "utf8");
  security.init({ initialRoots: new Set([workspace]), readOnlyRoots: [toolResults] });

  assert.equal(security.assertReadablePath(resultFile), fs.realpathSync.native(resultFile));
  assert.equal(security.isWithinAppReadOnlyData(resultFile), true);
  assert.throws(() => security.assertTrustedPath(resultFile), /outside the trusted workspace/);
  assert.throws(() => security.assertMutableTrustedPath(resultFile), /outside the trusted workspace/);
  assert.throws(() => security.assertReadablePath(siblingFile), /outside the trusted workspace/);
});

test("known user output folders open safe deliverables but reject executable files", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-user-output-"));
  const desktop = path.join(tempRoot, "Desktop");
  const outside = path.join(tempRoot, "outside");
  fs.mkdirSync(desktop);
  fs.mkdirSync(outside);
  const document = path.join(desktop, "report.docx");
  const script = path.join(desktop, "helper.ps1");
  const executable = path.join(desktop, "setup.exe");
  const privateDocument = path.join(outside, "private.docx");
  fs.writeFileSync(document, "document");
  fs.writeFileSync(script, "Write-Host nope");
  fs.writeFileSync(executable, "binary");
  fs.writeFileSync(privateDocument, "private");
  security.init({ initialRoots: new Set(), userOutputRoots: [desktop] });

  assert.equal(security.isReadableUserOutput(document), true);
  assert.equal(security.assertReadablePath(document), fs.realpathSync.native(document));
  assert.equal(security.isReadablePath(script), false);
  assert.equal(security.isReadablePath(executable), false);
  assert.equal(security.isReadablePath(privateDocument), false);
  assert.throws(() => security.assertReadablePath(script), /safe user output folders/);
});

test("MiniCode read-only roots reject symbolic-link escapes", (t) => {
  if (process.platform !== "win32") {
    t.skip("junction behavior is Windows-specific");
    return;
  }
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-readonly-junction-"));
  const toolResults = path.join(tempRoot, "tool-results");
  const outside = path.join(tempRoot, "outside");
  const junction = path.join(toolResults, "escape");
  fs.mkdirSync(toolResults);
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, "secret.txt"), "secret", "utf8");
  fs.symlinkSync(outside, junction, "junction");
  security.init({ initialRoots: new Set(), readOnlyRoots: [toolResults] });

  assert.throws(
    () => security.assertReadablePath(path.join(junction, "secret.txt")),
    /outside the trusted workspace/,
  );
});

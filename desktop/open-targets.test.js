"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const test = require("node:test");

function load(file, overrides, testProcess = process) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, file), "utf8"), {
    module, exports: module.exports, process: testProcess, Buffer, console,
    require(name) { return Object.hasOwn(overrides, name) ? overrides[name] : require(name); },
  }, { filename: file });
  return module.exports;
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-open-targets-"));
  t.after(() => {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()));
    fs.rmSync(root, { recursive: true });
  });
  const env = { ...process.env, LOCALAPPDATA: path.join(root, "Local"), APPDATA: path.join(root, "Roaming"),
    ProgramFiles: path.join(root, "Programs"), "ProgramFiles(x86)": path.join(root, "Programs32"),
    ProgramData: path.join(root, "ProgramData"), SystemRoot: path.join(root, "Windows"),
    Path: path.join(root, "bin"), PATHEXT: ".com;.exe;.bat;.cmd", CMDER_ROOT: "" };
  const launches = [], reveals = [], openPaths = [];
  const shell = { readShortcutLink: file => ({ target: fs.readFileSync(file, "utf8") }),
    showItemInFolder: file => reveals.push(file), openPath: async file => { openPaths.push(file); return ""; } };
  const childProcess = {
    spawnSync: () => ({ status: 1, stdout: "" }),
    spawn(command, args, options) {
      const child = new EventEmitter();
      const record = { command, args: Array.from(args), options, unref: false };
      child.unref = () => { record.unref = true; };
      launches.push(record);
      queueMicrotask(() => { child.emit("spawn"); child.emit("close", 0); });
      return child;
    },
  };
  const testProcess = { ...process, platform: "win32", env };
  const targets = load("open-targets.js", { electron: { shell }, "node:child_process": childProcess }, testProcess);
  const preferences = load("preferences.js", { "./open-targets": targets }, testProcess);
  const preferencesFile = path.join(root, "preferences.json");
  preferences.init(preferencesFile);
  const install = (file, contents = "executable fixture") => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, contents); return file; };
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const file = install(path.join(workspace, "app.ts"), "export const value = 1;");
  return { root, env, targets, preferences, preferencesFile, testProcess, childProcess, shell, launches, reveals, openPaths, install, workspace, file };
}

test("Windows discovery finds system installs, CLI executables, shortcuts and current JetBrains versions without advertising other platforms", t => {
  const f = fixture(t);
  const code = f.install(path.join(f.env.ProgramFiles, "Microsoft VS Code", "Code.exe"));
  f.install(path.join(f.env.LOCALAPPDATA, "Programs", "Microsoft VS Code Insiders", "Code - Insiders.exe"));
  f.install(path.join(f.env.Path, "code-insiders.cmd"));
  // The CLI must resolve the sibling executable, rather than launching the cmd shim.
  const insidersCli = f.install(path.join(f.env.Path, "Code - Insiders.exe"));
  const cursor = f.install(path.join(f.env.ProgramFiles, "Cursor", "resources", "app", "bin", "cursor.cmd"));
  f.install(path.join(f.env.ProgramFiles, "Cursor", "Cursor.exe"));
  f.env.Path += path.delimiter + path.dirname(cursor);
  f.install(path.join(f.env.ProgramFiles, "JetBrains", "PyCharm 2024.1", "bin", "pycharm64.exe"));
  const pycharm = f.install(path.join(f.env.ProgramFiles, "JetBrains", "PyCharm 2026.2", "bin", "pycharm64.exe"));
  const terminal = f.install(path.join(f.root, "Terminal", "WindowsTerminal.exe"));
  f.install(path.join(f.env.APPDATA, "Microsoft", "Windows", "Start Menu", "Programs", "Terminals", "Windows Terminal.lnk"), terminal);
  f.install(path.join(f.env.ProgramFiles, "Windsurf", "Windsurf.exe"));
  const discovered = f.targets.fileOpenTargets();
  assert.equal(discovered.find(target => target.id === "vscode").executable, code);
  assert.equal(discovered.find(target => target.id === "vscodeInsiders").executable, insidersCli);
  assert.equal(discovered.find(target => target.id === "cursor").executable, path.join(f.env.ProgramFiles, "Cursor", "Cursor.exe"));
  assert.equal(discovered.find(target => target.id === "pycharm").executable, pycharm);
  assert.equal(discovered.find(target => target.id === "terminal").executable, terminal);
  assert.equal(discovered.find(target => target.id === "system").hidden, true);
  assert.equal(discovered.find(target => target.id === "minicode").hidden, true);
  for (const id of ["windsurf", "warp", "goland", "rustrover", "wsl"]) assert.equal(discovered.some(target => target.id === id), false);
});

test("source target arguments distinguish directories, locations, file reveal and detached terminals", async t => {
  const f = fixture(t);
  for (const parts of [["Microsoft VS Code", "Code.exe"], ["Zed", "Zed.exe"], ["Git", "git-bash.exe"]]) f.install(path.join(f.env.ProgramFiles, ...parts));
  f.install(path.join(f.env.ProgramFiles, "JetBrains", "IntelliJ IDEA 2026.2", "bin", "idea64.exe"));
  f.install(path.join(f.env.LOCALAPPDATA, "Microsoft", "WindowsApps", "wt.exe"));
  const target = id => f.targets.fileOpenTargets().find(target => target.id === id);
  await f.targets.openInTarget(target("vscode"), f.workspace);
  await f.targets.openInTarget(target("vscode"), f.file, 7, 3);
  await f.targets.openInTarget(target("zed"), f.file, 7, 3);
  await f.targets.openInTarget(target("intellij"), f.file, 7, 3);
  await f.targets.openInTarget(target("gitBash"), f.file);
  await f.targets.openInTarget(target("terminal"), f.file);
  assert.deepEqual(f.launches.map(launch => launch.args), [
    [f.workspace], ["--goto", `${f.file}:7:3`], [`${f.file}:7:3`],
    ["--line", "7", "--column", "3", f.file], [`--cd=${f.workspace}`], ["-d", f.workspace],
  ]);
  assert.equal(f.launches.at(-1).options.detached, true);
  assert.equal(f.launches.at(-1).unref, true);
  await f.targets.openInTarget(target("fileManager"), f.file);
  await f.targets.openInTarget(target("fileManager"), f.workspace);
  assert.deepEqual(f.reveals, [f.file]);
  assert.deepEqual(f.openPaths, [f.workspace]);
});

test("actual target facade preserves the Source catalog slot for Default app without sorting later editor factories", t => {
  const f = fixture(t);
  f.install(path.join(f.env.ProgramFiles, "Microsoft VS Code", "Code.exe"));
  f.install(path.join(f.env.ProgramFiles, "Cursor", "Cursor.exe"));
  f.install(path.join(f.env.ProgramFiles, "Antigravity", "Antigravity.exe"));
  f.install(path.join(f.env.LOCALAPPDATA, "Microsoft", "WindowsApps", "wt.exe"));
  f.install(path.join(f.env.ProgramFiles, "JetBrains", "WebStorm 2026.2", "bin", "webstorm64.exe"));
  const ids = f.targets.fileOpenTargets().map(target => target.id);
  assert.deepEqual(Array.from(ids), ["minicode", "vscode", "cursor", "antigravity", "system", "fileManager", "terminal", "webstorm"]);
  f.preferences.setPreferences({ fileOpenTarget: "system" });
  assert.equal(f.preferences.getPreferences().fileOpenTarget, "system");
});

test("workspace preferences retain each path while global selection changes and survive a restart", t => {
  const f = fixture(t);
  const workspaceB = path.join(f.root, "workspaceB");
  f.preferences.setPreferredFileOpenTarget(f.workspace, "vscode");
  f.preferences.setPreferredFileOpenTarget(workspaceB, "cursor");
  f.preferences.setPreferences({ fileOpenTarget: "fileManager" });
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "vscode");
  assert.equal(f.preferences.preferredFileOpenTarget(workspaceB), "cursor");
  assert.equal(f.preferences.preferredFileOpenTarget(path.join(f.root, "workspaceC")), "fileManager");
  f.preferences.init(f.preferencesFile);
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "vscode");
  assert.equal(f.preferences.preferredFileOpenTarget(workspaceB), "cursor");
});

for (const layout of ["bin", "resources/app/bin", "cli-only"]) {
  test(`Antigravity ${layout} preserves its CLI driver and publishes the existing Source brand asset`, async t => {
    const f = fixture(t);
    const root = path.join(f.root, "ActualAntigravity");
    const directory = layout === "resources/app/bin" ? path.join(root, "resources", "app", "bin") : path.join(root, "bin");
    const launcher = f.install(path.join(directory, "antigravity.cmd"));
    if (layout !== "cli-only") f.install(path.join(root, "Antigravity.exe"));
    f.env.Path = directory;
    const target = f.targets.fileOpenTargets().find(target => target.id === "antigravity");
    assert.equal(target.executable, launcher);
    assert.equal(target.iconPath, null);
    assert.equal(target.icon, "apps/antigravity.png");
    await f.targets.openInTarget(target, f.file, 11, 3);
    assert.equal(f.launches.at(-1).args.includes(launcher), true);
    assert.deepEqual(f.launches.at(-1).args.slice(-2), ["--goto", `${f.file}:11:3`]);
  });
}

test("preload forwards one-time target and workspace persistence fields without changing them", async () => {
  let runtime;
  const calls = [];
  const ipcRenderer = new EventEmitter();
  ipcRenderer.sendSync = () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" });
  ipcRenderer.invoke = (...args) => { calls.push(args); return Promise.resolve({ openInEditor: false }); };
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    module, process: { ...process, env: {}, argv: [], contextIsolated: true }, console, window: {},
    require: () => ({ ipcRenderer, contextBridge: { exposeInMainWorld(_key, value) { runtime = value; } } }),
  });
  const options = { targetId: "terminal", workspaceRoot: "C:/workspace", persistPreferredTargetPath: "C:/workspace" };
  await runtime.desktop.openPreferredPath("C:/workspace", undefined, options);
  assert.deepEqual(calls[0], ["minicode:openPreferredPath", "C:/workspace", undefined, options]);
});

test("actual IPC consumer persists workspace choice and broadcasts it, but explicit file selection stays temporary", async t => {
  const f = fixture(t);
  f.install(path.join(f.env.ProgramFiles, "Microsoft VS Code", "Code.exe"));
  f.install(path.join(f.env.ProgramFiles, "Cursor", "Cursor.exe"));
  const handlers = new Map(), broadcasts = [];
  const webContents = { mainFrame: {}, send: (channel, payload) => broadcasts.push({ channel, payload }) };
  const window = { webContents, isDestroyed: () => false };
  const electron = { shell: f.shell,
    app: { getLocale: () => "en-US", getFileIcon: async () => ({ isEmpty: () => false, toDataURL: () => "data:image/png;base64,fixture" }) },
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler), on() {} },
  };
  const security = require("./security");
  security.init({ initialRoots: [f.workspace], userOutputRoots: [] });
  const ipc = load("ipc-handlers.js", { electron, "./open-targets": f.targets, "./security": security });
  ipc.init({ desktopPreferences: f.preferences, getMainWindows: () => [window],
    getWindowForSender: sender => sender === webContents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "open-target-fixture" }) });
  ipc.registerIpcHandlers();
  const event = { sender: webContents, senderFrame: webContents.mainFrame };
  const open = handlers.get("minicode:openPreferredPath");
  await open(event, f.workspace, undefined, { targetId: "vscode", workspaceRoot: f.workspace, persistPreferredTargetPath: f.workspace });
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "vscode");
  assert.equal(broadcasts.length, 1);
  assert.equal(broadcasts[0].channel, "minicode:preferences:changed");
  assert.equal(broadcasts[0].payload.fileOpenTargetPerPath[f.workspace], "vscode");
  assert.equal(broadcasts[0].payload.fileOpenTargets.find(target => target.id === "vscode").icon, "data:image/png;base64,fixture");
  await open(event, f.file, 11, { targetId: "cursor", workspaceRoot: f.workspace });
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "vscode");
  assert.equal(broadcasts.length, 1);
  assert.equal(path.basename(f.launches.at(-1).command), "Cursor.exe");
  await open(event, f.file, 7, { workspaceRoot: f.workspace });
  assert.equal(path.basename(f.launches.at(-1).command), "Code.exe");
  assert.deepEqual(f.launches.at(-1).args, ["--goto", `${f.file}:7:1`]);
  await assert.rejects(open(event, f.file, undefined, { targetId: "zed" }), /no longer installed/);
  const document = f.install(path.join(f.workspace, "report.pdf"));
  const launchCount = f.launches.length;
  await open(event, document, undefined, { workspaceRoot: f.workspace });
  assert.equal(f.openPaths.at(-1), document);
  assert.equal(f.launches.length, launchCount);
  await open(event, f.workspace, undefined, { workspaceRoot: f.workspace });
  assert.equal(f.openPaths.at(-1), f.workspace);
  assert.equal(f.launches.length, launchCount);
  await open(event, document, undefined, { targetId: "cursor", workspaceRoot: f.workspace });
  assert.equal(path.basename(f.launches.at(-1).command), "Cursor.exe");
  const originalLaunches = f.launches.length;
  const selectedInternal = await open(event, f.file, 9, { targetId: "minicode", workspaceRoot: f.workspace,
    persistPreferredTargetPath: f.workspace });
  assert.equal(selectedInternal.openInEditor, true);
  assert.equal(selectedInternal.path, f.file);
  assert.equal(f.launches.length, originalLaunches);
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "minicode");
  assert.equal(broadcasts.length, 2);
  assert.equal(broadcasts[1].payload.fileOpenTargetPerPath[f.workspace], "minicode");
  f.preferences.init(f.preferencesFile);
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "minicode");
  // Source Open with persists actual editor targets only. A non-editor cannot
  // become the preference even if an IPC caller supplies the explicit field.
  await open(event, f.workspace, undefined, { targetId: "fileManager", workspaceRoot: f.workspace,
    persistPreferredTargetPath: f.workspace });
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "minicode");
  assert.equal(broadcasts.length, 2);
  // Primary/header opens carry only targetId and remain temporary for editors.
  await open(event, f.file, 4, { targetId: "vscode", workspaceRoot: f.workspace });
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "minicode");
  assert.equal(broadcasts.length, 2);
  const result = await open(event, document, undefined, { workspaceRoot: f.workspace });
  assert.equal(result.openInEditor, true);
  assert.equal(f.openPaths.at(-1), f.workspace);
  assert.equal(f.preferences.preferredFileOpenTarget(f.workspace), "minicode");
});

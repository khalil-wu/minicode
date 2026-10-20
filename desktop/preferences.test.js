"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const preferences = require("./preferences");
const history = require("./browser-history");
const security = require("./security");
const ptyManager = require("./pty-manager");

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-preferences-test-"));
  const file = path.join(directory, "preferences.json");
  preferences.init(file, { defaultTaskDirectory: path.join(directory, "tasks") });
  return { directory, file };
}

test("persisted preferences drive close and typed notification policy after restart", () => {
  const { file } = fixture();
  preferences.setPreferences({ localeOverride: "en-US", confirmBeforeClose: "always", turnCompletionNotifications: "never", permissionNotifications: false, questionNotifications: true, showContextWindowUsage: true, showBottomPanel: false });
  preferences.init(file);
  assert.equal(preferences.getPreferences().localeOverride, "en-US");
  assert.equal(preferences.getPreferences().showContextWindowUsage, true);
  assert.equal(preferences.getPreferences().showBottomPanel, false);
  assert.equal(preferences.needsCloseConfirmation("button"), true);
  assert.equal(preferences.needsCloseConfirmation("native"), true);
  assert.equal(preferences.shouldNotify("completion", false), false);
  assert.equal(preferences.shouldNotify("permission", false), false);
  assert.equal(preferences.shouldNotify("question", true), true);
  preferences.setPreferences({ confirmBeforeClose: "keyboardOnly", turnCompletionNotifications: "onlyWhenUnfocused" });
  assert.equal(preferences.needsCloseConfirmation("button"), false);
  assert.equal(preferences.needsCloseConfirmation("native"), false);
  assert.equal(preferences.needsCloseConfirmation("keyboard"), true);
  assert.equal(preferences.shouldNotify("completion", true), false);
  assert.equal(preferences.shouldNotify("completion", false), true);
});

test("projectless chats follow the runtime default until a directory is explicitly selected", () => {
  const { directory, file } = fixture();
  assert.equal(preferences.getPreferences().defaultTaskDirectory, path.join(directory, "tasks"));
  assert.equal(preferences.getPreferences().taskDirectoryOverride, null);
  preferences.setPreferences({ showBottomPanel: false });
  const saved = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.equal(saved.defaultTaskDirectory, null);
  assert.equal("taskDirectoryOverride" in saved, false);
  const nextDefault = path.join(directory, "new-runtime-default");
  preferences.init(file, { defaultTaskDirectory: nextDefault });
  assert.equal(preferences.getPreferences().defaultTaskDirectory, nextDefault);
  assert.equal(preferences.getPreferences().taskDirectoryOverride, null);
  assert.equal(preferences.getPreferences().showBottomPanel, false);
});

test("projectless Reset clears only the override and preserves legacy paths, files and other preferences", () => {
  const { directory, file } = fixture();
  const original = path.join(directory, "original-task-folder");
  fs.mkdirSync(original);
  const content = path.join(original, "unsaved-work.txt");
  fs.writeFileSync(content, "Keep task data and draft content");
  const legacy = JSON.stringify({ defaultTaskDirectory: original, localeOverride: "en-US", showBottomPanel: false });
  fs.writeFileSync(file, legacy);
  const dynamicDefault = path.join(directory, "runtime-default");
  preferences.init(file, { defaultTaskDirectory: dynamicDefault });
  assert.equal(fs.readFileSync(file, "utf8"), legacy);
  assert.equal(preferences.getPreferences().taskDirectoryOverride, original);
  assert.equal(preferences.getPreferences().defaultTaskDirectory, original);
  preferences.setPreferences({ defaultTaskDirectory: dynamicDefault });
  assert.equal(preferences.getPreferences().taskDirectoryOverride, dynamicDefault);
  const reset = preferences.setPreferences({ defaultTaskDirectory: null });
  assert.equal(reset.defaultTaskDirectory, dynamicDefault);
  assert.equal(reset.taskDirectoryOverride, null);
  assert.equal(fs.existsSync(dynamicDefault), false);
  const nextDefault = path.join(directory, "runtime-default-after-restart");
  preferences.init(file, { defaultTaskDirectory: nextDefault });
  assert.equal(preferences.getPreferences().defaultTaskDirectory, nextDefault);
  assert.equal(preferences.getPreferences().taskDirectoryOverride, null);
  assert.equal(preferences.getPreferences().localeOverride, "en-US");
  assert.equal(preferences.getPreferences().showBottomPanel, false);
  assert.equal(fs.readFileSync(content, "utf8"), "Keep task data and draft content");
  assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).defaultTaskDirectory, null);
});

test("preference publication follows the durable write and rejects undeclared settings", (t) => {
  const { file } = fixture();
  preferences.setPreferences({ showFullUrl: false });
  const before = fs.readFileSync(file, "utf8");
  t.mock.method(fs, "renameSync", () => { throw new Error("preference disk failure"); });
  assert.throws(() => preferences.setPreferences({ showFullUrl: true }), /preference disk failure/);
  assert.equal(preferences.getPreferences().showFullUrl, false);
  assert.equal(fs.readFileSync(file, "utf8"), before);
  assert.throws(() => preferences.setPreferences({ showFullUrl: "yes" }), /Unsupported/);
  assert.throws(() => preferences.setPreferences({ unknownPreference: true }), /Unsupported/);
});

test("owned preference IPC broadcasts saved values and rejects a foreign frame", async () => {
  fixture();
  const handlers = new Map();
  const broadcasts = [];
  const mainFrame = {};
  const webContents = { mainFrame, send: (channel, value) => broadcasts.push({ channel, value }) };
  const window = { webContents, isDestroyed: () => false };
  let nativeMenus = 0;
  const moduleValue = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), { module: moduleValue, process, console,
    require(name) { return name === "electron" ? { app: { getLocale: () => "zh-CN" }, ipcMain: { on() {}, handle(channel, callback) { handlers.set(channel, callback); } } } : require(name); },
  });
  moduleValue.exports.init({ desktopPreferences: { ...preferences, fileOpenTargets: () => [] }, getMainWindows: () => [window], getWindowForSender: sender => sender === webContents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "owned-profile" }), onLocaleChanged: () => { nativeMenus++; } });
  moduleValue.exports.registerIpcHandlers();
  const set = handlers.get("minicode:preferences:set");
  const result = await set({ sender: webContents, senderFrame: mainFrame }, { localeOverride: "en-US", showFullAccess: false });
  assert.equal(result.localeOverride, "en-US");
  assert.equal(result.showFullAccess, false);
  assert.equal(nativeMenus, 1);
  assert.equal(broadcasts[0].channel, "minicode:preferences:changed");
  assert.equal(broadcasts[0].value.showFullAccess, false);
  assert.throws(() => set({ sender: webContents, senderFrame: {} }, { showFullUrl: true }), { code: "ERR_UNTRUSTED_IPC_SENDER" });
  assert.equal(preferences.getPreferences().showFullUrl, false);
});

test("native projectless Change, cancel and Reset publish effective paths to every window through the existing IPC", async () => {
  const { directory, file } = fixture();
  const selected = path.join(directory, "selected-folder");
  fs.mkdirSync(selected);
  fs.writeFileSync(path.join(selected, "keep.txt"), "original task data");
  const handlers = new Map();
  const broadcasts = [[], []];
  const mainFrame = {};
  const windows = broadcasts.map(messages => ({ isDestroyed: () => false,
    webContents: { mainFrame, send: (channel, value) => messages.push({ channel, value }) } }));
  const event = { sender: windows[0].webContents, senderFrame: mainFrame };
  let canceled = false;
  const chooserPaths = [];
  const moduleValue = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), { module: moduleValue, process, console,
    require(name) { return name === "electron" ? {
      app: { getLocale: () => "zh-CN" },
      ipcMain: { on() {}, handle(channel, callback) { handlers.set(channel, callback); } },
      dialog: { async showOpenDialog(_window, options) { chooserPaths.push(options.defaultPath); return { canceled, filePaths: [selected] }; } },
    } : require(name); },
  });
  moduleValue.exports.init({ desktopPreferences: { ...preferences, fileOpenTargets: () => [], terminalShellTargets: () => [] },
    getMainWindows: () => windows, getWindowForSender: sender => windows.find(window => window.webContents === sender),
    getWindowRecord: () => ({ windowInstanceId: "projectless-owner" }) });
  moduleValue.exports.registerIpcHandlers();
  const get = handlers.get("minicode:preferences:get");
  const choose = handlers.get("minicode:preferences:chooseTaskDirectory");
  const set = handlers.get("minicode:preferences:set");
  assert.equal((await get(event)).taskDirectoryOverride, null);
  const chosen = await choose(event);
  assert.equal(chosen.defaultTaskDirectory, fs.realpathSync(selected));
  assert.equal(chosen.taskDirectoryOverride, fs.realpathSync(selected));
  assert.equal(chooserPaths[0], path.join(directory, "tasks"));
  const saved = fs.readFileSync(file, "utf8");
  canceled = true;
  assert.equal(await choose(event), null);
  assert.equal(fs.readFileSync(file, "utf8"), saved);
  assert.equal(chooserPaths[1], fs.realpathSync(selected));
  assert.equal(broadcasts.every(messages => messages.length === 1), true);
  const reset = await set(event, { defaultTaskDirectory: null });
  assert.equal(reset.defaultTaskDirectory, path.join(directory, "tasks"));
  assert.equal(reset.taskDirectoryOverride, null);
  for (const messages of broadcasts) {
    assert.deepEqual(messages.map(message => message.channel), ["minicode:preferences:changed", "minicode:preferences:changed"]);
    assert.equal(messages[1].value.defaultTaskDirectory, reset.defaultTaskDirectory);
    assert.equal(messages[1].value.taskDirectoryOverride, null);
  }
  preferences.init(file, { defaultTaskDirectory: path.join(directory, "changed-runtime-default") });
  assert.equal((await get(event)).defaultTaskDirectory, path.join(directory, "changed-runtime-default"));
  assert.equal(fs.readFileSync(path.join(selected, "keep.txt"), "utf8"), "original task data");
});

function shellFixture(envPatch = {}, platform = "win32") {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-shell-settings-test-"));
  const bin = path.join(directory, "path-bin");
  fs.mkdirSync(bin);
  const env = { Path: bin, LOCALAPPDATA: path.join(directory, "local"), ProgramFiles: path.join(directory, "programs"),
    "ProgramFiles(x86)": path.join(directory, "programs-x86"), ...envPatch };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preferences.js"), "utf8"), {
    module: moduleBox, process: { platform, env }, console,
    require(name) { return name === "./open-targets" ? { FILE_OPEN_TARGET_IDS: ["minicode", "system"], fileOpenTargets: () => [] } : require(name); },
  });
  const prefs = moduleBox.exports;
  const file = path.join(directory, "preferences.json");
  prefs.init(file, { defaultTaskDirectory: path.join(directory, "tasks") });
  const install = (relative) => {
    const executable = path.join(directory, relative);
    fs.mkdirSync(path.dirname(executable), { recursive: true });
    fs.writeFileSync(executable, "fixture executable");
    return executable;
  };
  return { directory, bin, env, prefs, file, install };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test("Source PowerShell discovers PATH pwsh before powershell and publishes only actual ordinary shells", () => {
  const { prefs, env, install, directory } = shellFixture();
  const old = install("path-bin/powershell.exe");
  const modern = install("later-bin/pwsh.exe");
  const cmd = install("path-bin/cmd.exe");
  env.Path = [path.join(directory, "path-bin"), path.join(directory, "later-bin")].join(path.delimiter);
  assert.deepEqual(plain(prefs.terminalShellTargets()), [
    { id: "powershell", label: "PowerShell", executable: modern, args: [] },
    { id: "cmd", label: "Command Prompt", executable: cmd, args: [] },
  ]);
  assert.equal(prefs.resolveTerminalShell().executable, modern);
  fs.unlinkSync(modern);
  assert.equal(prefs.resolveTerminalShell().executable, old);
  fs.unlinkSync(old);
  assert.deepEqual(plain(prefs.terminalShellTargets()).map(target => target.id), ["cmd"]);
  assert.equal(prefs.resolveTerminalShell().executable, cmd);
  fs.unlinkSync(cmd);
  assert.deepEqual(plain(prefs.terminalShellTargets()), []);
  assert.throws(() => prefs.resolveTerminalShell(), /No installed terminal shell/);
});

test("Command Prompt uses trimmed COMSPEC and the actual PATH cmd when COMSPEC is empty", () => {
  const { prefs, env, install } = shellFixture();
  const cmd = install("path-bin/cmd.exe");
  const configured = install("custom/cmd.exe");
  env.COMSPEC = `   ${configured}   `;
  prefs.setPreferences({ terminalShell: "cmd" });
  assert.equal(prefs.resolveTerminalShell().executable, configured);
  env.COMSPEC = "  ";
  assert.equal(prefs.resolveTerminalShell().executable, cmd);
  env.COMSPEC = path.join(path.dirname(configured), "not-installed.exe");
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "cmd"), false);
});

test("Source Git Bash follows launcher, git-neighbour and installation-root discovery order", () => {
  const { prefs, env, directory, install } = shellFixture();
  const launcher = install("path-bin/git-bash.exe");
  install("Git/cmd/git.exe");
  const beside = install("Git/cmd/bash.exe");
  const parentBin = install("Git/bin/bash.exe");
  const local = install("local/Programs/Git/bin/bash.exe");
  const machine = install("programs/Git/bin/bash.exe");
  const x86 = install("programs-x86/Git/bin/bash.exe");
  env.Path = [path.join(directory, "path-bin"), path.join(directory, "Git", "cmd")].join(path.delimiter);
  const target = () => prefs.terminalShellTargets().find(target => target.id === "gitBash");
  assert.deepEqual(plain(target()), { id: "gitBash", label: "Git Bash", executable: launcher, args: ["--login", "-i"] });
  fs.unlinkSync(launcher);
  assert.equal(target().executable, beside);
  fs.unlinkSync(beside);
  assert.equal(target().executable, parentBin);
  fs.unlinkSync(parentBin);
  assert.equal(target().executable, local);
  fs.unlinkSync(local);
  assert.equal(target().executable, machine);
  fs.unlinkSync(machine);
  assert.equal(target().executable, x86);
  fs.unlinkSync(x86);
  assert.equal(target(), undefined);
});

test("integrated WSL requires a real PATH wsl executable and no distro or Windows Terminal probe", () => {
  const { prefs, install } = shellFixture();
  install("local/Microsoft/WindowsApps/wsl.exe");
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "wsl"), false);
  const executable = install("path-bin/wsl.exe");
  assert.deepEqual(plain(prefs.terminalShellTargets().find(target => target.id === "wsl")),
    { id: "wsl", label: "WSL", executable, args: [] });
  fs.unlinkSync(executable);
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "wsl"), false);
});

test("explicit pwsh survives cold preferences and cannot downgrade when its executable disappears", () => {
  const { prefs, env, file, install } = shellFixture();
  const old = install("path-bin/powershell.exe");
  const installedPwsh = install("programs/PowerShell/7/pwsh.exe");
  const pathPwsh = install("path-bin/pwsh.exe");
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "pwsh"), false);
  assert.equal(prefs.resolveTerminalShell().executable, pathPwsh);
  prefs.setPreferences({ terminalShell: "pwsh" });
  prefs.init(file);
  assert.equal(prefs.getPreferences().terminalShell, "pwsh");
  assert.equal(prefs.resolveTerminalShell().executable, installedPwsh);
  fs.unlinkSync(installedPwsh);
  assert.equal(prefs.resolveTerminalShell().executable, pathPwsh);
  fs.unlinkSync(pathPwsh);
  assert.throws(() => prefs.resolveTerminalShell(), /selected terminal shell is no longer installed/);
  assert.equal(prefs.getPreferences().terminalShell, "pwsh");
  assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).terminalShell, "pwsh");
  assert.equal(prefs.terminalShellTargets().find(target => target.id === "powershell").executable, old);
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "pwsh"), false);
  assert.ok(env.ProgramFiles);
});

test("saved system shell retains the actual OS shell after restart without changing agent driver settings", () => {
  const { prefs, env, file, install } = shellFixture();
  const cmd = install("path-bin/cmd.exe");
  const custom = install("custom/custom-shell.exe");
  prefs.setPreferences({ terminalShell: "system" });
  prefs.init(file);
  assert.equal(prefs.resolveTerminalShell().executable, cmd);
  env.SHELL = custom;
  assert.equal(prefs.resolveTerminalShell().executable, custom);
  assert.deepEqual(plain(prefs.resolveTerminalShell().args), []);
  assert.equal(prefs.getPreferences().terminalShell, "system");
  assert.equal(Object.keys(prefs.getPreferences()).some(key => /agent.*environment|wsl.*driver/i.test(key)), false);
  const unix = shellFixture({ SHELL: "/bin/zsh" }, "darwin");
  unix.prefs.setPreferences({ terminalShell: "system" });
  unix.prefs.init(unix.file);
  assert.deepEqual(plain(unix.prefs.resolveTerminalShell()), { id: "system", label: "/bin/zsh", executable: "/bin/zsh", args: [] });
});

test("Source vanished ordinary choices resolve a real default without changing the saved choice", () => {
  const { prefs, file, install } = shellFixture();
  const ps = install("path-bin/powershell.exe");
  install("path-bin/cmd.exe");
  prefs.setPreferences({ terminalShell: "wsl" });
  prefs.init(file);
  assert.equal(prefs.resolveTerminalShell().executable, ps);
  assert.equal(prefs.getPreferences().terminalShell, "wsl");
  assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).terminalShell, "wsl");
  assert.equal(prefs.terminalShellTargets().some(target => target.id === "wsl"), false);
});

test("browser history retains real visits, sources, titles, and selected removals after restart", () => {
  const { directory } = fixture();
  const file = path.join(directory, "history.json");
  history.init(file);
  const one = history.recordVisit("https://one.example/path", "Old title", "agent");
  const two = history.recordVisit("https://two.example/", "Other page", "other");
  history.updateTitle(one, "Actual page title");
  history.init(file);
  assert.equal(history.search({ source: "agent", text: "actual" })[0].id, one);
  assert.equal(history.search({ source: "other" })[0].id, two);
  history.remove([one]);
  assert.deepEqual(history.search().map(visit => visit.id), [two]);
  history.clear();
  history.init(file);
  assert.deepEqual(history.search(), []);
});

test("history removal deletes the selected URL on its local calendar day across visit sources", (t) => {
  const { directory } = fixture();
  const file = path.join(directory, "history.json");
  history.init(file);
  const day = new Date(2026, 9, 13, 12).getTime();
  let timestamp = day;
  t.mock.method(Date, "now", () => timestamp);
  const selected = history.recordVisit("https://same.example/page", "First visit", "agent");
  timestamp += 60000;
  history.recordVisit("https://same.example/page", "Reopened visit", "other");
  timestamp = day - 86400000;
  const previousDay = history.recordVisit("https://same.example/page", "Previous day", "other");
  timestamp = day;
  const anotherUrl = history.recordVisit("https://another.example/page", "Another page", "agent");
  history.init(file);
  history.remove([selected]);
  history.init(file);
  assert.deepEqual(history.search().map((visit) => visit.id), [anotherUrl, previousDay]);
});

test("native selection preserves backend-minted roots and restores only the exact task directory", () => {
  const { directory } = fixture();
  const first = path.join(directory, "first");
  const task = path.join(directory, "base", "date", "task");
  const picked = path.join(directory, "picked");
  fs.mkdirSync(first); fs.mkdirSync(task, { recursive: true }); fs.mkdirSync(picked);
  const ledger = path.join(directory, "trusted_workspaces.json");
  fs.writeFileSync(ledger, JSON.stringify({ version: 1, roots: [first] }));
  security.init({ initialRoots: [first], trustedRootsFile: ledger });
  fs.writeFileSync(ledger, JSON.stringify({ version: 1, roots: [first, task] }));
  security.rememberTrustedWorkspaceRoot(picked);
  assert.equal(JSON.parse(fs.readFileSync(ledger, "utf8")).roots.includes(task), true);
  assert.equal(security.restoreTrustedWorkspaceRoot(task), fs.realpathSync(task));
  assert.equal(security.restoreTrustedWorkspaceRoot(path.join(directory, "base")), "");
  assert.equal(security.isWithinTrustedWorkspace(path.join(directory, "base", "another-task")), false);
});

test("the selected shell is actually passed to native spawn rather than only changing a label", () => {
  const { file } = fixture();
  const expectedId = process.platform === "win32" ? "cmd" : "system";
  preferences.setPreferences({ terminalShell: expectedId });
  preferences.init(file);
  let spawned;
  let exit;
  ptyManager.init({ pty: { spawn: (shell, args) => { spawned = { shell, args }; return { pid: 0, process: shell, write() {}, resize() {}, kill() {}, onData() {}, onExit(callback) { exit = callback; } }; } },
    sanitizedPtyEnv: () => ({}), assertTrustedPath: value => value, getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }) });
  const result = ptyManager.spawnSession(process.cwd(), "shell-owner", "test-window");
  assert.equal(spawned.shell, preferences.terminalShellTargets().find(target => target.id === expectedId).executable);
  assert.deepEqual(spawned.args, []);
  exit({ exitCode: 0 });
  ptyManager.acknowledgeExitedSession(result.session_id, "shell-owner", "test-window");
});

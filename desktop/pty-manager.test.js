"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const manager = require("./pty-manager");
const utils = require("./utils");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const preferences = require("./preferences");

function isolatedShellModules() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-pty-shell-test-"));
  const bin = path.join(directory, "bin");
  fs.mkdirSync(bin);
  const env = { Path: bin, LOCALAPPDATA: path.join(directory, "local"), ProgramFiles: path.join(directory, "programs") };
  const preferenceBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preferences.js"), "utf8"), {
    module: preferenceBox, process: { platform: "win32", env }, console,
    require(name) { return name === "./open-targets" ? { FILE_OPEN_TARGET_IDS: ["minicode", "system"], fileOpenTargets: () => [] } : require(name); },
  });
  const prefs = preferenceBox.exports;
  const file = path.join(directory, "preferences.json");
  prefs.init(file);
  const managerBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "pty-manager.js"), "utf8"), {
    module: managerBox, process: { platform: "win32", env, cwd: () => directory }, console, setTimeout, clearTimeout,
    require(name) { return name === "./preferences" ? prefs : require(name); },
  });
  for (const executable of ["pwsh.exe", "powershell.exe", "cmd.exe", "git-bash.exe", "wsl.exe"]) {
    fs.writeFileSync(path.join(bin, executable), "fixture executable");
  }
  return { directory, bin, file, prefs, manager: managerBox.exports };
}

test("native terminal inherits the desktop GitHub runtime while excluding authentication secrets", () => {
  const path = require("node:path");
  const pathKey = Object.keys(process.env).find((name) => name.toLowerCase() === "path") || "PATH";
  const previousPath = process.env[pathKey];
  const previousCommand = process.env.MINICODE_GH_COMMAND;
  const previousToken = process.env.GH_TOKEN;
  const githubDirectory = path.resolve(__dirname, "github-runtime", "bin");
  let spawnEnvironment;
  let onExit;
  process.env[pathKey] = [githubDirectory, previousPath].filter(Boolean).join(path.delimiter);
  process.env.MINICODE_GH_COMMAND = path.join(githubDirectory, "gh.exe");
  process.env.GH_TOKEN = "not-forwarded-to-shell";
  manager.init({
    pty: { spawn: (_shell, _args, options) => {
      spawnEnvironment = options.env;
      return { pid: 0, process: "powershell", write: () => {}, resize: () => {}, kill: () => {},
        onData: () => {}, onExit: (callback) => { onExit = callback; } };
    } },
    sanitizedPtyEnv: utils.sanitizedPtyEnv,
    assertTrustedPath: (cwd) => cwd,
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });
  try {
    const session = manager.spawnSession(process.cwd(), "conv_github_runtime", "fixture-window");
    assert.equal(spawnEnvironment[pathKey].split(path.delimiter)[0], githubDirectory);
    assert.equal(spawnEnvironment.MINICODE_GH_COMMAND, path.join(githubDirectory, "gh.exe"));
    assert.equal(spawnEnvironment.GH_TOKEN, undefined);
    onExit({ exitCode: 0 });
    manager.acknowledgeExitedSession(session.session_id, "conv_github_runtime", "fixture-window");
  } finally {
    if (previousPath === undefined) delete process.env[pathKey]; else process.env[pathKey] = previousPath;
    if (previousCommand === undefined) delete process.env.MINICODE_GH_COMMAND; else process.env.MINICODE_GH_COMMAND = previousCommand;
    if (previousToken === undefined) delete process.env.GH_TOKEN; else process.env.GH_TOKEN = previousToken;
  }
});

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
    getWindowByInstanceId: () => ({
      isDestroyed: () => false,
      webContents: { send: (channel, payload) => sent.push({ channel, payload }) },
    }),
  });

  const conversationId = "conv_terminal_owner";
  const spawned = manager.spawnSession("C:\\workspace", conversationId, "fixture-window");
  onData("hello");
  onData(" world");

  assert.deepEqual(manager.listActiveSessions(), [{
    session_id: spawned.session_id,
    conversation_id: conversationId,
    window_instance_id: "fixture-window",
  }]);

  const snapshot = manager.snapshotSession(spawned.session_id, 7, conversationId, "fixture-window");
  assert.equal(snapshot.output, "o world");
  assert.equal(snapshot.output_start_cursor, 4);
  assert.equal(snapshot.output_end_cursor, 11);
  assert.equal(snapshot.total_output_chars, 11);
  assert.deepEqual(sent.filter((entry) => entry.channel === "minicode:pty:data").map((entry) => entry.payload), [
    { sessionId: spawned.session_id, conversationId, data: "hello", startCursor: 0, endCursor: 5 },
    { sessionId: spawned.session_id, conversationId, data: " world", startCursor: 5, endCursor: 11 },
  ]);

  assert.deepEqual(manager.clearSession(spawned.session_id, "conv-other", "fixture-window"), {
    cleared: false,
    outputCursor: 0,
  });
  assert.deepEqual(manager.clearSession(spawned.session_id, conversationId, "fixture-window"), {
    cleared: true,
    outputCursor: 11,
  });
  const cleared = manager.snapshotSession(spawned.session_id, undefined, conversationId, "fixture-window");
  assert.equal(cleared.output, "");
  assert.equal(cleared.output_start_cursor, 11);
  assert.equal(cleared.output_end_cursor, 11);
  onData("next");
  const afterClear = manager.snapshotSession(spawned.session_id, undefined, conversationId, "fixture-window");
  assert.equal(afterClear.output, "next");
  assert.equal(afterClear.output_start_cursor, 11);
  assert.equal(afterClear.output_end_cursor, 15);

  onExit({ exitCode: 0 });
  assert.deepEqual(manager.listActiveSessions(), []);
  const exited = manager.snapshotSession(spawned.session_id, undefined, conversationId, "fixture-window");
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
  assert.equal(manager.listSessions(conversationId, undefined, "fixture-window").some((session) => session.session_id === spawned.session_id), true);
  assert.equal(manager.acknowledgeExitedSession(spawned.session_id, conversationId, "fixture-window"), true);
  assert.equal(manager.snapshotSession(spawned.session_id, undefined, conversationId, "fixture-window"), null);
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
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });
  try {
    const ids = [];
    for (let index = 0; index < 27; index += 1) {
      ids.push(manager.spawnSession("C:\\workspace", "conv_tombstones", "fixture-window").session_id);
      now += 1;
      exits[index]({ exitCode: index });
    }
    assert.equal(manager.listSessions("conv_tombstones", undefined, "fixture-window").length, 24);
    assert.equal(manager.snapshotSession(ids[0], undefined, "conv_tombstones", "fixture-window"), null);
    assert.notEqual(manager.snapshotSession(ids.at(-1), undefined, "conv_tombstones", "fixture-window"), null);

    now += 31 * 60 * 1000;
    manager.pruneExitedSessions(now);
    assert.equal(manager.listSessions("conv_tombstones", undefined, "fixture-window").length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("large pty writes are chunked without truncating pasted input", async () => {
  const writes = [];
  let onExit;
  manager.init({
    pty: {
      spawn: () => ({
        pid: 0,
        process: "pwsh",
        write: (value) => writes.push(value),
        resize: () => {},
        kill: () => onExit({ exitCode: 0 }),
        onData: () => {},
        onExit: (callback) => { onExit = callback; },
      }),
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });

  const conversationId = "conv_large_write";
  const spawned = manager.spawnSession("C:\\workspace", conversationId, "fixture-window");
  const pasted = "x".repeat(20_000);
  assert.equal(manager.writeToSession(spawned.session_id, pasted, conversationId, "fixture-window"), true);
  assert.deepEqual(writes.map((value) => value.length), [8192, 8192, 3616]);
  assert.equal(writes.join(""), pasted);
  await manager.killConversation(conversationId, undefined, "fixture-window");
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
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });

  assert.throws(
    () => manager.spawnSession("C:\\workspace", "", "fixture-window"),
    /conversation owner is required/i,
  );

  const owned = manager.spawnSession("C:\\workspace", "conv_owner_a", "fixture-window");
  assert.equal(manager.listSessions("", undefined, "fixture-window").length, 0);
  assert.equal(manager.listSessions("conv_owner_b", undefined, "fixture-window").length, 0);
  assert.equal(manager.snapshotSession(owned.session_id, undefined, "conv_owner_b", "fixture-window"), null);
  assert.equal(manager.writeToSession(owned.session_id, "blocked", "conv_owner_b", "fixture-window"), false);
  manager.resizeSession(owned.session_id, 120, 40, "conv_owner_b", "fixture-window");
  assert.deepEqual(writes, []);
  assert.deepEqual(resizes, []);
  assert.equal(await manager.killSession(owned.session_id, "conv_owner_b", "fixture-window"), false);
  assert.equal(manager.listSessions("conv_owner_a", undefined, "fixture-window").length, 1);

  exits[0]({ exitCode: 0 });
  assert.equal(manager.acknowledgeExitedSession(owned.session_id, "conv_owner_b", "fixture-window"), false);
  assert.equal(manager.acknowledgeExitedSession(owned.session_id, "conv_owner_a", "fixture-window"), true);
});

test("killConversation terminates only sessions owned by that conversation", async () => {
  const killed = [];
  let pid = 0;
  manager.init({
    pty: {
      spawn: () => {
        const current = pid++;
        let onExit;
        return {
          pid: 0,
          process: "pwsh",
          write: () => {},
          resize: () => {},
          kill: () => {
            killed.push(current);
            onExit({ exitCode: 0 });
          },
          onData: () => {},
          onExit: (callback) => { onExit = callback; },
        };
      },
    },
    sanitizedPtyEnv: () => ({}),
    assertTrustedPath: (cwd) => cwd,
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });

  manager.spawnSession("C:\\workspace", "conv_cleanup_a", "fixture-window");
  manager.spawnSession("C:\\workspace", "conv_cleanup_a", "fixture-window");
  manager.spawnSession("C:\\workspace", "conv_cleanup_b", "fixture-window");

  assert.equal(await manager.killConversation("conv_cleanup_a", undefined, "fixture-window"), 2);
  assert.deepEqual(killed, [0, 1]);
  assert.equal(manager.listSessions("conv_cleanup_a", undefined, "fixture-window").length, 0);
  assert.equal(manager.listSessions("conv_cleanup_b", undefined, "fixture-window").length, 1);

  assert.equal(await manager.killConversation("conv_cleanup_b", undefined, "fixture-window"), 1);
  assert.deepEqual(killed, [0, 1, 2]);
});

test("archive termination retains exited PTY output and the other conversation's live shell", async () => {
  const callbacks = [];
  manager.init({
    pty: { spawn: () => {
      const listeners = {};
      callbacks.push(listeners);
      return { pid: 0, process: "pwsh", write() {}, resize() {},
        kill() { listeners.exit({ exitCode: 0 }); },
        onData(callback) { listeners.data = callback; },
        onExit(callback) { listeners.exit = callback; },
      };
    } },
    sanitizedPtyEnv: () => ({}), assertTrustedPath: (cwd) => cwd, getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });
  const archived = manager.spawnSession("C:\\workspace", "archive-native-owner", "fixture-window");
  const other = manager.spawnSession("C:\\workspace", "other-native-owner", "fixture-window");
  callbacks[0].data("Previous command output\r\n");
  assert.equal(await manager.killConversation("archive-native-owner", true, "fixture-window"), 1);
  const snapshot = manager.snapshotSession(archived.session_id, 8000, "archive-native-owner", "fixture-window");
  assert.equal(snapshot.is_alive, false);
  assert.match(snapshot.output, /Previous command output/);
  assert.equal(manager.listSessions("other-native-owner", undefined, "fixture-window")[0].is_alive, true);
  manager.acknowledgeExitedSession(archived.session_id, "archive-native-owner", "fixture-window");
  await manager.killConversation("other-native-owner", undefined, "fixture-window");
  assert.equal(manager.snapshotSession(other.session_id, 100, "other-native-owner", "fixture-window"), null);
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
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });

  const original = manager.spawnSession("C:\\workspace", "conv_restart", "fixture-window");
  exits[0]({ exitCode: 0 });

  assert.equal(await manager.restartSession(original.session_id, "conv_other", "fixture-window"), null);
  const replacement = await manager.restartSession(original.session_id, "conv_restart", "fixture-window");

  assert.notEqual(replacement.session_id, original.session_id);
  assert.equal(replacement.cwd, "C:\\workspace");
  assert.deepEqual(
    manager.listSessions("conv_restart", undefined, "fixture-window").map((session) => session.session_id),
    [replacement.session_id],
  );
});

test("cold saved shell choices feed exact executable and Source args to the owned PTY", () => {
  const { directory, bin, file, prefs, manager: isolated } = isolatedShellModules();
  const calls = [];
  let exit;
  isolated.init({
    pty: { spawn(shell, args, options) {
      calls.push({ shell, args: Array.from(args), cwd: options.cwd });
      return { pid: 0, process: shell, write() {}, resize() {}, kill() {}, onData() {}, onExit(callback) { exit = callback; } };
    } },
    sanitizedPtyEnv: () => ({}), assertTrustedPath: value => value,
    getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send() {} } }),
  });
  const expected = {
    powershell: { shell: path.join(bin, "pwsh.exe"), args: [] },
    cmd: { shell: path.join(bin, "cmd.exe"), args: [] },
    gitBash: { shell: path.join(bin, "git-bash.exe"), args: ["--login", "-i"] },
    wsl: { shell: path.join(bin, "wsl.exe"), args: [] },
    pwsh: { shell: path.join(bin, "pwsh.exe"), args: [] },
    system: { shell: path.join(bin, "cmd.exe"), args: [] },
  };
  for (const [choice, command] of Object.entries(expected)) {
    prefs.setPreferences({ terminalShell: choice });
    prefs.init(file);
    const session = isolated.spawnSession(directory, "owned-shell-choice", "owned-window");
    assert.deepEqual(calls.at(-1), { ...command, cwd: directory });
    assert.equal(session.shell, command.shell);
    assert.equal(prefs.getPreferences().terminalShell, choice);
    exit({ exitCode: 0 });
    isolated.acknowledgeExitedSession(session.session_id, "owned-shell-choice", "owned-window");
  }
  prefs.setPreferences({ terminalShell: "gitBash" });
  fs.unlinkSync(path.join(bin, "git-bash.exe"));
  const replacement = isolated.spawnSession(directory, "owned-shell-choice", "owned-window");
  assert.deepEqual(calls.at(-1), { shell: path.join(bin, "pwsh.exe"), args: [], cwd: directory });
  assert.equal(prefs.getPreferences().terminalShell, "gitBash");
  exit({ exitCode: 0 });
  isolated.acknowledgeExitedSession(replacement.session_id, "owned-shell-choice", "owned-window");
  prefs.setPreferences({ terminalShell: "pwsh" });
  fs.unlinkSync(path.join(bin, "pwsh.exe"));
  assert.throws(() => isolated.spawnSession(directory, "owned-shell-choice", "owned-window"), /selected terminal shell is no longer installed/);
  assert.equal(calls.length, 7);
});

test("Windows natural exit disposes native resources once and retains the real exit tombstone", async () => {
  const { directory, manager: isolated } = isolatedShellModules();
  const exits = [];
  let nativeKills = 0;
  const sent = [];
  isolated.init({ pty: { spawn() {
    let onExit;
    return { pid: 0, write() {}, resize() {}, onData() {},
      kill() { nativeKills += 1; onExit({ exitCode: 0 }); },
      onExit(callback) { onExit = callback; exits.push(callback); } };
  } }, sanitizedPtyEnv: () => ({}), assertTrustedPath: value => value,
  getWindowByInstanceId: () => ({ isDestroyed: () => false, webContents: { send(channel, value) { sent.push({ channel, value }); } } }) });
  const natural = isolated.spawnSession(directory, "natural-owner", "window");
  exits[0]({ exitCode: 7 });
  assert.equal(nativeKills, 1);
  assert.equal(isolated.snapshotSession(natural.session_id, undefined, "natural-owner", "window").exit_code, 7);
  assert.equal(sent.filter(event => event.channel === "minicode:pty:exit").length, 1);
  assert.equal(sent.find(event => event.channel === "minicode:pty:exit").value.exitCode, 7);
  await isolated.killSession(natural.session_id, "natural-owner", "window");
  assert.equal(nativeKills, 1);
  const requested = isolated.spawnSession(directory, "requested-owner", "window");
  await isolated.killSession(requested.session_id, "requested-owner", "window");
  assert.equal(nativeKills, 2);
  assert.equal(isolated.snapshotSession(requested.session_id, undefined, "requested-owner", "window"), null);
});

async function realShellRoundtrip(t, choice) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), `minicode-pty-real-${choice}-`));
  const file = path.join(directory, "preferences.json");
  preferences.init(file);
  const target = preferences.terminalShellTargets().find(target => target.id === choice);
  if (!target) {
    t.skip(`${choice} is not installed on this test host`);
    return;
  }
  preferences.setPreferences({ terminalShell: choice });
  preferences.init(file);
  const nativePty = require("node-pty");
  let spawned;
  let settleExit;
  let acceptOutput;
  let rejectOutput;
  let output = "";
  const exited = new Promise(resolve => { settleExit = resolve; });
  const received = new Promise((resolve, reject) => { acceptOutput = resolve; rejectOutput = reject; });
  const marker = `MINICODE_PTY_REAL_${choice}_${Date.now()}`;
  const expected = choice === "powershell" ? `终端中文|${marker}` : `${marker}_EXECUTED`;
  const capture = { isDestroyed: () => false, webContents: { send(channel, value) {
    if (channel === "minicode:pty:data") {
      output += value.data;
      if (output.includes(expected)) acceptOutput();
    }
    if (channel === "minicode:pty:exit") settleExit(value);
  } } };
  manager.init({ pty: { spawn(shell, args, options) {
    spawned = { shell, args: Array.from(args) };
    return nativePty.spawn(shell, args, options);
  } }, sanitizedPtyEnv: () => ({ ...utils.sanitizedPtyEnv(),
    HOME: directory.replaceAll("\\", "/"), USERPROFILE: directory,
    APPDATA: path.join(directory, "appdata"), LOCALAPPDATA: path.join(directory, "local"),
    HISTFILE: path.join(directory, "shell-history"), XDG_CONFIG_HOME: path.join(directory, "config") }),
  assertTrustedPath: value => value, getWindowByInstanceId: () => capture });
  const owner = `real-shell-${choice}`;
  const window = `real-window-${choice}`;
  const session = manager.spawnSession(directory, owner, window);
  const timer = setTimeout(() => rejectOutput(new Error(`No actual ${choice} output: ${output.slice(-1000)}`)), 15000);
  try {
    assert.deepEqual(spawned, { shell: target.executable, args: Array.from(target.args) });
    assert.equal(preferences.getPreferences().terminalShell, choice);
    const command = choice === "powershell"
      ? `[Console]::WriteLine((-join [char[]](0x7EC8,0x7AEF,0x4E2D,0x6587)) + '|${marker}')\r`
      : choice === "cmd" ? `set MINICODE_PTY_QA_SUFFIX=EXECUTED\recho ${marker}_%MINICODE_PTY_QA_SUFFIX%\r`
        : `unset HISTFILE; printf '%s_%s\\n' '${marker}' 'EXECUTED'\r`;
    assert.equal(manager.writeToSession(session.session_id, command, owner, window), true);
    await received;
    assert.equal(manager.snapshotSession(session.session_id, 20000, owner, window).output.includes(expected), true);
    clearTimeout(timer);
    manager.writeToSession(session.session_id, "exit\r", owner, window);
    let exitTimer;
    let exit;
    try {
      exit = await Promise.race([exited, new Promise((_, reject) => { exitTimer = setTimeout(() => reject(new Error(`${choice} did not exit`)), 5000); })]);
    } finally {
      clearTimeout(exitTimer);
    }
    assert.equal(exit.exitCode, 0);
    assert.equal(manager.acknowledgeExitedSession(session.session_id, owner, window), true);
  } finally {
    clearTimeout(timer);
    const remaining = manager.snapshotSession(session.session_id, 100, owner, window);
    if (remaining) await manager.killSession(session.session_id, owner, window);
  }
}

test("actual PowerShell PTY uses the cold preference and emits Unicode without injected initialization", { skip: process.platform !== "win32" }, async (t) => {
  await realShellRoundtrip(t, "powershell");
});

test("actual Command Prompt PTY executes input from the cold selected setting", { skip: process.platform !== "win32" }, async (t) => {
  await realShellRoundtrip(t, "cmd");
});

test("actual Git Bash PTY executes Source login interactive command from the cold setting", { skip: process.platform !== "win32" }, async (t) => {
  await realShellRoundtrip(t, "gitBash");
});

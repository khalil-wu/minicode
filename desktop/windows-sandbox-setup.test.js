"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { setupWindowsSandbox } = require("./windows-sandbox-setup");

const configuration = {
  platform: "win32", powershellCommand: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
  scriptPath: "C:\\Projects\\MiniCode\\desktop\\scripts\\setup-desktop-user-sandbox.ps1",
  apiBaseUrl: "http://127.0.0.1:8123", runtimeToken: "fixture-runtime-auth", shellProcessId: 4321,
  fetchStatus: async () => ({ ok: true, json: async () => sandboxConfiguration }),
};
const sandboxConfiguration = {
  sandbox_executable: "E:\\Custom Runtime\\codex.exe", sandbox_home: "D:\\Custom Home\\alice's sandbox",
};

test("desktop initialization waits for elevated exit and preserves the original user root and identity", async () => {
  let complete;
  let invocation;
  const setup = setupWindowsSandbox({ ...configuration, execFileProcess: (...args) => {
    invocation = args; complete = args[3];
  } });
  let settled = false;
  void setup.then(() => { settled = true; });
  await new Promise(setImmediate);
  assert.equal(settled, false);
  assert.equal(invocation[0], configuration.powershellCommand);
  assert.equal(invocation[2].windowsHide, true);
  const encoded = invocation[1].at(-1);
  const command = Buffer.from(encoded, "base64").toString("utf16le");
  assert.match(command, /-Verb RunAs -WindowStyle Hidden -Wait -PassThru/);
  assert.ok(command.includes("'-ShellProcessId', '4321'"));
  assert.ok(command.includes("'-SandboxHome', '\"D:\\Custom Home\\alice''s sandbox\"'"));
  assert.ok(command.includes(sandboxConfiguration.sandbox_executable));
  assert.ok(command.includes(configuration.scriptPath));
  assert.equal(command.includes(configuration.runtimeToken), false);
  complete(null, "", "");
  assert.deepEqual(await setup, { ok: true });
});

test("concurrent initialization is rejected and the lock releases after UAC cancellation", async () => {
  let complete;
  const options = { ...configuration, execFileProcess: (_command, _args, _options, callback) => { complete = callback; } };
  const first = setupWindowsSandbox(options);
  const second = await setupWindowsSandbox(options);
  assert.equal(second.ok, false);
  assert.match(second.error, /正在初始化/);
  await new Promise(setImmediate);
  complete({ code: 1223, message: "UAC cancelled" }, "", "");
  assert.deepEqual(await first, { ok: false, cancelled: true });
  const next = setupWindowsSandbox(options);
  await new Promise(setImmediate);
  complete(null, "", "");
  assert.deepEqual(await next, { ok: true });
});

test("failed initialization reports the real helper failure and does not claim success", async () => {
  const result = await setupWindowsSandbox({ ...configuration,
    execFileProcess: (_command, _args, _options, callback) => callback({ code: 1, message: "Helper exit 1" }, "", "Native initialization failed: owner SID disagrees.\n"),
  });
  assert.deepEqual(result, { ok: false, error: "Native initialization failed: owner SID disagrees." });
});

test("initialization reads the authenticated backend authority and does not start with a missing configured runtime", async () => {
  let spawned = false;
  const result = await setupWindowsSandbox({ ...configuration,
    fetchStatus: async (url, options) => {
      assert.equal(url, "http://127.0.0.1:8123/api/sandbox/status");
      assert.equal(options.headers["x-minicode-token"], "fixture-runtime-auth");
      return { ok: true, json: async () => ({ ...sandboxConfiguration, sandbox_executable: null }) };
    }, execFileProcess: () => { spawned = true; },
  });
  assert.equal(result.ok, false);
  assert.match(result.error, /后端配置.*尚未安装/);
  assert.equal(spawned, false);
});

test("a backend status failure prevents elevation and reports a readable error", async () => {
  let spawned = false;
  const result = await setupWindowsSandbox({ ...configuration,
    fetchStatus: async () => ({ ok: false, status: 502 }), execFileProcess: () => { spawned = true; },
  });
  assert.equal(result.ok, false);
  assert.match(result.error, /HTTP 502/);
  assert.equal(spawned, false);
});

test("an empty helper stderr reports its exit code without leaking the encoded command into the UI", async () => {
  const result = await setupWindowsSandbox({ ...configuration,
    execFileProcess: (_command, _args, _options, callback) => callback({ code: 7, message: "powershell -EncodedCommand giant-base64-text" }, "", ""),
  });
  assert.deepEqual(result, { ok: false, error: "Windows 执行环境初始化未完成（退出码 7）。" });
  assert.equal(result.error.includes("EncodedCommand"), false);
  assert.equal(result.error.includes("giant-base64"), false);
});

test("non-Windows calls return a clear unavailable result without launching a helper", async () => {
  let spawned = false;
  const result = await setupWindowsSandbox({ ...configuration, platform: "linux", execFileProcess: () => { spawned = true; } });
  assert.equal(result.ok, false);
  assert.equal(spawned, false);
});

test("preload exposes sandbox setup as an explicit parameter-free IPC invocation", async () => {
  let runtime;
  const calls = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: () => ({
      ipcRenderer: { sendSync: () => ({ apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000" }),
        on() {}, invoke: (...args) => { calls.push(args); return Promise.resolve({ ok: false, cancelled: true }); } },
      contextBridge: { exposeInMainWorld: (_key, value) => { runtime = value; } },
    }),
    process: { env: {}, argv: [], contextIsolated: true, platform: "win32", arch: "x64" }, window: {}, console,
  });
  assert.equal(calls.length, 0);
  assert.deepEqual(await runtime.desktop.sandbox.setup(), { ok: false, cancelled: true });
  assert.deepEqual(calls, [["minicode:sandbox:setup"]]);
});

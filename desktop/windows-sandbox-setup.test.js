"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
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

test("network initialization uses the authenticated current ingress settings and its actual runtime owner", async () => {
  const network = { required: true, ready: false, settings: { proxy_ports: [34321, 34322], allow_local_binding: true },
    sandbox_home: "E:\\Actual Network\\alice's home", sandbox_executable: "E:\\Actual Runtime\\codex.exe" };
  let command;
  const result = await setupWindowsSandbox({ ...configuration,
    fetchStatus: async (url, options) => {
      assert.equal(url, `${configuration.apiBaseUrl}/api/sandbox/status`);
      assert.equal(options.headers["x-minicode-token"], configuration.runtimeToken);
      return { ok: true, json: async () => ({ ...sandboxConfiguration, network_provisioning: network }) };
    }, execFileProcess: (_program, args, _options, callback) => {
      command = Buffer.from(args.at(-1), "base64").toString("utf16le");
      callback(null, "", "");
    },
  });
  assert.deepEqual(result, { ok: true });
  assert.ok(command.includes(network.sandbox_executable));
  assert.ok(command.includes(network.sandbox_home.replace(/'/g, "''")));
  assert.equal(command.includes(sandboxConfiguration.sandbox_executable), false);
  assert.equal(command.includes(sandboxConfiguration.sandbox_home), false);
  const encoded = command.match(/'-ProvisioningSettingsBase64', '([^']+)'/)[1];
  assert.deepEqual(JSON.parse(Buffer.from(encoded, "base64").toString("utf8")), network.settings);
  assert.equal(command.includes(configuration.runtimeToken), false);
});

test("required network authority never falls back to unrelated top-level home or executable", async () => {
  let spawned = false;
  const result = await setupWindowsSandbox({ ...configuration,
    fetchStatus: async () => ({ ok: true, json: async () => ({ ...sandboxConfiguration,
      network_provisioning: { required: true, settings: { proxy_ports: [34321], allow_local_binding: false },
        sandbox_home: "D:\\actual-owner", sandbox_executable: null } }) }),
    execFileProcess: () => { spawned = true; },
  });
  assert.equal(result.ok, false);
  assert.equal(spawned, false);
});

test("the elevation boundary accepts only the Source provisioning fields and typed actual ports", async (t) => {
  for (const settings of [
    { proxy_ports: [0], allow_local_binding: false },
    { proxy_ports: [65536], allow_local_binding: false },
    { proxy_ports: [3.5], allow_local_binding: false },
    { proxy_ports: ["34321"], allow_local_binding: false },
    { proxy_ports: [34321], allow_local_binding: "false" },
    { proxy_ports: [34321], allow_local_binding: false, permissions: { file_system: "unrestricted" } },
  ]) await t.test(JSON.stringify(settings), async () => {
    let spawned = false;
    const result = await setupWindowsSandbox({ ...configuration,
      fetchStatus: async () => ({ ok: true, json: async () => ({ ...sandboxConfiguration,
        network_provisioning: { required: true, settings, ...sandboxConfiguration } }) }),
      execFileProcess: () => { spawned = true; },
    });
    assert.equal(result.ok, false);
    assert.match(result.error, /网络隔离设置无效/);
    assert.equal(spawned, false);
  });
});

test("base filesystem initialization retains its default provisioning when no network execution requires ports", async () => {
  let command;
  const result = await setupWindowsSandbox({ ...configuration,
    execFileProcess: (_program, args, _options, callback) => {
      command = Buffer.from(args.at(-1), "base64").toString("utf16le");
      callback(null, "", "");
    },
  });
  assert.deepEqual(result, { ok: true });
  assert.equal(command.includes("ProvisioningSettingsBase64"), false);
});

for (const [kind, settings] of [["base", null], ["network", { proxy_ports: [34321, 34322], allow_local_binding: false }],
  ["reject-other-permissions", { proxy_ports: [34321], allow_local_binding: false, file_system: "unrestricted" }]]) {
  test(`Windows PowerShell passes exact native setup argv without running setup (${kind})`,
    { skip: process.platform !== "win32" && "Windows PowerShell native argument binder only" }, () => {
      const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-setup-arguments-"));
      const capture = path.join(directory, "capture.json");
      const stub = path.join(directory, "sandbox");
      fs.writeFileSync(stub, "require('node:fs').writeFileSync(process.env.MINICODE_SETUP_ARGUMENTS_FILE, JSON.stringify(process.argv.slice(2)));\n");
      const source = fs.readFileSync(path.join(__dirname, "scripts", "setup-desktop-user-sandbox.ps1"), "utf8");
      const nativeInvocation = source.slice(source.indexOf("# Source provisioning accepts only"));
      const literal = (value) => `'${value.replace(/'/g, "''")}'`;
      const sandboxHome = "D:\\Source Owner\\alice's sandbox";
      const command = `$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath ${literal(directory)}
$RuntimePath = ${literal(process.execPath)}
$SandboxHome = ${literal(sandboxHome)}
$account = 'OWNED\\desktop-user'
$ProvisioningSettingsBase64 = '${settings ? Buffer.from(JSON.stringify(settings), "utf8").toString("base64") : ""}'
${nativeInvocation}`;
      try {
        const invoke = () => execFileSync(configuration.powershellCommand, ["-NoProfile", "-NonInteractive", "-EncodedCommand",
          Buffer.from(command, "utf16le").toString("base64")], { windowsHide: true,
          env: { ...process.env, MINICODE_SETUP_ARGUMENTS_FILE: capture }, stdio: "pipe" });
        if (kind === "reject-other-permissions") {
          assert.throws(invoke, (error) => error.status !== 0 && error.stderr.toString().includes("Invalid Source Windows sandbox provisioning settings"));
          assert.equal(fs.existsSync(capture), false);
          return;
        }
        invoke();
        const argv = JSON.parse(fs.readFileSync(capture, "utf8"));
        assert.deepEqual(argv.slice(0, 6), ["setup", "--elevated", "--user", "OWNED\\desktop-user", "--codex-home", sandboxHome]);
        if (settings) {
          assert.equal(argv[6], "--settings-json");
          assert.deepEqual(JSON.parse(argv[7]), settings);
          assert.equal(argv.length, 8);
        } else assert.equal(argv.length, 6);
      } finally {
        fs.rmSync(capture, { force: true });
        fs.unlinkSync(stub);
        fs.rmdirSync(directory);
      }
    });
}

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

test("a remote window's authenticated setup invocation cannot initialize the local Windows host", async () => {
  const handlers = new Map();
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), {
    module: moduleBox, process, console,
    require(name) {
      if (name === "electron") return { ipcMain: { on() {}, handle(channel, handler) { handlers.set(channel, handler); } } };
      return require(name);
    },
  });
  const mainFrame = { url: "file:///minicode/index.html" };
  const webContents = { mainFrame, getURL: () => mainFrame.url };
  const win = { webContents, isDestroyed: () => false };
  let localSetupCalls = 0;
  moduleBox.exports.init({ getWindowForSender: (sender) => sender === webContents ? win : null,
    getWindowRecord: () => ({ windowInstanceId: "owned-remote-window", remoteHostId: "owned-remote" }),
    getRemoteDesktop: () => ({ invokes() { throw new Error("Remote setup is not a forwarded native capability"); } }),
    setupWindowsSandbox: () => { localSetupCalls += 1; return Promise.resolve({ ok: true }); },
  });
  moduleBox.exports.registerIpcHandlers();
  const result = await handlers.get("minicode:sandbox:setup")({ sender: webContents, senderFrame: mainFrame },
    { sandbox_home: "C:\\foreign-owner", settings: { allow_local_binding: true } });
  assert.equal(result.ok, false);
  assert.equal(result.error, "请在远端 Windows 主机上初始化沙箱。");
  assert.equal(localSetupCalls, 0);
});

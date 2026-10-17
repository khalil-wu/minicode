"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { RemoteHost, transportCommand } = require("./remote-host");
const bridge = require("./embedded-browser-bridge");

test("disconnect terminates an owned transport that has not reached remote readiness", { timeout: 10_000 }, async () => {
  let child;
  const host = new RemoteHost({ id: "not-ready", kind: "wsl", distribution: "owned", displayName: "Owned" }, "a".repeat(32), {
    bridgeEndpoint: "http://127.0.0.1:1", spawnTransport: (_program, _args, options) => {
      child = spawn(process.execPath, ["-e", "process.stdout.write('starting');setInterval(()=>{},1000)"], options);
      return child;
    },
  });
  const failed = assert.rejects(host.connect(), /Remote MiniCode transport exited/);
  await once(child, "spawn");
  await host.close();
  await failed;
  assert.ok(child.exitCode !== null || child.signalCode !== null);
  assert.equal(host.status, "disconnected");
});

test("SSH startup uses noninteractive authentication and login-shell PATH for probes and proxy", async () => {
  const calls = [];
  const connection = { id: "ssh-setup", kind: "ssh", sshHost: "alice@server", sshPort: 2222, identity: "C:/keys/my key", displayName: "Server" };
  const host = new RemoteHost(connection, "b".repeat(32), {
    spawnTransport: (program, args, options) => {
      calls.push({ program, args });
      const command = args.at(-1);
      const code = command.includes("command -v") ? "process.exit(0)"
        : command.includes("minicode-host --version") ? "process.stdout.write('MiniCode 0.2.0 desktop-host/1\\n')"
        : "process.stderr.write('remote proxy refused attachment');process.exit(23)";
      return spawn(process.execPath, ["-e", code], options);
    },
  });
  await assert.rejects(host.connect(), /Remote MiniCode transport exited \(23\): remote proxy refused attachment/);
  await host.close();
  assert.equal(host.installedVersion, "MiniCode 0.2.0 desktop-host/1");
  assert.equal(host.status, "disconnected");
  assert.equal(calls.length, 3);
  for (const { program, args } of calls) {
    assert.equal(program, "ssh");
    assert.deepEqual(args.slice(0, -1), ["-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=12",
      "-o", "ExitOnForwardFailure=yes", "-p", "2222", "-i", "C:/keys/my key", "--", "alice@server"]);
    assert.match(args.at(-1), /\$SHELL/);
    assert.match(args.at(-1), /MINICODE_INSTALL_DIR:-\$HOME\/\.local\/bin/);
  }
  assert.match(calls[0].args.at(-1), /command -v minicode-host/);
  assert.match(calls[1].args.at(-1), /minicode-host --version/);
  assert.match(calls[2].args.at(-1), /--marker/);
  assert.deepEqual(transportCommand({ kind: "wsl", distribution: "Ubuntu" }, "desktop", "marker"),
    ["wsl.exe", ["--distribution", "Ubuntu", "--exec", "minicode-host", "proxy", "--desktop-id", "desktop", "--marker", "marker"]]);
});

test("missing or broken remote CLI stops before proxy and preserves the actual SSH error", async () => {
  for (const scenario of [
    { results: [{ code: 86, stderr: "" }], error: /No `minicode-host` found in the remote login shell PATH/ },
    { results: [{ code: 255, stderr: "Permission denied (publickey)." }], error: /lookup exited \(255\): Permission denied \(publickey\)\./ },
    { results: [{ code: 0, stderr: "" }, { code: 1, stderr: "ModuleNotFoundError: filelock" }], error: /version check exited \(1\): ModuleNotFoundError: filelock/ },
  ]) {
    let attempts = 0;
    const host = new RemoteHost({ id: "broken", kind: "ssh", sshHost: "owned", displayName: "Owned" }, "c".repeat(32), {
      spawnTransport: (_program, _args, options) => {
        const result = scenario.results[attempts++];
        assert.ok(result, "must not start proxy after preflight failure");
        return spawn(process.execPath, ["-e", `process.stderr.write(${JSON.stringify(result.stderr)});process.exit(${result.code})`], options);
      },
    });
    await assert.rejects(host.connect(), scenario.error);
    assert.equal(attempts, scenario.results.length);
    assert.equal(host.status, "disconnected");
    assert.equal(host.closed, true);
  }
});

test("disconnect during SSH CLI lookup terminates that owned process without starting proxy", async () => {
  let child, attempts = 0;
  const host = new RemoteHost({ id: "lookup", kind: "ssh", sshHost: "owned", displayName: "Owned" }, "d".repeat(32), {
    spawnTransport: (_program, _args, options) => {
      attempts++;
      child = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"], options);
      return child;
    },
  });
  const failed = assert.rejects(host.connect(), /SSH MiniCode host lookup was disconnected/);
  await once(child, "spawn");
  await host.close();
  await failed;
  assert.equal(attempts, 1);
  assert.equal(host.status, "disconnected");
  assert.ok(child.exitCode !== null || child.signalCode !== null);
});

test("real MiniCode daemon multiplexes HTTP and reverse window binding, then reattaches to the same host", { timeout: 90_000 }, async (t) => {
  const root = path.resolve(__dirname, "..");
  const bundled = path.join(__dirname, "python-runtime", "python.exe");
  const python = process.env.MINICODE_TEST_PYTHON || (fs.existsSync(bundled) ? bundled : "python");
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-remote-host-chain-"));
  const desktopId = crypto.randomBytes(16).toString("hex");
  const workspace = path.join(state, "workspace");
  fs.mkdirSync(workspace);
  fs.writeFileSync(path.join(workspace, "owner.txt"), "remote-owned-file\n");
  const windows = new Map();
  bridge.init({ token: "local-backend-private", getWindowRecordByInstanceId: id => windows.get(id),
    manager: { async executeControlCommand() { return { ok: true }; } } });
  const endpoint = await bridge.start();
  const payload = crypto.randomBytes(8 * 1024 * 1024);
  const preview = http.createServer((request, response) => {
    if (request.method === "POST") {
      const hash = crypto.createHash("sha256");
      let bytes = 0;
      request.pause();
      request.on("data", chunk => { bytes += chunk.length; hash.update(chunk); });
      request.on("end", () => { response.setHeader("content-type", "application/json"); response.end(JSON.stringify({ bytes, sha256: hash.digest("hex") })); });
      setTimeout(() => request.resume(), 100);
    } else { response.writeHead(200, { "content-length": payload.length }); response.end(payload); }
  });
  preview.listen(0, "127.0.0.1");
  await once(preview, "listening");
  const peers = [];
  const children = [];
  const connection = { id: "owned-real-host", kind: "ssh", displayName: "Owned local transport", sshHost: "owned-test-peer" };
  const spawnTransport = (_program, args, options) => {
    const command = args.at(-1);
    if (command.includes("command -v")) return spawn(process.execPath, ["-e", "process.exit(0)"], options);
    if (command.includes("minicode-host --version")) return spawn(python, ["-m", "backend.desktop_remote", "--version"], { ...options, cwd: root });
    const marker = command.match(/[a-f0-9]{64}/)[0];
    const child = spawn(python, ["-m", "backend.desktop_remote", "proxy", "--desktop-id", desktopId, "--marker", marker, "--state-root", state],
      { ...options, cwd: root, env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8",
        MINICODE_CONFIG_DIR: path.join(state, "config"), MINICODE_MANAGED_SETTINGS_DIR: path.join(state, "managed"),
        MINICODE_REQUIREMENTS_FILE: path.join(state, "requirements.toml"), MINICODE_SYSTEM_CONFIG_FILE: path.join(state, "system.toml") } });
    children.push(child);
    return child;
  };
  const attach = async () => {
    const host = new RemoteHost(connection, desktopId, { bridgeEndpoint: endpoint, spawnTransport });
    peers.push(host);
    await host.connect();
    bridge.registerRemoteHost(connection.id, host);
    return host;
  };
  t.after(async () => {
    for (const peer of peers) { bridge.unregisterRemoteHost(connection.id, peer); await peer.close(); }
    if (fs.existsSync(path.join(state, "desktop-hosts", desktopId, "daemon.json"))) {
      const stopped = spawn(python, ["-m", "backend.desktop_remote", "stop", "--desktop-id", desktopId, "--state-root", state], { cwd: root, windowsHide: true, stdio: "pipe" });
      let stopError = "";
      stopped.stderr.on("data", chunk => { stopError += chunk; });
      const [code] = await once(stopped, "exit");
      assert.equal(code, 0, stopError);
    }
    await bridge.stop();
    await new Promise(resolve => preview.close(resolve));
  });
    const host = await attach();
    const record = { remoteHostId: connection.id, windowInstanceId: "remote-fixture-window", windowOwnerId: "primary",
      windowBindingToken: crypto.randomBytes(32).toString("hex"), window: { isDestroyed: () => false } };
    windows.set(record.windowInstanceId, record);
    const runtime = host.runtime();
    const health = await fetch(`${runtime.apiBaseUrl}/health`, { headers: { "x-minicode-token": runtime.runtimeToken } });
    assert.equal(health.status, 200);
    await host.native(record, "minicode:workspace:trust", [workspace]);
    const read = await host.native(record, "minicode:fs:readFile", [path.join(workspace, "owner.txt")]);
    assert.equal(read.content, "remote-owned-file\n");
    const localPreview = await host.mapUrl(`http://127.0.0.1:${preview.address().port}/large-preview`);
    const posted = await fetch(localPreview, { method: "POST", body: payload });
    assert.equal(posted.status, 200);
    assert.deepEqual(await posted.json(), { bytes: payload.length, sha256: crypto.createHash("sha256").update(payload).digest("hex") });
    const response = await new Promise((resolve, reject) => { const request = http.get(localPreview, resolve); request.once("error", reject); });
    response.pause();
    await new Promise(resolve => setTimeout(resolve, 100));
    const before = performance.now();
    const concurrentRead = await host.native(record, "minicode:fs:readFile", [path.join(workspace, "owner.txt")]);
    assert.equal(concurrentRead.content, read.content);
    assert.ok(performance.now() - before < 3000, "a stalled preview must not block the reverse binding stream");
    const received = [];
    response.on("data", chunk => received.push(chunk));
    const ended = once(response, "end");
    response.resume();
    await ended;
    assert.deepEqual(Buffer.concat(received), payload);
    const generation = host.ready.generation;
    await host.close();
    bridge.unregisterRemoteHost(connection.id, host);
    if (children[0].exitCode === null && children[0].signalCode === null) await once(children[0], "exit");
    const resumed = await attach();
    assert.equal(resumed.ready.generation, generation, "transport EOF must keep the original remote harness alive");
    assert.equal((await resumed.native(record, "minicode:fs:readFile", [path.join(workspace, "owner.txt")])).content, read.content);
    windows.delete(record.windowInstanceId);
    await assert.rejects(resumed.native(record, "minicode:window:release", []), /403/);
    await resumed.native(record, "minicode:window:release", [], { lifecycle: true });
    t.diagnostic(`same generation ${generation}; exact preview bytes ${payload.length}; actual CLI, HTTP, reverse bridge and lifecycle secret`);
});

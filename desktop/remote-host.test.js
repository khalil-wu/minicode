"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFile } = require("node:child_process");
const { once } = require("node:events");
const { promisify } = require("node:util");
const { PassThrough } = require("node:stream");
const { RemoteHost, RemoteHosts, transportCommand } = require("./remote-host");
const bridge = require("./embedded-browser-bridge");

function discoveryFixture(t, files) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-ssh-discovery-"));
  const homeDirectory = path.join(root, "home");
  const userData = path.join(root, "user-data");
  fs.mkdirSync(homeDirectory);
  fs.mkdirSync(userData);
  for (const [name, contents] of Object.entries(files)) {
    const file = path.join(homeDirectory, name);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, contents);
  }
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const hosts = new RemoteHosts(userData, { homeDirectory, platform: "linux",
    execFile: async () => { throw new Error("SSH discovery must not run a process"); } });
  return { hosts, homeDirectory, userData };
}

test("SSH discovery returns the first concrete Host alias, excludes colima, and sorts without resolving hosts", async t => {
  const { hosts } = discoveryFixture(t, { ".ssh/config": [
    "# Host commented-out",
    "Host *.wild !negated [ab] beta second-name # trailing comment",
    "  HostName beta.example",
    "hOsT=Alpha ignored-alternate",
    "Host beta duplicate",
    'Host "quoted alias" another-name',
    'Host "single alias"',
    "Host CoLiMa discarded-secondary",
    "Host github-alias",
    "  HostName github.com",
    "Host missing-hostname",
    "Host !excluded * wildcard? bracket[ab]",
  ].join("\r\n") });
  const expected = ["beta", "Alpha", "quoted alias", "single", "github-alias", "missing-hostname"].sort((left, right) => left.localeCompare(right));
  assert.deepEqual(await hosts.discover("ssh"), { sshAliases: expected, wslDistributions: [] });
});

test("SSH discovery expands root Include globs from the entry directory and visits each config once", async t => {
  const files = {
    ".ssh/config": [
      'Include conf.d/*.conf extra.conf ~/home.conf ~\\backslash.conf absent.conf no-match-*.conf',
      'Include "split config"',
      "Host zulu alternate",
      "  Include inside-host.conf",
      "Match all",
      "  Include inside-match.conf",
      "Host direct another-alias",
    ].join("\n"),
    ".ssh/conf.d/first.conf": "Include shared/*.conf config\nHost alpha alternate\n",
    ".ssh/conf.d/second.conf": "Include conf.d/first.conf\nHost beta alternate\nHost alpha\n",
    ".ssh/shared/shared.conf": "Include conf.d/first.conf\nHost gamma\n",
    ".ssh/extra.conf": "Host delta\n",
    ".ssh/split": "Host split-path\n",
    ".ssh/split config": "Host must-not-preserve-quoted-include-spaces\n",
    "home.conf": "Host home-alias\n",
    "backslash.conf": "Host backslash-alias\n",
    ".ssh/inside-host.conf": "Host must-not-expand-host-block-include\n",
    ".ssh/inside-match.conf": "Host must-not-expand-match-block-include\n",
  };
  const { hosts, homeDirectory } = discoveryFixture(t, files);
  fs.mkdirSync(path.join(homeDirectory, ".ssh/conf.d/directory.conf"));
  const reads = new Map();
  const readFileSync = fs.readFileSync;
  t.mock.method(fs, "readFileSync", (file, ...args) => {
    const name = path.relative(homeDirectory, file);
    reads.set(name, (reads.get(name) || 0) + 1);
    return readFileSync(file, ...args);
  });
  const expected = ["alpha", "beta", "gamma", "delta", "split-path", "home-alias", "backslash-alias", "zulu", "direct"].sort((left, right) => left.localeCompare(right));
  assert.deepEqual(await hosts.discover("ssh"), { sshAliases: expected, wslDistributions: [] });
  assert.equal(reads.size, 8);
  assert.ok([...reads.values()].every(count => count === 1));
  for (const [name, contents] of Object.entries(files)) assert.equal(readFileSync(path.join(homeDirectory, name), "utf8"), contents);
});

test("SSH discovery leaves a missing entry empty and reports malformed config rather than returning aliases", async t => {
  const { hosts, homeDirectory } = discoveryFixture(t, {});
  assert.deepEqual(await hosts.discover("ssh"), { sshAliases: [], wslDistributions: [] });
  fs.mkdirSync(path.join(homeDirectory, ".ssh"));
  const config = path.join(homeDirectory, ".ssh/config");
  for (const [contents, error] of [
    ['Host "unterminated\n', /Unexpected line break in SSH config/],
    ["Host\n", /Missing value in SSH config/],
    ['Include "unterminated\n', /Unexpected line break in SSH config/],
  ]) {
    fs.writeFileSync(config, contents);
    await assert.rejects(hosts.discover("ssh"), error);
  }
  fs.writeFileSync(config, "Include\n");
  assert.deepEqual(await hosts.discover("ssh"), { sshAliases: [], wslDistributions: [] });
});

test("SSH discovery keeps the existing WSL command, UTF-16 decoding, and error result", async t => {
  const { hosts } = discoveryFixture(t, { ".ssh/config": "Host owned-alias\n" });
  const calls = [];
  hosts.dependencies.platform = "win32";
  hosts.dependencies.execFile = async (program, args, options) => {
    calls.push({ program, args, options });
    return { stdout: Buffer.from(" Ubuntu\0\r\nDebian\r\n\r\n", "utf16le") };
  };
  assert.deepEqual(await hosts.discover("wsl"), { sshAliases: [], wslDistributions: ["Ubuntu", "Debian"] });
  assert.deepEqual(calls, [{ program: "wsl.exe", args: ["--list", "--quiet"], options: { windowsHide: true, encoding: "buffer" } }]);
  const failure = new Error("actual WSL discovery failure");
  hosts.dependencies.execFile = async () => { throw failure; };
  await assert.rejects(hosts.discover("wsl"), error => error === failure);
  assert.deepEqual(await hosts.discover("ssh"), { sshAliases: ["owned-alias"], wslDistributions: [] });
});

test("discovered SSH aliases persist as aliases without freezing resolved overrides or changing manual and WSL entries", async t => {
  const config = "Host owned-alias\n  HostName example.test\n";
  const { hosts, homeDirectory, userData } = discoveryFixture(t, { ".ssh/config": config });
  const entry = hosts.save({ id: "owned-alias-record", kind: "ssh", displayName: " My alias ", source: "discovered",
    alias: " owned-alias ", sshHost: "resolved.example", sshPort: 2222, identity: "C:/unread-key" });
  assert.deepEqual(entry, { id: "owned-alias-record", kind: "ssh", displayName: "My alias", source: "discovered",
    alias: "owned-alias", sshHost: "owned-alias", sshPort: null, identity: null });
  const manual = hosts.save({ id: "manual-record", kind: "ssh", displayName: "Manual", sshHost: " alice@example.test ", sshPort: 2222, identity: "C:/unread-key" });
  assert.deepEqual(manual, { id: "manual-record", kind: "ssh", displayName: "Manual", sshHost: "alice@example.test", sshPort: 2222, identity: "C:/unread-key" });
  const wsl = hosts.save({ id: "wsl-record", kind: "wsl", displayName: "Linux", distribution: " Ubuntu " });
  assert.deepEqual(wsl, { id: "wsl-record", kind: "wsl", displayName: "Linux", distribution: "Ubuntu" });
  const restored = new RemoteHosts(userData, { homeDirectory, execFile: async () => ({ stdout: "hostname example.test\nport 2202\nidentityfile none\n" }) });
  assert.deepEqual(restored.saved.connections, [entry, manual, wsl]);
  await restored.refresh();
  assert.deepEqual(restored.list(), [{ ...entry, sshHost: "example.test", sshPort: 2202 }, manual, wsl].map(connection => ({ ...connection, status: "disconnected" })));
  assert.equal(fs.readFileSync(path.join(homeDirectory, ".ssh/config"), "utf8"), config);
});

test("SSH metadata refresh derives Source hostname, first valid port and first non-none identity while transport keeps the alias", async t => {
  const { hosts, homeDirectory, userData } = discoveryFixture(t, { ".ssh/config": "Host owned-alias\n" });
  const raw = hosts.save({ id: "metadata-alias", displayName: "Owned", kind: "ssh", source: "discovered", alias: "owned-alias" });
  const calls = [];
  hosts.dependencies.execFile = async (program, args, options) => {
    calls.push({ program, args, options });
    return { stdout: ['hostname "actual.example"', "hostname ignored.example", "port invalid", "port 2202", "port 2203",
      "identityfile none", 'identityfile "C:\\keys\\actual key"', "identityfile ignored-key"].join("\n") };
  };
  const [resolved] = await hosts.refresh();
  assert.deepEqual(resolved, { ...raw, sshHost: "actual.example", sshPort: 2202, identity: "C:\\keys\\actual key", status: "disconnected" });
  assert.deepEqual(calls, [{ program: "ssh", args: ["-G", "-F", path.join(homeDirectory, ".ssh/config"), "owned-alias"], options: { windowsHide: true, encoding: "utf8" } }]);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userData, "remote-hosts.json"), "utf8")).connections, [raw]);
  const [program, args] = transportCommand(resolved, "desktop", "marker");
  assert.equal(program, "ssh");
  assert.equal(args.includes("-p"), false);
  assert.equal(args.includes("-i"), false);
  assert.equal(args.at(-2), "owned-alias");
  assert.equal(args.includes("actual.example"), false);
  hosts.hosts.set(raw.id, { status: "connected", ready: { version: "actual-app-server-version" } });
  assert.equal(hosts.list()[0].appServerVersion, "actual-app-server-version");
  hosts.hosts.get(raw.id).status = "disconnected";
  assert.equal("appServerVersion" in hosts.list()[0], false);
});

test("SSH metadata refresh filters Source unavailable aliases, logs actual errors and leaves saved entries intact", async t => {
  const { hosts } = discoveryFixture(t, { ".ssh/config": "# owned metadata fixture\n" });
  const logs = [];
  hosts.log = message => logs.push(message);
  hosts.saveMany(["working", "github", "missing-hostname", "failed"].map(alias => ({ id: alias, kind: "ssh", source: "discovered", alias, displayName: alias }))
    .concat([{ id: "manual", kind: "ssh", displayName: "Manual", sshHost: "manual.example" }, { id: "wsl", kind: "wsl", displayName: "Linux", distribution: "Owned" }]));
  hosts.dependencies.execFile = async (_program, args) => {
    const alias = args.at(-1);
    if (alias === "failed") throw Object.assign(new Error("Actual ssh -G process failed"), { code: 255, stderr: "actual configuration line 7 error" });
    return { stdout: alias === "github" ? "hostname GitHub.Com\n" : alias === "missing-hostname" ? "port 22\n" : "hostname actual.example\n" };
  };
  assert.deepEqual((await hosts.refresh()).map(connection => connection.id), ["working", "manual", "wsl"]);
  assert.equal(hosts.saved.connections.length, 6);
  assert.ok(logs.some(message => message.includes("ssh_alias_excluded") && message.includes("GitHub.Com")));
  assert.ok(logs.some(message => message.includes("ssh_alias_missing_hostname") && message.includes("missing-hostname")));
  assert.ok(logs.some(message => message.includes("255") && message.includes("actual configuration line 7 error")));
  const unavailable = Object.assign(new Error("spawn ssh ENOENT"), { code: "ENOENT" });
  hosts.dependencies.execFile = async () => { throw unavailable; };
  await assert.rejects(hosts.refresh(), error => error === unavailable);
  assert.ok(logs.some(message => message.includes("spawn ssh ENOENT")));
});

test("SSH metadata refresh uses the actual local ssh -G against an owned config without connecting", async t => {
  const { hosts, homeDirectory } = discoveryFixture(t, {});
  fs.mkdirSync(path.join(homeDirectory, ".ssh"));
  const configPath = path.join(homeDirectory, ".ssh/config");
  const identity = path.join(homeDirectory, "unread identity").replace(/\\/g, "/");
  const config = `Host actual-alias\n  HostName 192.0.2.17\n  Port 2227\n  IdentityFile "${identity}"\n`;
  fs.writeFileSync(configPath, config);
  hosts.dependencies.execFile = promisify(execFile);
  hosts.save({ id: "actual-metadata", kind: "ssh", displayName: "Actual metadata", source: "discovered", alias: "actual-alias" });
  const [resolved] = await hosts.refresh();
  assert.equal(resolved.sshHost, "192.0.2.17");
  assert.equal(resolved.sshPort, 2227);
  assert.equal(resolved.identity, identity);
  assert.equal(fs.existsSync(identity), false);
  assert.equal(fs.readFileSync(configPath, "utf8"), config);
});

test("SSH metadata refresh generation keeps a late old resolution from replacing a later save", async t => {
  const { hosts } = discoveryFixture(t, {});
  const resolves = [];
  hosts.dependencies.execFile = () => new Promise(resolve => resolves.push(resolve));
  hosts.save({ id: "generation", kind: "ssh", displayName: "Old", source: "discovered", alias: "old-alias" });
  const old = hosts.refresh();
  hosts.save({ id: "generation", kind: "ssh", displayName: "New", source: "discovered", alias: "new-alias" });
  const current = hosts.refresh();
  resolves[1]({ stdout: "hostname new.example\nport 2202\n" });
  const latest = await current;
  resolves[0]({ stdout: "hostname old.example\nport 2201\n" });
  assert.deepEqual(await old, latest);
  assert.equal(hosts.list()[0].alias, "new-alias");
  assert.equal(hosts.list()[0].sshHost, "new.example");
  assert.equal(hosts.saved.connections[0].sshHost, "new-alias");
});

test("connection batch validates all entries before one persist and accepts a live display-name swap", t => {
  const { hosts, userData } = discoveryFixture(t, {});
  hosts.saveMany([{ id: "first", kind: "ssh", displayName: "First", sshHost: "first.example" }, { id: "second", kind: "ssh", displayName: "Second", sshHost: "second.example" }]);
  const file = path.join(userData, "remote-hosts.json"), before = fs.readFileSync(file, "utf8");
  const persist = hosts.persist.bind(hosts);
  let writes = 0;
  hosts.persist = saved => { writes++; persist(saved); };
  for (const invalid of [
    [{ id: "new", kind: "ssh", displayName: "New", sshHost: "new.example" }, { id: "bad", kind: "ssh", displayName: "Second", sshHost: "bad.example" }],
    [{ id: "same", kind: "ssh", displayName: "One", sshHost: "one" }, { id: "same", kind: "ssh", displayName: "Two", sshHost: "two" }],
    [{ id: "new", kind: "ssh", displayName: "New", sshHost: "new.example" }, { id: "bad", kind: "ssh", displayName: "Bad", sshHost: "bad", sshPort: 70000 }],
  ]) {
    assert.throws(() => hosts.saveMany(invalid));
    assert.equal(writes, 0);
    assert.equal(fs.readFileSync(file, "utf8"), before);
    assert.deepEqual(hosts.saved.connections.map(connection => connection.id), ["first", "second"]);
  }
  hosts.hosts.set("first", { status: "connected", ready: { version: "actual" } });
  hosts.saveMany([{ id: "first", kind: "ssh", displayName: "Second", sshHost: "first.example" }, { id: "second", kind: "ssh", displayName: "First", sshHost: "second.example" }]);
  assert.equal(writes, 1);
  assert.deepEqual(hosts.saved.connections.map(connection => connection.displayName), ["Second", "First"]);
});

test("ready platform maps Windows wire spelling without changing Linux or Darwin", () => {
  for (const [wire, platform] of [["windows", "win32"], ["linux", "linux"], ["darwin", "darwin"]]) {
    const host = new RemoteHost({ id: "runtime", kind: "ssh", displayName: "Actual runtime", sshHost: "host" }, "desktop");
    host.ready = { platform: wire, home: "actual-home", generation: "actual-generation", runtime_token: "actual-token", version: "0.2.0" };
    host.backendPort = 1234;
    assert.equal(host.runtime().remoteHost.platform, platform);
    assert.equal(host.ready.platform, wire);
  }
});

async function ownedEofTransport(t, exitCode, remainOpen) {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-proxy-eof-"));
  const receipt = path.join(state, "stdin-eof.json");
  let child;
  const host = new RemoteHost({ id: "owned-eof", kind: "wsl", distribution: "owned", displayName: "Owned EOF" }, "a".repeat(32), {
    spawnTransport: (_program, args, options) => {
      const ready = { type: "ready", protocol: 1, stream_window_bytes: 262144, stream_chunk_bytes: 65536 };
      const code = `const fs=require('node:fs');const chunks=[];const payload=Buffer.from(${JSON.stringify(JSON.stringify(ready))});const header=Buffer.alloc(4);header.writeUInt32BE(payload.length);process.stdout.write(Buffer.concat([Buffer.from(${JSON.stringify(args.at(-1))},'hex'),header,payload]));process.stdin.on('data',data=>chunks.push(data));process.stdin.once('end',()=>{fs.writeFileSync(${JSON.stringify(receipt)},JSON.stringify({ended:true,bytes:Buffer.concat(chunks).length}));${exitCode ? `process.stderr.write('owned proxy exit ${exitCode}\\n');` : ""}process.exitCode=${exitCode};});${remainOpen ? "setInterval(()=>{},1000);" : ""}`;
      child = spawn(process.execPath, ["-e", code], options);
      return child;
    },
  });
  t.after(async () => {
    if (!host.closed) await host.close(new Error("owned test cleanup"));
    fs.rmSync(state, { recursive: true, force: true });
  });
  await host.connect();
  return { host, child, receipt };
}

for (const exitCode of [0, 7]) {
  test(`connected normal disconnect delivers stdin EOF and observes proxy exit ${exitCode}`, { timeout: 10_000 }, async t => {
    const { host, child, receipt } = await ownedEofTransport(t, exitCode, false);
    const notifications = [];
    host.on("status", (status, error) => notifications.push({ status, error }));
    const frame = { type: "ping", id: "last complete physical frame" };
    await host.send(frame);
    if (exitCode === 0) await host.close();
    else await assert.rejects(host.close(), /Remote MiniCode transport exited \(7\): owned proxy exit 7/);
    assert.deepEqual(JSON.parse(fs.readFileSync(receipt, "utf8")), { ended: true, bytes: 4 + Buffer.byteLength(JSON.stringify(frame)) });
    assert.equal(child.exitCode, exitCode);
    assert.equal(child.signalCode, null);
    assert.equal(host.status, "disconnected");
    assert.equal(notifications.length, 1);
    if (exitCode === 0) assert.equal(notifications[0].error, undefined);
    else assert.match(notifications[0].error.message, /owned proxy exit 7/);
  });
}

test("connected transport failure still terminates its owned proxy and preserves the actual error", { timeout: 10_000 }, async t => {
  const { host, child } = await ownedEofTransport(t, 0, true);
  const failure = new Error("actual attachment I/O failure");
  const notifications = [];
  host.on("status", (status, error) => notifications.push({ status, error }));
  await host.close(failure);
  assert.equal(child.signalCode, "SIGTERM");
  assert.equal(host.status, "disconnected");
  assert.deepEqual(notifications, [{ status: "disconnected", error: failure }]);
});

async function queuedRemoteSocketFixture(t) {
  const physical = new PassThrough({ highWaterMark: 1 });
  const host = new RemoteHost({ id: "ordered-close", kind: "wsl", distribution: "owned" }, "owned");
  host.ready = { stream_window_bytes: 262144, stream_chunk_bytes: 65536 };
  host.process = { stdin: physical };
  const errors = [], peers = [];
  host.on("streamError", value => errors.push(value));
  const server = net.createServer({ allowHalfOpen: true });
  server.on("connection", socket => { peers.push(socket); socket.on("error", error => errors.push({ error, side: "consumer" })); });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => {
    physical.resume();
    await host.close();
    for (const socket of peers) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  async function attach(id) {
    const accepted = once(server, "connection");
    const socket = net.createConnection({ host: "127.0.0.1", port: server.address().port, allowHalfOpen: true });
    host.track(id, socket);
    const closed = once(socket, "close");
    await once(socket, "connect");
    const [peer] = await accepted;
    const chunks = [];
    const firstData = once(peer, "data");
    peer.on("data", data => chunks.push(data));
    const ended = new Promise(resolve => peer.on("end", () => { resolve(Buffer.concat(chunks)); peer.end(); }));
    return { socket, closed, firstData, ended };
  }
  return { host, physical, errors, attach };
}

test("remote graceful close drains queued bytes and EOF without blocking another virtual stream", { timeout: 10_000 }, async t => {
  const { host, physical, errors, attach } = await queuedRemoteSocketFixture(t);
  const stream = await attach(2);
  const first = Buffer.from("first queued TCP bytes\n"), final = Buffer.from("final 中文 😀 bytes\n");
  await host.receive({ type: "data", id: 2, data: first.toString("base64") });
  await stream.firstData;
  assert.ok(physical.writableNeedDrain, "the actual physical writer is backpressured");
  await host.receive({ type: "data", id: 2, data: final.toString("base64") });
  await host.receive({ type: "end", id: 2 });
  await host.receive({ type: "close", id: 2 });
  assert.equal(stream.socket.destroyed, false, "Close must leave the accepted write queue and EOF owned until they drain");

  const other = await attach(4);
  const body = Buffer.from("independent TCP stream");
  await host.receive({ type: "data", id: 4, data: body.toString("base64") });
  assert.deepEqual((await other.firstData)[0], body);
  assert.equal(stream.socket.destroyed, false);
  physical.resume();
  assert.deepEqual(await stream.ended, Buffer.concat([first, final]));
  await stream.closed;
  assert.equal(host.streams.has(2), false);
  await host.receive({ type: "end", id: 4 });
  await host.receive({ type: "close", id: 4 });
  assert.deepEqual(await other.ended, body);
  await other.closed;
  assert.deepEqual(errors, []);
  assert.equal(host.closed, false);
});

for (const operation of ["close-without-end", "error-after-end", "detach"]) {
  test(`remote ${operation} interrupts pending writes instead of waiting for graceful drain`, { timeout: 10_000 }, async t => {
    const { host, physical, errors, attach } = await queuedRemoteSocketFixture(t);
    const stream = await attach(2);
    await host.receive({ type: "data", id: 2, data: Buffer.from("accepted TCP bytes").toString("base64") });
    await stream.firstData;
    assert.ok(physical.writableNeedDrain);
    await host.receive({ type: "data", id: 2, data: Buffer.from("queued bytes").toString("base64") });
    if (operation === "close-without-end") await host.receive({ type: "close", id: 2 });
    else if (operation === "error-after-end") {
      await host.receive({ type: "end", id: 2 });
      await host.receive({ type: "error", id: 2, error: "actual virtual stream error" });
    } else await host.close();
    assert.equal(stream.socket.destroyed, true);
    physical.resume();
    await stream.closed.catch(error => { assert.equal(operation, "error-after-end"); assert.match(error.message, /actual virtual stream error/); });
    if (operation === "error-after-end") assert.ok(errors.some(value => value.error.message === "actual virtual stream error"));
    assert.equal(host.closed, operation === "detach");
  });
}

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
    assert.equal(host.ready.platform, process.platform === "win32" ? "windows" : process.platform);
    assert.equal(runtime.remoteHost.platform, process.platform);
    assert.equal(host.ready.version, require("./package.json").version);
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
    const taskConversation = "owned-surviving-task";
    const terminal = await host.native(record, "minicode:pty:spawn", [workspace, taskConversation]);
    const taskFile = path.join(workspace, "task-survived-detach.txt"), releaseFile = path.join(workspace, "release-owned-task");
    const taskCode = `import pathlib,time;p=pathlib.Path(${JSON.stringify(taskFile)});r=pathlib.Path(${JSON.stringify(releaseFile)});p.write_text('started');exec('while not r.exists(): time.sleep(.02)');p.write_text('finished');print('TASK_AFTER_DETACH',flush=True)`;
    const shellQuote = value => "'" + value.replace(/'/g, process.platform === "win32" ? "''" : "'\\''") + "'";
    const command = `${process.platform === "win32" ? "& " : ""}${shellQuote(python)} -c ${shellQuote(taskCode)}${process.platform === "win32" ? "\r" : "\n"}`;
    await host.native(record, "minicode:pty:write", [terminal.session_id, command, taskConversation]);
    const waitTask = async expected => {
      const deadline = Date.now() + 12_000;
      while ((!fs.existsSync(taskFile) || fs.readFileSync(taskFile, "utf8") !== expected) && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      assert.equal(fs.readFileSync(taskFile, "utf8"), expected);
    };
    await waitTask("started");
    const generation = host.ready.generation;
    const daemonLog = path.join(state, "desktop-hosts", desktopId, "host.log");
    const beforeDisconnectLogBytes = fs.statSync(daemonLog).size;
    await host.close();
    bridge.unregisterRemoteHost(connection.id, host);
    if (children[0].exitCode === null && children[0].signalCode === null) await once(children[0], "exit");
    assert.equal(children[0].exitCode, 0, "the production proxy must finish its own EOF detach instead of being killed");
    assert.equal(children[0].signalCode, null);
    const disconnectLog = fs.readFileSync(daemonLog).subarray(beforeDisconnectLogBytes).toString("utf8");
    assert.doesNotMatch(disconnectLog, /Unhandled exception in client_connected_cb|Exception in callback|WinError (?:64|10054|121)/);
    fs.writeFileSync(releaseFile, "complete while proxy is detached");
    await waitTask("finished");
    const resumed = await attach();
    assert.equal(resumed.ready.generation, generation, "transport EOF must keep the original remote harness alive");
    assert.equal((await resumed.native(record, "minicode:fs:readFile", [path.join(workspace, "owner.txt")])).content, read.content);
    const retained = await resumed.native(record, "minicode:pty:snapshot", [terminal.session_id, 200000, taskConversation]);
    assert.equal(retained.pid, terminal.pid);
    assert.equal(retained.is_alive, true);
    assert.match(retained.output, /TASK_AFTER_DETACH/);
    windows.delete(record.windowInstanceId);
    await assert.rejects(resumed.native(record, "minicode:window:release", []), /403/);
    await resumed.native(record, "minicode:window:release", [], { lifecycle: true });
    t.diagnostic(`same generation ${generation}; actual task completed while proxy detached; same live PTY pid ${terminal.pid}; exact preview bytes ${payload.length}; actual CLI, HTTP, reverse bridge and lifecycle secret`);
});

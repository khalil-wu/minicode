"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { spawn } = require("node:child_process");
const { once, EventEmitter } = require("node:events");
const { RemoteDesktop } = require("./remote-desktop");
const { RemoteHost } = require("./remote-host");

test("closing an offline window persists cleanup and the same daemon can receive it under a new connection", async (t) => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-remote-release-"));
  const calls = [];
  const backend = http.createServer(async (request, response) => {
    const parts = [];
    for await (const part of request) parts.push(part);
    const body = JSON.parse(Buffer.concat(parts));
    calls.push({ path: request.url, headers: request.headers, body });
    assert.equal(request.headers["x-minicode-token"], "actual-runtime");
    assert.equal(request.headers["x-minicode-desktop-lifecycle-token"], "private-lifecycle");
    response.setHeader("content-type", "application/json"); response.end('{"released":true}');
  });
  backend.listen(0, "127.0.0.1");
  await once(backend, "listening");
  t.after(async () => { await new Promise(resolve => backend.close(resolve)); });
  const deps = { userData: profile, bridgeEndpoint: "http://127.0.0.1:12345", windowManager: { getMainWindows: () => [] }, browserBridge: {}, log() {} };
  const first = new RemoteDesktop(deps);
  const record = { remoteHostId: "old-connection", windowOwnerId: "secondary-2", windowInstanceId: "original-closed-window",
    runtime: { remoteHost: { generation: "same-real-daemon" } } };
  assert.deepEqual(await first.releaseWindow(record), { pending: true });
  assert.equal(calls.length, 0);
  const cold = new RemoteDesktop(deps);
  assert.equal(cold.hosts.saved.releasedWindows[0].windowInstanceId, "original-closed-window");
  const host = new RemoteHost({ id: "new-connection" }, "owned");
  host.ready = { runtime_token: "actual-runtime", bridge_token: "private-lifecycle", generation: "same-real-daemon" };
  host.runtime = () => ({ apiBaseUrl: `http://127.0.0.1:${backend.address().port}`, runtimeToken: "actual-runtime" });
  await cold.flushReleasedWindows("new-connection", host);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(call => call.path).sort(), ["/api/desktop-host/native", "/api/workspace/language/lifecycle"]);
  for (const call of calls) assert.equal(call.body.window_instance_id, record.windowInstanceId);
  assert.equal(cold.hosts.saved.releasedWindows.length, 0);
  assert.equal(new RemoteDesktop(deps).hosts.saved.releasedWindows.length, 0);
});

test("connection configuration does not persist caller supplied live status or expose runtime secrets", async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-remote-config-"));
  const remote = new RemoteDesktop({ userData: profile, windowManager: { getMainWindows: () => [] } });
  await remote.save({ id: "actual-config", kind: "ssh", displayName: "My server", sshHost: "user@host", status: "connected", runtimeToken: "untrusted-token" });
  const listed = (await remote.list())[0];
  assert.equal(listed.status, "disconnected");
  assert.equal(listed.runtimeToken, undefined);
  const stored = JSON.parse(fs.readFileSync(path.join(profile, "remote-hosts.json"), "utf8"));
  assert.equal(stored.connections[0].status, undefined);
  assert.equal(stored.connections[0].runtimeToken, undefined);
});

function desktopFixture(t) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-remote-catalog-"));
  t.after(() => fs.rmSync(profile, { recursive: true, force: true }));
  const broadcasts = [], logs = [], registered = new Map();
  const record = { remoteHostId: "live", windowInstanceId: "same-window", windowOwnerId: "same-owner", windowBindingToken: "same-binding",
    runtime: { revision: 1, remoteHost: { generation: "old-generation" } } };
  const window = { webContents: { send: (...args) => broadcasts.push(args) } };
  let reloads = 0;
  const remote = new RemoteDesktop({ userData: profile, log: message => logs.push(message),
    windowManager: { getMainWindows: () => [window], getWindowRecord: () => record,
      reloadWindow: () => { reloads++; } },
    browserBridge: { registerRemoteHost: (id, host) => registered.set(id, host),
      unregisterRemoteHost: (id, host) => { if (registered.get(id) === host) registered.delete(id); } } });
  remote.hosts.dependencies.homeDirectory = profile;
  return { remote, broadcasts, logs, registered, record, profile, reloads: () => reloads };
}

test("desktop batch saves once, refreshes metadata once and broadcasts only the usable catalog", async t => {
  const { remote, broadcasts, profile } = desktopFixture(t);
  let probes = 0, writes = 0, changed = false;
  const persist = remote.hosts.persist.bind(remote.hosts);
  remote.hosts.persist = saved => { writes++; persist(saved); };
  remote.hosts.dependencies.execFile = async (_program, args) => {
    probes++;
    const alias = args.at(-1);
    return { stdout: `hostname ${alias === "github" ? "github.com" : changed ? "changed.example" : alias + ".example"}\nport 2202\nidentityfile none\n` };
  };
  remote.hosts.connect = async () => { throw new Error("Metadata refresh must not invent a live connection"); };
  const inputs = ["alpha", "beta", "github"].map(alias => ({ id: alias, kind: "ssh", displayName: alias, source: "discovered", alias }));
  inputs.push({ id: "manual", kind: "ssh", displayName: "Manual", sshHost: "manual.example" });
  const available = await remote.saveMany(inputs);
  assert.deepEqual(available.map(connection => connection.id), ["alpha", "beta", "manual"]);
  assert.equal(available[0].sshHost, "alpha.example");
  assert.equal(available[0].sshPort, 2202);
  assert.equal(writes, 1);
  assert.equal(probes, 3);
  assert.equal(broadcasts.length, 1);
  assert.deepEqual(broadcasts[0], ["minicode:connections:changed", available]);
  const file = path.join(profile, "remote-hosts.json"), before = fs.readFileSync(file, "utf8");
  const saved = JSON.parse(before).connections;
  assert.equal(saved[0].sshHost, "alpha");
  assert.equal(saved[0].sshPort, null);
  assert.equal(saved[0].identity, null);
  await assert.rejects(remote.saveMany([{ id: "new", kind: "ssh", displayName: "New", sshHost: "new" },
    { id: "invalid", kind: "ssh", displayName: "alpha", sshHost: "invalid" }]), /display name already exists/);
  assert.equal(writes, 1);
  assert.equal(probes, 3);
  assert.equal(broadcasts.length, 1);
  assert.equal(fs.readFileSync(file, "utf8"), before);
  assert.equal(await remote.save({ ...inputs[2], displayName: "Unavailable" }), null);
  changed = true;
  assert.equal((await remote.list()).find(connection => connection.id === "alpha").sshHost, "changed.example");
  assert.ok(Array.isArray(remote.hosts.list()));
});

function ownedHost(connection, generation) {
  const host = new RemoteHost(connection, "owned");
  Object.assign(host, { connection, closed: false, status: "connected", backendPort: 1, nativeCalls: [], closes: 0,
    ready: { generation, version: "actual-server-0.2.0", platform: "windows", home: "C:\\Owned", runtime_token: "runtime", bridge_token: "lifecycle" },
    runtime: RemoteHost.prototype.runtime });
  host.native = async (...args) => { host.nativeCalls.push(args); return { released: true }; };
  host.close = async () => { host.closes++; host.closed = true; host.status = "disconnected"; host.emit("status", "disconnected"); };
  host.connecting = Promise.resolve(host);
  return host;
}

test("desktop live edits preserve a renamed owner, replace a changed transport and never release an old generation on its new target", async t => {
  const { remote, registered, record, reloads } = desktopFixture(t);
  const input = { id: "live", kind: "ssh", displayName: "Original", sshHost: "old.example" };
  await remote.save(input);
  const created = [];
  remote.hosts.connect = async id => {
    const existing = remote.hosts.hosts.get(id);
    if (existing && !existing.closed) return existing;
    const connection = remote.hosts.catalog.find(item => item.id === id);
    const host = ownedHost(connection, connection.sshHost === "old.example" ? "old-generation" : "new-generation");
    created.push(host); remote.hosts.hosts.set(id, host); return host;
  };
  const original = await remote.connect("live");
  const previousReloads = reloads();
  const taskOwner = { generation: original.ready.generation, windowOwnerId: record.windowOwnerId };
  const renamed = await remote.save({ ...input, displayName: "Renamed" });
  assert.equal(renamed.appServerVersion, "actual-server-0.2.0");
  assert.equal(original.connection.displayName, "Renamed");
  assert.equal(original.closes, 0);
  assert.equal(registered.get("live"), original);
  assert.equal(reloads(), previousReloads);
  remote.hosts.saved.releasedWindows = [{ remoteHostId: "live", remoteGeneration: "old-generation",
    windowInstanceId: "closed-old-window", windowOwnerId: "old-owner" }];
  remote.hosts.persist();
  await remote.save({ ...input, displayName: "Renamed", sshHost: "new.example" });
  const replacement = created[1];
  assert.equal(original.closes, 1);
  assert.equal(original.nativeCalls.length, 0, "detaching the old transport must not send remote stop/release operations");
  assert.equal(replacement.nativeCalls.length, 0, "a new target must not receive the old generation's release");
  assert.equal(registered.get("live"), replacement);
  assert.equal(reloads(), previousReloads + 1, "replacement uses windowManager.reloadWindow and its unload protection");
  assert.equal(record.windowOwnerId, taskOwner.windowOwnerId);
  assert.equal(record.windowBindingToken, "same-binding");
  assert.equal(record.runtime.remoteHost.generation, "new-generation");
  assert.equal(record.runtime.remoteHost.platform, "win32");
  assert.equal(taskOwner.generation, "old-generation");
  assert.equal(remote.hosts.saved.releasedWindows[0].remoteGeneration, "old-generation");
  remote.hosts.dependencies.execFile = async () => ({ stdout: "hostname github.com\n" });
  assert.equal(await remote.save({ ...input, source: "discovered", alias: "github" }), null);
  assert.equal(replacement.closes, 1);
  assert.equal(created.length, 2, "a filtered target must not acquire a fabricated connection");
  assert.equal(registered.has("live"), false);
  assert.equal(remote.hosts.saved.releasedWindows.length, 1);
});

async function releaseHttpFixture(t) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-release-disconnect-"));
  const connection = { id: "release-owner", kind: "ssh", displayName: "Release owner", sshHost: "owned.example" };
  const remote = new RemoteDesktop({ userData: profile, log() {}, windowManager: { getMainWindows: () => [] },
    browserBridge: { registerRemoteHost() {}, unregisterRemoteHost() {} } });
  remote.hosts.save(connection);
  await remote.hosts.refresh();
  const host = new RemoteHost(connection, remote.hosts.saved.desktopId);
  const nativeStarts = [], nativeRequest = host.native.bind(host);
  host.native = (...args) => { nativeStarts.push(args); return nativeRequest(...args); };
  host.ready = { generation: "current-daemon", runtime_token: "actual-runtime", bridge_token: "actual-lifecycle", platform: "windows" };
  host.status = "connected";
  remote.hosts.hosts.set(connection.id, host);
  const oldRelease = { remoteHostId: connection.id, remoteGeneration: "offline-old-daemon",
    windowInstanceId: "old-generation-window", windowOwnerId: "old-owner" };
  remote.hosts.saved.releasedWindows = [oldRelease];
  remote.hosts.persist();
  const arrivals = new EventEmitter(), requests = [];
  const backend = http.createServer((request, response) => {
    const parts = [];
    request.on("data", part => parts.push(part));
    request.once("end", () => {
      const body = JSON.parse(Buffer.concat(parts));
      const call = { path: request.url, body, response, headers: request.headers };
      requests.push(call);
      arrivals.emit("request", call);
    });
  });
  let streamId = 0;
  // These are real HTTP sockets owned by the production closeTransport path.
  backend.on("connection", socket => { host.streams.set(++streamId, { socket }); });
  backend.listen(0, "127.0.0.1");
  await once(backend, "listening");
  host.backendPort = backend.address().port;
  host.servers.set("owned-http", backend);
  t.after(async () => { backend.closeAllConnections(); await host.close(); });
  const record = id => ({ remoteHostId: connection.id, windowInstanceId: id, windowOwnerId: `owner-${id}`,
    runtime: { remoteHost: { generation: host.ready.generation } } });
  const requestFor = async (id, endpoint, after) => {
    let call = requests.slice(after).find(item => item.body.window_instance_id === id && item.path === endpoint);
    while (!call) {
      await once(arrivals, "request");
      call = requests.slice(after).find(item => item.body.window_instance_id === id && item.path === endpoint);
    }
    return call;
  };
  const releasesFor = (id, after = 0) => Promise.all([
    requestFor(id, "/api/desktop-host/native", after), requestFor(id, "/api/workspace/language/lifecycle", after),
  ]);
  const respond = (call, status = 200) => {
    call.response.writeHead(status, { "content-type": "application/json" });
    call.response.end(status === 200 ? '{"released":true}' : '{"detail":"actual release rejected"}');
  };
  const outcome = promise => promise.then(value => ({ value }), error => ({ error }));
  return { remote, host, oldRelease, nativeStarts, requests, record, requestFor, releasesFor, respond, outcome, profile };
}

for (const action of ["disconnect", "refresh", "close"]) {
  test(`${action} waits for both actual window release acknowledgements before closing the transport`, { timeout: 10_000 }, async t => {
    const { remote, host, oldRelease, record, releasesFor, respond, outcome, profile } = await releaseHttpFixture(t);
    const release = outcome(remote.releaseWindow(record("closed-current-window")));
    const [native, editor] = await releasesFor("closed-current-window");
    if (action === "refresh") {
      remote.hosts.save({ ...host.connection, sshHost: "replacement.example" });
      remote.hosts.connect = async id => {
        const replacement = ownedHost(remote.hosts.catalog.find(item => item.id === id), "replacement-daemon");
        remote.hosts.hosts.set(id, replacement);
        return replacement;
      };
    }
    const detached = outcome(action === "disconnect" ? remote.disconnect(host.connection.id) : remote[action]());
    await new Promise(setImmediate);
    assert.equal(host.closed, false, "detach overtook the two in-flight cleanup HTTP requests");
    respond(native);
    await new Promise(setImmediate);
    assert.equal(host.closed, false, "the editor release acknowledgement is still outstanding");
    respond(editor);
    assert.deepEqual(await release, { value: { pending: false } });
    assert.equal((await detached).error, undefined);
    assert.equal(host.closed, true);
    assert.deepEqual(remote.hosts.saved.releasedWindows, [oldRelease]);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(profile, "remote-hosts.json"), "utf8")).releasedWindows, [oldRelease]);
    assert.equal(native.headers["x-minicode-desktop-lifecycle-token"], "actual-lifecycle");
    assert.equal(editor.headers["x-minicode-desktop-lifecycle-token"], "actual-lifecycle");
  });
}

test("concurrent closed windows share one release queue without duplicating the first owner", { timeout: 10_000 }, async t => {
  const { remote, host, oldRelease, nativeStarts, requests, record, releasesFor, respond, outcome } = await releaseHttpFixture(t);
  const firstRecord = record("first-window");
  const first = outcome(remote.releaseWindow(firstRecord));
  const firstCalls = await releasesFor(firstRecord.windowInstanceId);
  const repeated = outcome(remote.releaseWindow(firstRecord));
  const second = outcome(remote.releaseWindow(record("second-window")));
  assert.equal(nativeStarts.length, 1, "the first window has exactly one native release while its acknowledgements are pending");
  firstCalls.forEach(call => respond(call));
  const secondCalls = await releasesFor("second-window");
  const detached = outcome(remote.disconnect(host.connection.id));
  assert.equal(host.closed, false);
  secondCalls.forEach(call => respond(call));
  for (const released of await Promise.all([first, repeated, second])) assert.deepEqual(released, { value: { pending: false } });
  assert.equal((await detached).error, undefined);
  assert.equal(requests.length, 4);
  assert.deepEqual(remote.hosts.saved.releasedWindows, [oldRelease]);
});

test("detach also waits for a new closed window queued when the previous release owner completes", { timeout: 10_000 }, async t => {
  const { remote, host, oldRelease, requests, record, releasesFor, respond, outcome } = await releaseHttpFixture(t);
  const first = outcome(remote.releaseWindow(record("first-owner")));
  const firstCalls = await releasesFor("first-owner");
  let second;
  const queued = remote.flushReleasedWindows(host.connection.id, host).then(() => {
    second = outcome(remote.releaseWindow(record("next-owner")));
  });
  const detached = outcome(remote.disconnect(host.connection.id));
  firstCalls.forEach(call => respond(call));
  await queued;
  assert.equal(host.closed, false, "detach must observe the new owner before closing its transport");
  const secondCalls = await releasesFor("next-owner");
  secondCalls.forEach(call => respond(call));
  assert.deepEqual(await first, { value: { pending: false } });
  assert.deepEqual(await second, { value: { pending: false } });
  assert.equal((await detached).error, undefined);
  assert.equal(requests.length, 4);
  assert.deepEqual(remote.hosts.saved.releasedWindows, [oldRelease]);
});

test("a failed release waits for the other actual request, retains its owner and rejects detach without retrying", { timeout: 10_000 }, async t => {
  const { remote, host, oldRelease, requests, record, releasesFor, respond, outcome } = await releaseHttpFixture(t);
  let nativeSettled;
  const settled = new Promise(resolve => { nativeSettled = resolve; });
  const nativeRequest = host.native.bind(host);
  host.native = (...args) => {
    const request = nativeRequest(...args);
    void request.then(nativeSettled, nativeSettled);
    return request;
  };
  let releaseSettled = false;
  const release = outcome(remote.releaseWindow(record("rejected-window"))).then(result => { releaseSettled = true; return result; });
  const [native, editor] = await releasesFor("rejected-window");
  respond(native, 503);
  await settled;
  await new Promise(setImmediate);
  assert.equal(releaseSettled, false, "the editor request still belongs to this cleanup operation");
  const detached = outcome(remote.disconnect(host.connection.id));
  assert.equal(host.closed, false);
  respond(editor);
  const failure = (await release).error;
  assert.match(failure.message, /HTTP 503.*actual release rejected/);
  assert.equal((await detached).error, failure);
  assert.equal(host.closed, false, "failed cleanup cannot be reported as a successful disconnect");
  assert.equal(requests.length, 2, "disconnect does not retry failed release requests");
  assert.deepEqual(remote.hosts.saved.releasedWindows.map(item => item.windowInstanceId), [oldRelease.windowInstanceId, "rejected-window"]);
});

for (const transport of ["connected", "closed"]) {
  test(`actual main explicit second cleanup handles the failed remote owner when transport is ${transport}`, { timeout: 10_000 }, async t => {
    const { remote, host, oldRelease, nativeStarts, requests, record, releasesFor, respond, outcome } = await releaseHttpFixture(t);
    const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
    const releaseCode = source.slice(source.indexOf("const windowCleanupTasks ="), source.indexOf("async function launchManagedBackend"));
    const drainCode = source.slice(source.indexOf("async function drainDesktopResources()"), source.indexOf("async function prepareAutomaticRollback()"));
    const events = [], logs = [];
    const context = { module: { exports: {} }, Promise, remoteDesktop: remote, EMBEDDED_BROWSER_TOKEN: "local-lifecycle",
      dictationHotkeys: { async dispose() { events.push("dictation-stopped"); } },
      generalMaintenance: null, petManager: null, petInitialization: null,
      appendDesktopLog: message => logs.push(message),
      windowManager: { clearWindowStateSaveTimer() {}, persistWindowState() {} },
      embeddedBrowserBridge: { releaseWindow() {}, async stop() { events.push("bridge-stopped"); } },
      embeddedBrowserManager: { releaseWindow() {}, disposeAll() {} },
      ptyManager: { async killAllSessions() { events.push("local-terminals-stopped"); } },
      backendSidecar: { async stopBackendSidecar() { events.push("backend-stopped"); }, getBackendProcess: () => null },
    };
    vm.runInNewContext(`${releaseCode}\n${drainCode}\nmodule.exports={releaseWindowResources,drainDesktopResources,windowCleanupTasks};`, context);
    const lifecycle = context.module.exports, window = record("failed-main-window");
    const first = outcome(lifecycle.releaseWindowResources(window));
    const firstCalls = await releasesFor(window.windowInstanceId);
    respond(firstCalls[0], 503);
    respond(firstCalls[1]);
    const failure = (await first).error;
    assert.match(failure.message, /HTTP 503.*actual release rejected/);
    await assert.rejects(lifecycle.drainDesktopResources(), error => error === failure);
    assert.equal(requests.length, 2, "the first failed shutdown does not retry");
    assert.equal(events.includes("backend-stopped"), false);
    assert.equal(lifecycle.windowCleanupTasks.size, 1);
    if (transport === "closed") await host.close();
    const draining = outcome(lifecycle.drainDesktopResources());
    await new Promise(setImmediate);
    if (transport === "connected") {
      assert.equal(nativeStarts.length, 2, "only the existing explicit retry starts a new release owner");
      const retried = outcome(remote.releaseWindow(window, true));
      const retryCalls = await releasesFor(window.windowInstanceId, 2);
      assert.equal(nativeStarts.length, 2, "an explicit retry joins the active retry owner");
      assert.equal(host.closed, false);
      retryCalls.forEach(call => respond(call));
      assert.deepEqual(await retried, { value: { pending: false } });
    }
    assert.equal((await draining).error, undefined);
    assert.equal(lifecycle.windowCleanupTasks.size, 0);
    assert.equal(host.closed, true);
    assert.equal(events.includes("backend-stopped"), true);
    assert.ok(logs.some(message => message.includes("cleanup failed") && message.includes("actual release rejected")), "the first actual failure remains in the desktop log");
    if (transport === "connected") {
      assert.equal(requests.length, 4);
      assert.deepEqual(remote.hosts.saved.releasedWindows, [oldRelease]);
    } else {
      assert.equal(requests.length, 2, "offline explicit Quit cannot replay release requests");
      assert.deepEqual(remote.hosts.saved.releasedWindows.map(item => item.windowInstanceId), [oldRelease.windowInstanceId, window.windowInstanceId]);
    }
  });
}

test("normal transport close owns the native response until its complete JSON body is parsed", { timeout: 10_000 }, async t => {
  const { remote, host, record, requestFor, outcome } = await releaseHttpFixture(t);
  const window = record("body-owner"), ordinary = outcome(remote.invoke(window, "minicode:pty:list", ["body-conversation"]));
  const request = await requestFor(window.windowInstanceId, "/api/desktop-host/native", 0);
  request.response.writeHead(200, { "content-type": "application/json" });
  request.response.write('{"body":');
  const closing = outcome(host.close());
  await new Promise(setImmediate);
  assert.equal(host.closed, false, "HTTP headers and a partial body are not a completed native operation");
  await assert.rejects(remote.invoke(window, "minicode:pty:list", []), /attachment is disconnecting/);
  request.response.end('"complete"}');
  assert.deepEqual(await ordinary, { value: { body: "complete" } });
  assert.equal((await closing).error, undefined);
  assert.equal(host.closed, true);
  assert.equal(host.nativeRequests.size, 0);
});

test("an actual transport error interrupts a normal close that is draining native HTTP", { timeout: 10_000 }, async t => {
  const { remote, host, record, requestFor, outcome } = await releaseHttpFixture(t);
  const window = record("interrupted-owner"), ordinary = outcome(remote.invoke(window, "minicode:pty:list", ["interrupted-conversation"]));
  await requestFor(window.windowInstanceId, "/api/desktop-host/native", 0);
  const statuses = [];
  host.on("status", (status, error) => statuses.push({ status, error }));
  const closing = outcome(host.close());
  assert.equal(host.closed, false);
  const failure = new Error("actual attachment I/O failure during native drain");
  const interrupted = outcome(host.close(failure));
  assert.match((await ordinary).error.message, /fetch failed/);
  assert.equal((await closing).error, undefined);
  assert.equal((await interrupted).error, undefined);
  assert.equal(host.nativeRequests.size, 0);
  assert.deepEqual(statuses, [{ status: "disconnected", error: failure }]);
});

async function nativeBindingDaemon(t) {
  const root = path.resolve(__dirname, ".."), state = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-binding-owner-"));
  const bundled = path.join(__dirname, "python-runtime", "python.exe");
  const python = process.env.MINICODE_TEST_PYTHON || (fs.existsSync(bundled) ? bundled : "python");
  const environment = { ...process.env, MINICODE_CONFIG_DIR: path.join(state, "config"),
    MINICODE_MANAGED_SETTINGS_DIR: path.join(state, "managed"), MINICODE_REQUIREMENTS_FILE: path.join(state, "requirements.toml"),
    MINICODE_SYSTEM_CONFIG_FILE: path.join(state, "system.toml"), PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" };
  const bindings = [], arrivals = new EventEmitter();
  const bridge = http.createServer((request, response) => {
    const parts = [];
    request.on("data", part => parts.push(part));
    request.once("end", () => {
      bindings.push({ path: request.url, headers: request.headers, body: JSON.parse(Buffer.concat(parts)), response });
      arrivals.emit("binding");
    });
  });
  bridge.listen(0, "127.0.0.1");
  await once(bridge, "listening");
  let remote;
  remote = new RemoteDesktop({ userData: state, bridgeEndpoint: `http://127.0.0.1:${bridge.address().port}`,
    windowManager: { getMainWindows: () => [] }, browserBridge: { registerRemoteHost() {}, unregisterRemoteHost() {} }, log() {},
    spawnTransport: (_program, args, options) => spawn(python, ["-X", "dev", "-X", "utf8", "-m", "backend.desktop_remote", "proxy",
      "--desktop-id", remote.hosts.saved.desktopId, "--marker", args.at(-1), "--state-root", state], { ...options, cwd: root, env: environment }),
  });
  const id = remote.hosts.saved.desktopId, daemonDir = path.join(state, "desktop-hosts", id), daemonFile = path.join(daemonDir, "daemon.json");
  fs.mkdirSync(daemonDir, { recursive: true });
  const ready = new Promise(resolve => {
    const watcher = fs.watch(daemonDir, (_event, name) => {
      if (name === "daemon.json" && fs.existsSync(daemonFile)) { watcher.close(); resolve(); }
    });
  });
  const logFile = path.join(daemonDir, "host.log"), logFd = fs.openSync(logFile, "a");
  const daemon = spawn(python, ["-X", "dev", "-X", "utf8", "-m", "backend.desktop_remote", "serve",
    "--desktop-id", id, "--state-root", state], { cwd: root, env: environment, windowsHide: true, stdio: ["ignore", logFd, logFd] });
  fs.closeSync(logFd);
  const daemonClosed = once(daemon, "close");
  await ready;
  await remote.save({ id: "ordinary-native", kind: "wsl", distribution: "owned", displayName: "Ordinary native owner" });
  const host = await remote.connect("ordinary-native");
  let lifecycleStarts = 0;
  const native = host.native.bind(host);
  host.native = (...args) => { if (args[1] === "minicode:window:release") lifecycleStarts++; return native(...args); };
  const record = { remoteHostId: host.connection.id, windowOwnerId: "owned-surface", windowInstanceId: "closed-surface",
    windowBindingToken: "owned-binding", runtime: host.runtime() };
  const outcome = promise => promise.then(value => ({ value }), error => ({ error }));
  const waitForBindings = async count => { while (bindings.length < count) await once(arrivals, "binding"); };
  let answered = 0;
  const respond = status => {
    for (const binding of bindings.slice(answered)) {
      binding.response.writeHead(status, { "content-type": "application/json" });
      binding.response.end(JSON.stringify({ ok: status === 200 }));
    }
    answered = bindings.length;
  };
  let finishing;
  const finish = () => finishing || (finishing = (async () => {
    respond(403);
    await host.close();
    const stopping = spawn(python, ["-m", "backend.desktop_remote", "stop", "--desktop-id", id, "--state-root", state],
      { cwd: root, env: environment, windowsHide: true, stdio: "pipe" });
    let stderr = "";
    stopping.stderr.on("data", data => { stderr += data; });
    const [code] = await once(stopping, "close");
    assert.equal(code, 0, stderr);
    const [daemonCode] = await daemonClosed;
    assert.equal(daemonCode, 0);
    await new Promise(resolve => bridge.close(resolve));
    const log = fs.readFileSync(logFile, "utf8");
    t.diagnostic(JSON.stringify({ state, generation: host.ready.generation, daemonCode,
      asgiErrors: (log.match(/Exception in ASGI application/g) || []).length,
      httpxProtocolErrors: (log.match(/httpx\.RemoteProtocolError/g) || []).length,
      unclosedReceiveStreams: (log.match(/Unclosed <MemoryObjectReceiveStream/g) || []).length }));
    return log;
  })());
  t.after(finish);
  return { remote, host, record, bindings, waitForBindings, respond, outcome, finish, lifecycleStarts: () => lifecycleStarts };
}

for (const action of ["transport-close", "window-release-disconnect"]) {
  test(`real reverse binding requests finish before ${action}`, { timeout: 90_000 }, async t => {
    const fixture = await nativeBindingDaemon(t);
    const { remote, host, record, bindings, waitForBindings, respond, outcome, finish } = fixture;
    const ordinary = Array.from({ length: 5 }, () => outcome(remote.invoke(record, "minicode:pty:list", ["actual-conversation"])));
    await waitForBindings(5);
    for (const binding of bindings) {
      assert.equal(binding.path, "/v1/window-binding");
      assert.equal(binding.headers.authorization, `Bearer ${host.ready.bridge_token}`);
      assert.equal(binding.body.window_instance_id, record.windowInstanceId);
    }
    let release;
    if (action === "window-release-disconnect") release = outcome(remote.releaseWindow(record));
    const closing = outcome(action === "transport-close" ? host.close() : remote.disconnect(host.connection.id));
    await new Promise(setImmediate);
    const closedBeforeBindingAck = host.closed, lifecycleBeforeBindingAck = fixture.lifecycleStarts();
    respond(action === "transport-close" ? 200 : 403);
    const results = await Promise.all(ordinary);
    const released = release && await release, detached = await closing;
    const log = await finish();
    assert.equal(closedBeforeBindingAck, false, "transport closed while real Native._binding HTTPX calls were outstanding");
    assert.equal(lifecycleBeforeBindingAck, 0, "window resources were released before its accepted native requests finished");
    assert.equal(detached.error, undefined);
    if (release) assert.deepEqual(released, { value: { pending: false } });
    for (const result of results) {
      if (action === "transport-close") assert.deepEqual(result, { value: [] });
      else assert.match(result.error.message, /minicode:pty:list: HTTP 403.*binding is no longer active/);
    }
    assert.doesNotMatch(log, /Exception in ASGI application|httpx\.RemoteProtocolError|Unclosed <MemoryObjectReceiveStream|ResourceWarning|Traceback/);
  });
}

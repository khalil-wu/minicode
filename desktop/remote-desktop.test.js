"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { once } = require("node:events");
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
  const host = { closed: false, ready: { runtime_token: "actual-runtime", bridge_token: "private-lifecycle", generation: "same-real-daemon" },
    runtime: () => ({ apiBaseUrl: `http://127.0.0.1:${backend.address().port}`, runtimeToken: "actual-runtime" }), native: RemoteHost.prototype.native };
  await cold.flushReleasedWindows("new-connection", host);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(call => call.path).sort(), ["/api/desktop-host/native", "/api/workspace/language/lifecycle"]);
  for (const call of calls) assert.equal(call.body.window_instance_id, record.windowInstanceId);
  assert.equal(cold.hosts.saved.releasedWindows.length, 0);
  assert.equal(new RemoteDesktop(deps).hosts.saved.releasedWindows.length, 0);
});

test("connection configuration does not persist caller supplied live status or expose runtime secrets", () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-remote-config-"));
  const remote = new RemoteDesktop({ userData: profile, windowManager: { getMainWindows: () => [] } });
  remote.save({ id: "actual-config", kind: "ssh", displayName: "My server", sshHost: "user@host", status: "connected", runtimeToken: "untrusted-token" });
  const listed = remote.list()[0];
  assert.equal(listed.status, "disconnected");
  assert.equal(listed.runtimeToken, undefined);
  const stored = JSON.parse(fs.readFileSync(path.join(profile, "remote-hosts.json"), "utf8"));
  assert.equal(stored.connections[0].status, undefined);
  assert.equal(stored.connections[0].runtimeToken, undefined);
});

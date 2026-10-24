"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { RemoteDesktop } = require("./remote-desktop");
const { IPC_CAPABILITIES } = require("./security");

test("personalization lists only real connected hosts and keeps per-host tokens in main", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-personalization-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const remote = new RemoteDesktop({ userData: directory, windowManager: { getMainWindows: () => [] } });
  const actualRuntime = { apiBaseUrl: "http://127.0.0.1:9911", runtimeToken: "private-connected-token" };
  const localRuntime = { apiBaseUrl: "http://127.0.0.1:9912", runtimeToken: "private-local-token" };
  remote.hosts.hosts.set("remote-a", { closed: false, status: "connected", connection: { id: "remote-a", displayName: "Lab", kind: "ssh" }, runtime: () => actualRuntime });
  remote.hosts.hosts.set("offline-b", { closed: false, status: "disconnected", connection: { id: "offline-b", displayName: "Offline", kind: "wsl" } });
  remote.hosts.connect = () => { throw new Error("Preferences must never connect or authenticate a host"); };
  const hosts = remote.personalizationHosts();
  assert.deepEqual(hosts, [{ id: "local", displayName: "Local", kind: "local" }, { id: "remote-a", displayName: "Lab", kind: "ssh" }]);
  assert.equal(JSON.stringify(hosts).includes("private-"), false);
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    requests.push({ url: String(url), ...init });
    return Response.json({ feature_enabled: true, memories: { generate_memories: true, use_memories: true, disable_on_external_context: false } });
  });
  const payload = await remote.personalizationRequest("remote-a", "update-memory", { enabled: true }, localRuntime);
  await remote.personalizationRequest("local", "read-memory", undefined, localRuntime);
  assert.equal(payload.feature_enabled, true);
  assert.equal(requests[0].url, "http://127.0.0.1:9911/api/settings/memory");
  assert.equal(requests[0].headers["X-MiniCode-Token"], "private-connected-token");
  assert.deepEqual(JSON.parse(requests[0].body), { enabled: true });
  assert.equal(requests[1].headers["X-MiniCode-Token"], "private-local-token");
  assert.equal(JSON.stringify(payload).includes("private-"), false);
  await assert.rejects(remote.personalizationRequest("offline-b", "read-memory", undefined, localRuntime), /disconnected/);
  await assert.rejects(remote.personalizationRequest("remote-a", "/api/llm/settings", undefined, localRuntime), /Unknown personalization/);
  assert.equal(requests.length, 2);
});

test("preload forwards selected-host operations without replacing the window runtime", async () => {
  const owned = { apiBaseUrl: "http://127.0.0.1:8000", wsBaseUrl: "ws://127.0.0.1:8000", runtimeToken: "owned-window-token" };
  const invokes = [];
  let exposed;
  const electron = {
    ipcRenderer: { sendSync: () => owned, on() {}, removeListener() {}, invoke: async (...args) => { invokes.push(args); return {}; } },
    contextBridge: { exposeInMainWorld: (_key, value) => { exposed = value; } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.js"), "utf8"), {
    require: name => name === "electron" ? electron : require(name), process: { ...process, contextIsolated: true, argv: ["electron"], env: {} },
    console, Buffer, URL, setTimeout, clearTimeout, crypto: require("node:crypto").webcrypto,
  });
  await exposed.desktop.personalizations.listHosts();
  await exposed.desktop.personalizations.request("remote-a", "update-instructions", { instructions: "owned draft" });
  assert.deepEqual(invokes, [["minicode:personalizations:listHosts"], ["minicode:personalizations:request", "remote-a", "update-instructions", { instructions: "owned draft" }]]);
  assert.equal(exposed.apiBaseUrl, owned.apiBaseUrl);
  assert.equal(exposed.runtimeToken, owned.runtimeToken);
  assert.equal(IPC_CAPABILITIES.has("minicode:personalizations:listHosts"), true);
  assert.equal(IPC_CAPABILITIES.has("minicode:personalizations:request"), true);
});

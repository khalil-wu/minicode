"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { DatabaseSync } = require("node:sqlite");
const { createBrowserProfileImporter } = require("./browser-profile-import");
const history = require("./browser-history");

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-profile-import-test-"));
  const rootPath = path.join(directory, "chrome");
  const profilePath = path.join(rootPath, "Default");
  fs.mkdirSync(profilePath, { recursive: true });
  const historyPath = path.join(directory, "browser-history.json");
  history.init(historyPath);
  const localState = { profile: { info_cache: { Default: { name: "Personal" }, "Profile 1": { name: "Empty" } } } };
  fs.writeFileSync(path.join(rootPath, "Local State"), JSON.stringify(localState));
  const database = new DatabaseSync(path.join(profilePath, "History"));
  database.exec("PRAGMA journal_mode=WAL; CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT); CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);");
  t.after(() => { database.close(); fs.rmSync(directory, { recursive: true, force: true }); });
  const addVisit = (id, url, title, milliseconds) => {
    database.prepare("INSERT INTO urls (id, url, title) VALUES (?, ?, ?)").run(id, url, title);
    database.prepare("INSERT INTO visits (id, url, visit_time) VALUES (?, ?, ?)").run(id, id, (BigInt(milliseconds) + 11644473600000n) * 1000n + 899n);
  };
  const service = createBrowserProfileImporter({ history, rootPath, temporaryRoot: directory });
  const request = { source: "chrome", profilePath, importHistory: true, importCookies: false, importPasswords: false, importExtensions: false };
  return { directory, rootPath, profilePath, historyPath, database, addVisit, service, request };
}

test("discovers named Chrome profiles with history without offering empty profiles", t => {
  const { service, rootPath, profilePath } = fixture(t);
  assert.deepEqual(service.listImportableBrowserProfiles(), [{ source: "chrome", appName: "Google Chrome", profileName: "Personal", profileDirectoryName: "Default", profilePath, rootPath, hasHistory: true }]);
  const missing = createBrowserProfileImporter({ history, rootPath: path.join(rootPath, "not-installed") });
  assert.deepEqual(missing.listImportableBrowserProfiles(), []);
  fs.writeFileSync(path.join(rootPath, "Local State"), JSON.stringify({}));
  assert.deepEqual(service.listImportableBrowserProfiles(), []);
});

test("imports a live WAL snapshot, preserves native history and deduplicates after restart", async t => {
  const { directory, historyPath, addVisit, service, request } = fixture(t);
  const timestamp = Date.UTC(2026, 9, 20, 5, 30, 0, 123);
  addVisit(1, "https://example.com/docs", "Imported docs", timestamp);
  addVisit(2, "chrome://settings", "Private settings", timestamp - 10);
  addVisit(3, "not a URL", "Invalid URL", timestamp - 20);
  history.recordVisit("https://existing.example/", "Existing conversation page", "agent");
  const first = await service.importBrowserProfile(request);
  assert.deepEqual(first.history, { status: "completed", discovered: 3, imported: 1, skippedExisting: 0, skippedInvalid: 2, failed: 0 });
  history.init(historyPath);
  assert.equal(history.search({ source: "agent" })[0].title, "Existing conversation page");
  assert.deepEqual(history.search({ text: "Imported docs", source: "other" }).map(({ url, title, visitTime }) => ({ url, title, visitTime })), [{ url: "https://example.com/docs", title: "Imported docs", visitTime: timestamp }]);
  const second = await service.importBrowserProfile(request);
  assert.deepEqual(second.history, { status: "completed", discovered: 3, imported: 0, skippedExisting: 1, skippedInvalid: 2, failed: 0 });
  assert.equal(history.search().length, 2);
  assert.deepEqual(fs.readdirSync(directory).filter(name => name.startsWith("minicode-browser-import-")), []);
});

test("rejects undiscovered paths and unsupported secret imports before reading their files", async t => {
  const { service, request } = fixture(t);
  await assert.rejects(service.importBrowserProfile({ ...request, profilePath: path.join(os.tmpdir(), "unselected-profile") }), /no longer importable/);
  await assert.rejects(service.importBrowserProfile({ ...request, importCookies: true }), /history only/);
  await assert.rejects(service.importBrowserProfile({ ...request, importHistory: false }), /history only/);
  assert.deepEqual(history.search(), []);
});

test("owned native IPC reaches persisted history and blocks a foreign frame", async t => {
  const { addVisit, service, request } = fixture(t);
  addVisit(1, "https://import.example/", "IPC import", Date.now());
  const handlers = new Map();
  const mainFrame = {};
  const contents = { mainFrame };
  const window = { webContents: contents, isDestroyed: () => false };
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "ipc-handlers.js"), "utf8"), { module: moduleBox, process, console,
    require(name) { return name === "electron" ? { ipcMain: { on() {}, handle(channel, handler) { handlers.set(channel, handler); } } } : require(name); },
  });
  moduleBox.exports.init({ browserProfileImport: service, browserHistory: history, getWindowForSender: sender => sender === contents ? window : null,
    getWindowRecord: () => ({ windowInstanceId: "owned-import-window" }) });
  moduleBox.exports.registerIpcHandlers();
  const event = { sender: contents, senderFrame: mainFrame };
  assert.equal((await handlers.get("minicode:browserProfileImport:list")(event))[0].profilePath, request.profilePath);
  const result = await handlers.get("minicode:browserProfileImport:import")(event, request);
  assert.equal(result.history.imported, 1);
  assert.equal(handlers.get("minicode:browser:history")(event, { text: "IPC import" })[0].source, "other");
  assert.throws(() => handlers.get("minicode:browserProfileImport:import")({ sender: contents, senderFrame: {} }, request), { code: "ERR_UNTRUSTED_IPC_SENDER" });
});

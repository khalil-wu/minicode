"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const assetRoot = path.resolve(__dirname, "../frontend/public/pets");
function harness(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-pets-test-"));
  const handles = new Map();
  const listeners = new Map();
  const windows = [];
  const openedFolders = [];
  const settingsOwners = [];
  const changes = [];
  const openedTasks = [];
  const openedMain = [];
  const taskActions = [];
  const followups = [];
  const clock = { now: Date.now() };
  const timers = [];
  let actionError = null;
  let contextMenu;
  const main = { getBounds: () => ({ x: 100, y: 100, width: 1000, height: 700 }) };
  class Window extends EventEmitter {
    constructor(options) { super(); this.options = options; this.visible = false; this.destroyed = false; this.bounds = { x: 0, y: 0, width: options.width, height: options.height }; this.webContents = new EventEmitter(); this.webContents.sent = []; this.webContents.send = (...args) => this.webContents.sent.push(args); this.webContents.setWindowOpenHandler = handler => { this.webContents.openHandler = handler; }; windows.push(this); }
    isDestroyed() { return this.destroyed; }
    isVisible() { return this.visible; }
    setMenuBarVisibility() {}
    setIgnoreMouseEvents(ignored, options) { this.mouseIgnored = ignored; this.mouseOptions = options; }
    setFocusable(value) { this.focusable = value; }
    focus() { this.focused = true; }
    setBounds(bounds) { this.bounds = bounds; }
    getBounds() { return this.bounds; }
    setPosition(x, y) { this.bounds = { ...this.bounds, x, y }; }
    async loadFile(file) { this.file = file; }
    showInactive() { this.visible = true; }
    hide() { this.visible = false; }
    destroy() { this.destroyed = true; this.visible = false; this.emit("closed"); }
  }
  const moduleValue = { exports: {} };
  const electron = {
    BrowserWindow: Window, app: { getLocale: () => "en-US" },
    ipcMain: { handle: (name, fn) => handles.set(name, fn), removeHandler: name => handles.delete(name), on: (name, fn) => listeners.set(name, fn), removeAllListeners: name => listeners.delete(name) },
    screen: { getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 2000, height: 1200 } }), getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 2000, height: 1200 } }), getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 2000, height: 1200 } }) },
    shell: { openPath: async folder => { openedFolders.push(folder); return ""; } },
    Menu: { buildFromTemplate: template => { contextMenu = template; return { popup() {} }; } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "pet-manager.js"), "utf8"), {
    module: moduleValue, __dirname, Buffer, Date: class extends Date { static now() { return clock.now; } },
    setTimeout(callback, delay) { const timer = { callback, delay, unref() {} }; timers.push(timer); return timer; }, clearTimeout(timer) { timer.cleared = true; },
    require: name => name === "electron" ? electron : name === "./utils" ? { atomicWriteTextSync(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, value); } } : require(name),
  });
  const manager = moduleValue.exports.createPetManager({ configRoot: root, builtinAssetRoot: assetRoot, getMainWindow: () => main, onOpenSettings: owner => settingsOwners.push(owner),
    onOpenMain: owner => openedMain.push(owner), onOpenTask: (owner, id) => openedTasks.push({ owner, id }),
    onTaskAction: async (owner, taskId, actionId) => { taskActions.push({ owner, taskId, actionId }); if (actionError) throw new Error(actionError); },
    onTaskFollowup: async (owner, taskId, prompt) => { followups.push({ owner, taskId, prompt }); if (actionError) throw new Error(actionError); }, onStateChanged: state => changes.push(state) });
  t.after(() => { manager.dispose(); assert.equal(path.dirname(root), os.tmpdir()); assert.match(path.basename(root), /^minicode-pets-test-/); fs.rmSync(root, { recursive: true, force: true }); });
  return { ...moduleValue.exports, manager, root, main, windows, handles, listeners, openedFolders, settingsOwners, openedTasks, openedMain, taskActions, followups, changes, clock, timers,
    setActionError(value) { actionError = value; }, get menu() { return contextMenu; } };
}
function customPet(root, name = "fixture-pet") {
  const directory = path.join(root, "pets", name);
  fs.mkdirSync(directory, { recursive: true });
  fs.copyFileSync(path.join(assetRoot, "codex.webp"), path.join(directory, "spritesheet.webp"));
  fs.writeFileSync(path.join(directory, "pet.json"), JSON.stringify({ displayName: "Fixture Pet", description: "Local fixture", spriteVersionNumber: 2, spritesheetPath: "spritesheet.webp" }));
  return directory;
}

test("follow-up focuses only on user input and sends the preserved prompt to its real owner", async t => {
  const h = harness(t);
  await h.manager.initialize();
  h.manager.publishActivity({ running: true, waiting: false, failed: false, review: false,
    tasks: [{ id: "actual-task", title: "Actual task", status: "running", followUpAllowed: true }] }, "actual-owner");
  await h.manager.toggleMini();
  const window = h.windows[0];
  const act = h.handles.get("minicode:pets:mini-action");
  await act({ sender: window.webContents }, "focus-followup", { taskId: "actual-task" });
  assert.equal(window.focusable, true);
  assert.equal(window.focused, true);
  await act({ sender: window.webContents }, "task-followup", { taskId: "actual-task", prompt: "Keep the exact message" });
  assert.deepEqual(h.followups, [{ owner: "actual-owner", taskId: "actual-task", prompt: "Keep the exact message" }]);
  window.emit("blur");
  assert.equal(window.focusable, false);
  h.setActionError("Actual backend refusal");
  await assert.rejects(act({ sender: window.webContents }, "task-followup", { taskId: "actual-task", prompt: "Retained on failure" }), /Actual backend refusal/);
});

test("pet initialization loads the nine real Source atlases without opening a native window", async t => {
  const h = harness(t);
  const state = await h.manager.initialize();
  assert.deepEqual(Array.from(state.pets, pet => pet.id), ["codex", "dewey", "fireball", "hoots", "rocky", "seedy", "stacky", "bsod", "null-signal"]);
  assert.equal(state.size, 112); assert.equal(state.miniOpen, false); assert.equal(h.windows.length, 0);
  assert.equal(state.petDirectory, path.join(h.root, "pets"));
  for (const pet of state.pets) { const dimensions = h.imageDimensions(fs.readFileSync(path.join(assetRoot, `${pet.assetRef}.webp`))); assert.equal(dimensions.width, 1536); assert.equal(dimensions.height, 2288); }
});

test("Refresh reads real local pet packages and preserves malformed records as visible issues", async t => {
  const h = harness(t); await h.manager.initialize();
  customPet(h.root);
  const broken = path.join(h.root, "pets", "broken"); fs.mkdirSync(broken); fs.writeFileSync(path.join(broken, "pet.json"), "{broken");
  const state = await h.manager.refresh();
  assert.equal(state.pets[0].id, "custom:fixture-pet");
  assert.equal(state.issues.length, 1); assert.match(state.issues[0], /broken.*pet\.json/);
  assert.match(await h.manager.getSprite("custom:fixture-pet"), /^data:image\/webp;base64,/);
  await h.manager.update({ selectedPetId: "custom:fixture-pet", size: 160 });
  const saved = JSON.parse(fs.readFileSync(path.join(h.root, "pets-settings.json"), "utf8"));
  assert.equal(saved.selectedPetId, "custom:fixture-pet"); assert.equal(saved.size, 160);
  await h.manager.openFolder(); assert.deepEqual(h.openedFolders, [path.join(h.root, "pets")]);
  await assert.rejects(h.manager.update({ selectedPetId: "invented-pet" }), /Unsupported pet preference/);
  await assert.rejects(h.manager.update({ size: 300 }), /Unsupported pet preference/);
  assert.equal(h.windows.length, 0);
});

test("Wake, Tuck, visibility, size and the two context actions consume one native Mini instance", async t => {
  const h = harness(t); await h.manager.initialize();
  assert.equal((await h.manager.toggleMini()).miniOpen, true);
  const window = h.windows[0];
  assert.equal(window.options.transparent, true); assert.equal(window.options.focusable, false); assert.equal(window.options.webPreferences.nodeIntegration, false);
  assert.equal(window.mouseIgnored, true);
  h.handles.get("minicode:pets:mini-action")({ sender: window.webContents }, "pointer-interaction", { enabled: true });
  assert.equal(window.mouseIgnored, false);
  assert.equal(window.webContents.openHandler().action, "deny");
  await h.manager.update({ size: 224, reducedMotion: true });
  assert.equal(window.getBounds().width, 248); assert.equal(h.manager.getState().reducedMotion, true);
  assert.equal((await h.manager.toggleMini()).miniOpen, false);
  assert.equal((await h.manager.toggleMini()).miniOpen, true);
  assert.equal(h.windows.length, 1);
  h.handles.get("minicode:pets:mini-action")({ sender: window.webContents }, "context-menu");
  assert.deepEqual(Array.from(h.menu, item => item.label), ["Settings", "Close Mini"]);
  h.menu[0].click(); assert.equal(h.settingsOwners[0], h.main);
  h.menu[1].click(); assert.equal(h.manager.getState().miniOpen, false); assert.equal(window.destroyed, false);
  await h.manager.toggleMini();
  await h.manager.update({ petVisible: false });
  assert.equal(h.manager.getState().miniOpen, false); assert.equal(h.manager.getState().petVisible, false);
  assert.equal(h.windows.length, 1);
});

test("profile visibility updates show and hide the real existing Mini instead of only changing its saved preference", async t => {
  const h = harness(t); await h.manager.initialize();
  assert.equal(h.manager.getState().petVisible, true);
  assert.equal(h.manager.getState().miniOpen, false);
  const shown = await h.manager.update({ petVisible: true });
  assert.equal(shown.petVisible, true); assert.equal(shown.miniOpen, true);
  assert.equal(h.windows.length, 1); assert.equal(h.windows[0].visible, true);
  const hidden = await h.manager.update({ petVisible: false });
  assert.equal(hidden.petVisible, false); assert.equal(hidden.miniOpen, false);
  assert.equal(h.windows[0].visible, false); assert.equal(h.windows[0].destroyed, false);
  const restored = await h.manager.update({ petVisible: true });
  assert.equal(restored.petVisible, true); assert.equal(restored.miniOpen, true);
  assert.equal(h.windows.length, 1); assert.equal(h.changes.at(-1).miniOpen, true);
});

test("real window owners aggregate activity until their own lifecycle releases it", async t => {
  const h = harness(t); await h.manager.initialize();
  const idle = { running: false, waiting: false, failed: false, review: false, tasks: [] };
  h.manager.publishActivity({ ...idle, running: true }, "window-a");
  h.manager.publishActivity(idle, "window-b"); assert.equal(h.manager.getState().activity.running, true);
  h.manager.publishActivity({ ...idle, waiting: true }, "window-b");
  assert.deepEqual(JSON.parse(JSON.stringify(h.manager.getState().activity)), { ...idle, running: true, waiting: true });
  h.manager.releaseActivity("window-a"); assert.deepEqual(JSON.parse(JSON.stringify(h.manager.getState().activity)), { ...idle, waiting: true });
  h.manager.releaseActivity("window-b"); assert.deepEqual(JSON.parse(JSON.stringify(h.manager.getState().activity)), idle);
});

test("native drag stays on its display and private IPC rejects unrelated renderers", async t => {
  const h = harness(t); await h.manager.initialize(); await h.manager.toggleMini();
  const window = h.windows[0]; const event = { sender: window.webContents };
  assert.throws(() => h.handles.get("minicode:pets:mini-get-state")({ sender: {} }), /requires the Mini window/);
  const action = h.handles.get("minicode:pets:mini-action");
  const bounds = window.getBounds();
  action(event, "drag-start", { x: bounds.x + 5, y: bounds.y + 5 });
  action(event, "drag-move", { x: 9000, y: 9000 });
  action(event, "drag-end", { x: 9000, y: 9000 });
  const final = window.getBounds();
  assert.ok(final.x + final.width <= 2000 && final.y + final.height <= 1200);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(h.root, "pets-settings.json"))).position, { x: final.x + 12, y: final.y + 12 });
});

test("attention cards keep true task identities, deduplicate owners and route to the owning main thread", async t => {
  const h = harness(t); await h.manager.initialize();
  const idle = { running: false, waiting: false, failed: false, review: false, tasks: [] };
  h.manager.publishActivity({ ...idle, running: true, tasks: [{ id: "shared", title: "Actual running chat", status: "running" }] }, "window-a");
  h.manager.publishActivity({ ...idle, waiting: true, tasks: [{ id: "shared", title: "Actual approval", status: "waiting" }] }, "window-b");
  assert.equal(h.manager.getState().activity.tasks.length, 1);
  assert.equal(h.manager.getState().activity.tasks[0].title, "Actual approval");
  await h.manager.toggleMini();
  const window = h.windows[0]; const event = { sender: window.webContents };
  assert.equal(window.getBounds().width, 354);
  const action = h.handles.get("minicode:pets:mini-action");
  action(event, "open-main"); assert.equal(h.openedMain.length, 1);
  action(event, "open-task", { id: "shared" }); assert.deepEqual(h.openedTasks, [{ owner: "window-b", id: "shared" }]);
  assert.throws(() => action(event, "open-task", { id: "invented" }), /no longer available/);
  h.manager.releaseActivity("window-b");
  action(event, "open-task", { id: "shared" }); assert.deepEqual(h.openedTasks.at(-1), { owner: "window-a", id: "shared" });
});

test("only the request's actual action reaches its owner and a backend rejection keeps the request", async t => {
  const h = harness(t); await h.manager.initialize();
  h.manager.publishActivity({ running: false, waiting: true, failed: false, review: false,
    tasks: [{ id: "approval-chat", title: "Actual request", status: "waiting", detail: "Actual command", actions: [{ id: "request-7:allow", label: "Allow once" }] }] }, "request-owner");
  await h.manager.toggleMini();
  const action = h.handles.get("minicode:pets:mini-action"); const event = { sender: h.windows[0].webContents };
  assert.throws(() => action(event, "task-action", { taskId: "approval-chat", actionId: "invented" }), /no longer available/);
  h.setActionError("Backend rejected the actual request");
  await assert.rejects(action(event, "task-action", { taskId: "approval-chat", actionId: "request-7:allow" }), /Backend rejected/);
  assert.equal(h.manager.getState().activity.tasks[0].actions[0].id, "request-7:allow");
  assert.deepEqual(h.taskActions[0], { owner: "request-owner", taskId: "approval-chat", actionId: "request-7:allow" });
  h.setActionError(null);
  await action(event, "task-action", { taskId: "approval-chat", actionId: "request-7:allow" });
  assert.equal(h.manager.getState().activity.tasks.length, 1);
  h.manager.publishActivity({ running: false, waiting: false, failed: false, review: false, tasks: [] }, "request-owner");
  assert.equal(h.manager.getState().activity.tasks.length, 0);
});

test("the native notification Mute chat entry persists only Mini suppression and Unmute restores the retained task", async t => {
  const h = harness(t); await h.manager.initialize();
  const active = { running: true, waiting: false, failed: false, review: false, tasks: [{ id: "actual", title: "Actual task", status: "running", turnKey: "turn-1" }] };
  h.manager.publishActivity(active, "task-owner"); await h.manager.toggleMini();
  const event = { sender: h.windows[0].webContents };
  h.handles.get("minicode:pets:mini-action")(event, "task-context-menu", { taskId: "actual" });
  assert.deepEqual(Array.from(h.menu, item => item.label), ["Mute chat"]);
  h.menu[0].click();
  assert.deepEqual(Array.from(h.manager.getState().mutedConversationIds), ["actual"]);
  assert.equal(h.manager.getState().activity.tasks.length, 0);
  assert.equal(h.manager.getState().activity.running, true);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(h.root, "pets-settings.json"))).mutedConversationIds, ["actual"]);
  await h.manager.initialize();
  h.manager.publishActivity(active, "task-owner");
  assert.equal(h.manager.getState().activity.tasks.length, 0);
  h.manager.setNotificationMuted({ conversationId: "actual", muted: false });
  assert.equal(h.manager.getState().activity.tasks[0].title, "Actual task");
  const priorBroadcasts = h.changes.length;
  h.manager.setNotificationMuted({ conversationId: "not-currently-active", muted: true });
  assert.equal(h.changes.length, priorBroadcasts + 1);
  assert.equal(h.changes.at(-1).mutedConversationIds.includes("not-currently-active"), true);
});

test("a failed Mini mute keeps the task and exposes the real persistence error", async t => {
  const h = harness(t); await h.manager.initialize();
  h.manager.publishActivity({ running: true, waiting: false, failed: false, review: false, tasks: [{ id: "actual", title: "Actual task", status: "running" }] }, "owner");
  await h.manager.toggleMini();
  h.handles.get("minicode:pets:mini-action")({ sender: h.windows[0].webContents }, "task-context-menu", { taskId: "actual" });
  t.mock.method(fs, "writeFileSync", () => { throw new Error("Actual preference disk failure"); });
  h.menu[0].click();
  assert.equal(h.manager.getState().activity.tasks.length, 1);
  assert.deepEqual(Array.from(h.manager.getState().mutedConversationIds), []);
  const [channel, message] = h.windows[0].webContents.sent.at(-1);
  assert.equal(channel, "minicode:pets:mini-error");
  assert.match(message, /Actual preference disk failure/);
});

test("dismiss targets one turn and source expiration removes only the attention card", async t => {
  const h = harness(t); await h.manager.initialize();
  const active = { running: false, waiting: true, failed: false, review: false, tasks: [{ id: "actual", title: "Actual request", status: "waiting", turnKey: "turn-1", updatedAt: h.clock.now }] };
  h.manager.publishActivity(active, "owner"); await h.manager.toggleMini();
  const action = h.handles.get("minicode:pets:mini-action"); const event = { sender: h.windows[0].webContents };
  assert.throws(() => action(event, "dismiss-task", { taskId: "actual", turnKey: "old-turn" }), /has changed/);
  action(event, "dismiss-task", { taskId: "actual", turnKey: "turn-1" });
  h.manager.publishActivity(active, "owner"); assert.equal(h.manager.getState().activity.tasks.length, 0);
  h.manager.publishActivity({ ...active, tasks: [{ ...active.tasks[0], turnKey: "turn-2" }] }, "owner");
  assert.equal(h.manager.getState().activity.tasks.length, 1);
  assert.equal(h.timers.at(-1).delay, 86_400_000);
  h.clock.now += 86_400_000; h.timers.at(-1).callback();
  assert.equal(h.manager.getState().activity.tasks.length, 0);
  assert.equal(h.manager.getState().activity.waiting, true);
});

test("Review routes the unchanged request to the original main window without an approval response", async t => {
  const h = harness(t); await h.manager.initialize();
  h.manager.publishActivity({ running: false, waiting: true, failed: false, review: false,
    tasks: [{ id: "actual", title: "Actual complex input", status: "waiting", actions: [{ id: "open-original", label: "Review", openInMainWindow: true }] }] }, "actual-owner");
  await h.manager.toggleMini();
  await h.handles.get("minicode:pets:mini-action")({ sender: h.windows[0].webContents }, "task-action", { taskId: "actual", actionId: "open-original" });
  assert.deepEqual(h.openedTasks, [{ owner: "actual-owner", id: "actual" }]);
  assert.deepEqual(h.taskActions, []);
  assert.equal(h.manager.getState().activity.tasks.length, 1);
});

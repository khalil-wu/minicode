"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { BrowserWindow, Menu, app, ipcMain, screen, shell } = require("electron");
const { atomicWriteTextSync } = require("./utils");

const BUILTIN_PETS = [
  ["codex", "Codex", "The original Codex companion."],
  ["dewey", "Dewey", "A calm companion for focused workspace days"],
  ["fireball", "Fireball", "Hot path energy for fast iteration."],
  ["hoots", "Hoots", "A sharp-eyed owl for polished work in a blink."],
  ["rocky", "Rocky", "A steady rock when the diff gets large."],
  ["seedy", "Seedy", "Small green shoots for new ideas."],
  ["stacky", "Stacky", "A balanced stack for deep work."],
  ["bsod", "BSOD", "A tiny blue-screen gremlin."],
  ["null-signal", "Null Signal", "Quiet signal from the void."],
].map(([id, displayName, description]) => ({ id, displayName, description, assetRef: id, spriteVersionNumber: 2, updatedAt: 0 }));

const MINI_GET = "minicode:pets:mini-get-state";
const MINI_SPRITE = "minicode:pets:mini-get-sprite";
const MINI_ACTION = "minicode:pets:mini-action";
const MINI_STATE = "minicode:pets:mini-state";

function imageDimensions(bytes) {
  if (bytes.length >= 24 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
    return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20), mime: "image/png" };
  }
  if (bytes.length >= 30 && bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP") {
    const format = bytes.toString("ascii", 12, 16);
    if (format === "VP8X") return { width: bytes.readUIntLE(24, 3) + 1, height: bytes.readUIntLE(27, 3) + 1, mime: "image/webp" };
    if (format === "VP8L") {
      const dimensions = bytes.readUInt32LE(21);
      return { width: (dimensions & 16383) + 1, height: ((dimensions >>> 14) & 16383) + 1, mime: "image/webp" };
    }
    if (format === "VP8 ") return { width: bytes.readUInt16LE(26) & 16383, height: bytes.readUInt16LE(28) & 16383, mime: "image/webp" };
  }
  throw new Error("Pet spritesheet must be PNG or WebP.");
}

function readCustomPet(directory, folder) {
  const manifestPath = path.join(directory, folder, "pet.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const version = manifest.spriteVersionNumber ?? 1;
  if (version !== 1 && version !== 2) throw new Error("spriteVersionNumber must be 1 or 2.");
  const petRoot = path.join(directory, folder);
  const spritePath = path.resolve(petRoot, manifest.spritesheetPath ?? "spritesheet.webp");
  const relative = path.relative(petRoot, spritePath);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error("Pet spritesheet must be inside its pet folder.");
  const dimensions = imageDimensions(fs.readFileSync(spritePath));
  if (dimensions.width !== 1536 || dimensions.height !== (version === 2 ? 2288 : 1872)) {
    throw new Error(`Pet v${version} requires a 1536×${version === 2 ? 2288 : 1872} spritesheet.`);
  }
  const displayName = manifest.displayName ?? manifest.id ?? folder;
  if (typeof displayName !== "string" || !displayName.trim()) throw new Error("Pet displayName must be a non-empty string.");
  if (manifest.description != null && typeof manifest.description !== "string") throw new Error("Pet description must be a string.");
  return { pet: { id: `custom:${folder}`, displayName, description: manifest.description ?? "", spriteVersionNumber: version,
    updatedAt: Math.max(fs.statSync(manifestPath).mtimeMs, fs.statSync(spritePath).mtimeMs) }, spritePath };
}

function createPetManager({ configRoot, builtinAssetRoot, getMainWindow, onOpenSettings, onOpenMain, onOpenTask, onTaskAction, onTaskFollowup, onStateChanged, getLocale = () => app.getLocale() }) {
  const petDirectory = path.join(configRoot, "pets");
  const preferencesFile = path.join(configRoot, "pets-settings.json");
  const animations = JSON.parse(fs.readFileSync(path.join(builtinAssetRoot, "animations.json"), "utf8"));
  let preferences = { selectedPetId: "codex", size: 112, petVisible: true, miniOpen: false, position: null, mutedConversationIds: [] };
  let pets = BUILTIN_PETS;
  let customSpritePaths = new Map();
  let issues = [];
  const activityFlags = ["running", "waiting", "failed", "review"];
  let activity = { running: false, waiting: false, failed: false, review: false, tasks: [] };
  const activityOwners = new Map();
  let taskOwners = new Map();
  let reducedMotion = false;
  let miniWindow = null;
  let miniReady;
  let measuredNoticeHeight = null;
  let drag = null;
  let disposed = false;
  let expiryTimer = null;
  const dismissedTurns = new Map();
  const save = () => atomicWriteTextSync(preferencesFile, `${JSON.stringify(preferences, null, 2)}\n`);
  const miniLayout = () => {
    const petHeight = Math.ceil(preferences.size * 208 / 192);
    const estimated = activity.tasks.reduce((height, task) => height + 76 + (task.detail ? 36 : 0) + (task.actions?.length ? 40 : 0), 0);
    const noticeHeight = Math.min(260, measuredNoticeHeight ?? estimated);
    const width = activity.tasks.length ? Math.max(330, preferences.size) + 24 : preferences.size + 24;
    return { width, height: petHeight + 24 + noticeHeight, petLeft: width - preferences.size - 12, petTop: noticeHeight + 12, noticeHeight };
  };
  const getState = () => ({ pets, selectedPetId: preferences.selectedPetId, size: preferences.size, petVisible: preferences.petVisible,
    miniOpen: Boolean(miniWindow && !miniWindow.isDestroyed() && miniWindow.isVisible()), petDirectory, activity, reducedMotion, issues, mutedConversationIds: [...preferences.mutedConversationIds] });
  const notify = () => {
    const state = getState();
    if (miniWindow && !miniWindow.isDestroyed()) { if (!drag) positionMini(); miniWindow.webContents.send(MINI_STATE, { ...state, locale: getLocale(), animations, layout: miniLayout() }); }
    onStateChanged(state);
    return state;
  };
  const getSprite = async id => {
    const pet = pets.find(item => item.id === id);
    if (!pet) throw new Error(`Pet is unavailable: ${id}`);
    const spritePath = pet.assetRef ? path.join(builtinAssetRoot, `${pet.assetRef}.webp`) : customSpritePaths.get(id);
    const bytes = await fs.promises.readFile(spritePath);
    const { mime } = imageDimensions(bytes);
    return `data:${mime};base64,${bytes.toString("base64")}`;
  };
  const refresh = async () => {
    await fs.promises.mkdir(petDirectory, { recursive: true });
    const custom = [];
    const paths = new Map();
    const nextIssues = [];
    for (const entry of await fs.promises.readdir(petDirectory, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      try {
        const { pet, spritePath } = readCustomPet(petDirectory, entry.name);
        custom.push(pet); paths.set(pet.id, spritePath);
      } catch (error) { nextIssues.push(`${path.join(petDirectory, entry.name, "pet.json")}: ${error.message}`); }
    }
    pets = [...custom.sort((left, right) => right.updatedAt - left.updatedAt), ...BUILTIN_PETS];
    customSpritePaths = paths; issues = nextIssues;
    return notify();
  };
  const positionMini = () => {
    const { width, height, petLeft, petTop } = miniLayout();
    const workArea = preferences.position ? screen.getDisplayNearestPoint(preferences.position).workArea
      : screen.getDisplayMatching(getMainWindow()?.getBounds() ?? screen.getPrimaryDisplay().workArea).workArea;
    const point = preferences.position ?? { x: workArea.x + workArea.width - preferences.size - 24, y: workArea.y + workArea.height - Math.ceil(preferences.size * 208 / 192) - 24 };
    miniWindow.setBounds({ width, height, x: Math.round(Math.min(Math.max(point.x - petLeft, workArea.x), workArea.x + workArea.width - width)),
      y: Math.round(Math.min(Math.max(point.y - petTop, workArea.y), workArea.y + workArea.height - height)) });
  };
  const hideMini = () => {
    preferences.miniOpen = false;
    drag = null;
    if (miniWindow && !miniWindow.isDestroyed()) { miniWindow.setIgnoreMouseEvents(true, { forward: true }); miniWindow.hide(); }
    save(); return notify();
  };
  const showMini = async () => {
    if (disposed) throw new Error("Pet manager has been disposed.");
    if (!pets.some(pet => pet.id === preferences.selectedPetId)) throw new Error(`Selected pet is unavailable: ${preferences.selectedPetId}`);
    if (!miniWindow || miniWindow.isDestroyed()) {
      miniWindow = new BrowserWindow({ width: preferences.size + 24, height: Math.ceil(preferences.size * 208 / 192) + 24,
        title: "Mini", transparent: true, backgroundColor: "#00000000", frame: false, resizable: false, hasShadow: false,
        alwaysOnTop: true, skipTaskbar: true, show: false, focusable: false, maximizable: false, minimizable: false, fullscreenable: false,
        webPreferences: { preload: path.join(__dirname, "pet-preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true } });
      miniWindow.setMenuBarVisibility(false);
      miniWindow.setIgnoreMouseEvents(true, { forward: true });
      miniWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      miniWindow.webContents.on("will-navigate", event => event.preventDefault());
      miniWindow.on("blur", () => miniWindow.setFocusable(false));
      miniWindow.on("closed", () => { miniWindow = null; drag = null; if (!disposed) { preferences.miniOpen = false; save(); notify(); } });
      positionMini();
      miniReady = miniWindow.loadFile(path.join(__dirname, "pet-window.html"));
    } else positionMini();
    await miniReady;
    preferences.miniOpen = true; preferences.petVisible = true;
    miniWindow.showInactive(); save();
    return notify();
  };
  const update = async patch => {
    for (const [key, value] of Object.entries(patch)) {
      if (key === "selectedPetId" && typeof value === "string" && pets.some(pet => pet.id === value)) continue;
      if (key === "size" && Number.isInteger(value) && value >= 80 && value <= 224) continue;
      if ((key === "petVisible" || key === "reducedMotion") && typeof value === "boolean") continue;
      throw new Error(`Unsupported pet preference: ${key}`);
    }
    const { reducedMotion: nextMotion, ...savedPatch } = patch;
    if (nextMotion !== undefined) reducedMotion = nextMotion;
    preferences = { ...preferences, ...savedPatch };
    if (savedPatch.petVisible === false) return hideMini();
    if (savedPatch.petVisible === true) return showMini();
    save();
    if (miniWindow && !miniWindow.isDestroyed()) positionMini();
    return notify();
  };
  const requireMini = event => {
    if (!miniWindow || miniWindow.isDestroyed() || event.sender !== miniWindow.webContents) throw new Error("Pet action requires the Mini window.");
  };
  const setNotificationMuted = ({ conversationId, muted }) => {
    if (typeof conversationId !== "string" || !conversationId.trim() || typeof muted !== "boolean") throw new Error("Mini notification preferences require a conversation and boolean muted state.");
    const ids = new Set(preferences.mutedConversationIds);
    if (muted === ids.has(conversationId)) return getState();
    if (muted) ids.add(conversationId); else ids.delete(conversationId);
    const next = { ...preferences, mutedConversationIds: [...ids] };
    atomicWriteTextSync(preferencesFile, `${JSON.stringify(next, null, 2)}\n`);
    preferences = next;
    return publishOwners(true);
  };
  ipcMain.handle(MINI_GET, event => { requireMini(event); return { ...getState(), locale: getLocale(), animations, layout: miniLayout() }; });
  ipcMain.handle(MINI_SPRITE, (event, id) => { requireMini(event); return getSprite(id); });
  ipcMain.handle(MINI_ACTION, (event, action, point) => {
    requireMini(event);
    if (action === "open-main") return onOpenMain();
    if (action === "pointer-interaction") {
      if (!point || typeof point.enabled !== "boolean") throw new Error("Mini pointer interaction requires a boolean.");
      miniWindow.setIgnoreMouseEvents(!point.enabled, { forward: true }); return;
    }
    if (action === "open-task") {
      if (!point || !taskOwners.has(point.id)) throw new Error("Mini task is no longer available.");
      return onOpenTask(taskOwners.get(point.id), point.id);
    }
    if (action === "task-action") {
      const task = point && activity.tasks.find(task => task.id === point.taskId);
      const selected = task?.actions?.find(item => item.id === point.actionId);
      if (!selected) throw new Error("Mini request is no longer available.");
      if (selected.openInMainWindow) return onOpenTask(taskOwners.get(task.id), task.id);
      return onTaskAction(taskOwners.get(task.id), task.id, point.actionId);
    }
    if (action === "dismiss-task") {
      const task = point && activity.tasks.find(task => task.id === point.taskId);
      if (!task || task.turnKey !== point.turnKey) throw new Error("Mini notification has changed.");
      dismissedTurns.set(task.id, task.turnKey);
      return publishOwners();
    }
    if (action === "task-context-menu") {
      const task = point && activity.tasks.find(task => task.id === point.taskId);
      if (!task) throw new Error("Mini notification is no longer available.");
      const chinese = /^zh/i.test(getLocale());
      Menu.buildFromTemplate([{ label: chinese ? "静音聊天" : "Mute chat", click: () => {
        try { setNotificationMuted({ conversationId: task.id, muted: true }); }
        catch (error) { miniWindow.webContents.send("minicode:pets:mini-error", error instanceof Error ? error.message : String(error)); }
      } }]).popup({ window: miniWindow });
      return;
    }
    if (action === "focus-followup") {
      if (!point || !activity.tasks.some(task => task.id === point.taskId && task.followUpAllowed)) throw new Error("This task cannot accept a follow-up.");
      miniWindow.setFocusable(true); miniWindow.focus(); return;
    }
    if (action === "task-followup") {
      const task = point && activity.tasks.find(task => task.id === point.taskId);
      if (!task?.followUpAllowed || typeof point.prompt !== "string" || !point.prompt.trim()) throw new Error("A follow-up needs an available task and message.");
      return onTaskFollowup(taskOwners.get(task.id), task.id, point.prompt);
    }
    if (action === "element-size-changed") {
      if (!point || !Number.isFinite(point.height) || point.height < 0) throw new Error("Mini content size must be a nonnegative height.");
      const height = Math.min(260, Math.ceil(point.height));
      if (height !== measuredNoticeHeight) { measuredNoticeHeight = height; notify(); }
      return;
    }
    if (action === "context-menu") {
      const chinese = /^zh/i.test(getLocale());
      Menu.buildFromTemplate([
        { label: chinese ? "设置" : "Settings", click: () => onOpenSettings(getMainWindow()) },
        { label: chinese ? "关闭 Mini" : "Close Mini", click: hideMini },
      ]).popup({ window: miniWindow });
      return;
    }
    if (!["drag-start", "drag-move", "drag-end"].includes(action) || !point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("Unknown Mini drag action.");
    if (action === "drag-start") { const bounds = miniWindow.getBounds(); drag = { x: point.x - bounds.x, y: point.y - bounds.y }; return; }
    if (!drag) return;
    const position = { x: Math.round(point.x - drag.x), y: Math.round(point.y - drag.y) };
    miniWindow.setPosition(position.x, position.y);
    if (action === "drag-end") {
      const layout = miniLayout(); preferences.position = { x: position.x + layout.petLeft, y: position.y + layout.petTop };
      drag = null; positionMini(); const bounds = miniWindow.getBounds(); preferences.position = { x: bounds.x + layout.petLeft, y: bounds.y + layout.petTop }; save();
    }
  });
  const publishOwners = (forceNotify = false) => {
    const priority = { waiting: 0, failed: 1, review: 2, running: 3 };
    const tasks = new Map();
    taskOwners = new Map();
    for (const [ownerId, owner] of activityOwners) for (const task of owner.tasks) {
      const previous = tasks.get(task.id);
      if (!previous || priority[task.status] < priority[previous.status]
        || priority[task.status] === priority[previous.status] && (task.actions?.length || 0) >= (previous.actions?.length || 0)) {
        tasks.set(task.id, task); taskOwners.set(task.id, ownerId);
      }
    }
    const now = Date.now();
    const durations = { failed: 3_600_000, waiting: 86_400_000, review: 604_800_000 };
    let expiresAt = null;
    const visible = [...tasks.values()].filter(task => {
      if (preferences.mutedConversationIds.includes(task.id) || dismissedTurns.has(task.id) && dismissedTurns.get(task.id) === task.turnKey) return false;
      const deadline = task.status === "running" || task.updatedAt === undefined ? null : task.updatedAt + durations[task.status];
      if (deadline !== null && deadline <= now) return false;
      if (deadline !== null && (expiresAt === null || deadline < expiresAt)) expiresAt = deadline;
      return true;
    });
    if (expiryTimer !== null) clearTimeout(expiryTimer);
    expiryTimer = expiresAt === null ? null : setTimeout(() => { expiryTimer = null; publishOwners(); }, expiresAt - now);
    expiryTimer?.unref?.();
    const next = { ...Object.fromEntries(activityFlags.map(key => [key, [...activityOwners.values()].some(owner => owner[key])])),
      tasks: visible.sort((left, right) => (right.sortAt ?? right.updatedAt ?? 0) - (left.sortAt ?? left.updatedAt ?? 0) || left.id.localeCompare(right.id)) };
    if (JSON.stringify(activity) === JSON.stringify(next)) return forceNotify ? notify() : getState();
    if (JSON.stringify(activity.tasks) !== JSON.stringify(next.tasks)) measuredNoticeHeight = null;
    activity = next; return notify();
  };
  return {
    getState, getSprite, refresh, update, setNotificationMuted,
    async initialize() {
      if (fs.existsSync(preferencesFile)) preferences = { ...preferences, ...JSON.parse(await fs.promises.readFile(preferencesFile, "utf8")) };
      if (!Array.isArray(preferences.mutedConversationIds) || preferences.mutedConversationIds.some(id => typeof id !== "string")) throw new Error("Mini muted conversation preferences must contain conversation ids.");
      return refresh();
    },
    restoreMini: () => preferences.miniOpen ? showMini() : Promise.resolve(getState()),
    toggleMini: () => getState().miniOpen ? Promise.resolve(hideMini()) : showMini(),
    async openFolder() { await fs.promises.mkdir(petDirectory, { recursive: true }); const error = await shell.openPath(petDirectory); if (error) throw new Error(error); return getState(); },
    publishActivity(next, ownerId) {
      for (const key of activityFlags) if (typeof next[key] !== "boolean") throw new Error(`Pet activity requires ${key}.`);
      if (!Array.isArray(next.tasks) || next.tasks.some(task => !task || typeof task.id !== "string" || typeof task.title !== "string" || !["running", "waiting", "failed", "review"].includes(task.status)
        || task.followUpAllowed !== undefined && typeof task.followUpAllowed !== "boolean"
        || task.turnKey !== undefined && typeof task.turnKey !== "string"
        || task.updatedAt !== undefined && !Number.isFinite(task.updatedAt) || task.sortAt !== undefined && !Number.isFinite(task.sortAt)
        || task.detail !== undefined && typeof task.detail !== "string" || task.actions !== undefined && (!Array.isArray(task.actions) || task.actions.some(action => !action || typeof action.id !== "string" || typeof action.label !== "string"
          || action.openInMainWindow !== undefined && typeof action.openInMainWindow !== "boolean")))) throw new Error("Pet activity requires real task identities, states and request actions.");
      activityOwners.set(ownerId, next); return publishOwners();
    },
    releaseActivity(ownerId) { activityOwners.delete(ownerId); return publishOwners(); },
    dispose() { disposed = true; if (expiryTimer !== null) clearTimeout(expiryTimer); ipcMain.removeHandler(MINI_GET); ipcMain.removeHandler(MINI_SPRITE); ipcMain.removeHandler(MINI_ACTION); if (miniWindow && !miniWindow.isDestroyed()) miniWindow.destroy(); },
  };
}

module.exports = { createPetManager, BUILTIN_PETS, imageDimensions, readCustomPet };

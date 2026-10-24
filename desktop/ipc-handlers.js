"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { ipcMain, dialog, shell, app } = require("electron");
const { NATIVE_MENU_KEYS } = require("./menu-popup");
const { openInTarget } = require("./open-targets");
const windowAppearance = require("./window-appearance");
const nativeFileExtensions = require("./native-open-file-extensions.json");
const notificationSounds = require("./notification-sounds");
const systemSettings = require("./system-settings");
const systemFonts = require("./system-fonts");

const {
  isHttpUrl,
  isProbablyTextBuffer,
  hashFileContent,
  withFileMutationQueue,
  countDirEntries,
  isSamePath,
} = require("./utils");

const {
  assertIpcCapability,
  assertReadablePath,
  assertTrustedPath,
  assertMutableTrustedPath,
  isWithinTrustedWorkspace,
  isReadablePath,
  rememberTrustedWorkspaceRoot,
  restoreTrustedWorkspaceRoot,
} = require("./security");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let ipcHandlersRegistered = false;

// Dependencies injected via init()
let ctx = {};

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  ctx = deps || {};
}

// ---------------------------------------------------------------------------
// Workspace search (internal helpers)
// ---------------------------------------------------------------------------

const MAX_WORKSPACE_SEARCH_DEPTH =
  Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_DEPTH) > 0
    ? Math.floor(Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_DEPTH))
    : 12;
const MAX_WORKSPACE_SEARCH_ENTRIES =
  Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_ENTRIES) > 0
    ? Math.floor(Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_ENTRIES))
    : 50_000;
const MAX_WORKSPACE_SEARCH_MS =
  Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_MS) > 0
    ? Math.floor(Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_MS))
    : 1_500;

const WORKSPACE_SEARCH_IGNORED_DIRS = new Set([
  ".git", ".idea", ".vscode", ".venv", "venv",
  "node_modules", "__pycache__", "dist", "build",
]);

function scoreWorkspaceSearchMatch(relativePath, query) {
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const normalizedPath = relativePath.replace(/\\/g, "/").toLowerCase();
  if (!normalizedQuery) {
    return { score: 1, matched_indices: [] };
  }

  let queryIndex = 0;
  const matchedIndices = [];
  let score = 0;
  for (let pathIndex = 0; pathIndex < normalizedPath.length && queryIndex < normalizedQuery.length; pathIndex += 1) {
    if (normalizedPath[pathIndex] !== normalizedQuery[queryIndex]) {
      continue;
    }

    matchedIndices.push(pathIndex);
    score += 1;
    if (pathIndex === 0 || ["/", "\\", "-", "_", "."].includes(relativePath[pathIndex - 1])) {
      score += 10;
    }
    if (matchedIndices.length > 1 && matchedIndices[matchedIndices.length - 2] === pathIndex - 1) {
      score += 5;
    }
    queryIndex += 1;
  }

  if (queryIndex < normalizedQuery.length) {
    return null;
  }

  const fileName = path.basename(relativePath).toLowerCase();
  if (fileName.includes(normalizedQuery)) {
    score += 50;
  }

  return { score, matched_indices: matchedIndices };
}

async function searchWorkspaceFiles(rootPath, query, limit = 20, kind = "file") {
  const workspaceRoot = assertTrustedPath(rootPath, "Workspace root");
  if (!fs.existsSync(workspaceRoot)) {
    throw new Error(`Directory not found: ${rootPath}`);
  }

  const maxResults = Math.max(1, Math.min(Number(limit) || 20, 100));
  const searchKind = ["file", "folder", "all"].includes(kind) ? kind : "file";
  const results = [];
  const startedAt = Date.now();
  let visitedEntries = 0;
  let truncated = false;

  async function visitDirectory(directoryPath, depth = 0) {
    if (truncated || depth > MAX_WORKSPACE_SEARCH_DEPTH) {
      return;
    }
    const dirents = await fs.promises.readdir(directoryPath, { withFileTypes: true });
    for (const dirent of dirents) {
      visitedEntries += 1;
      if (visitedEntries > MAX_WORKSPACE_SEARCH_ENTRIES || Date.now() - startedAt > MAX_WORKSPACE_SEARCH_MS) {
        truncated = true;
        break;
      }
      if (dirent.name.startsWith(".") || WORKSPACE_SEARCH_IGNORED_DIRS.has(dirent.name)) {
        continue;
      }
      if (dirent.isSymbolicLink()) {
        continue;
      }

      const fullPath = path.join(directoryPath, dirent.name);
      if (!isWithinTrustedWorkspace(fullPath)) {
        continue;
      }
      if (dirent.isDirectory()) {
        if (searchKind === "folder" || searchKind === "all") {
          const relativePath = path.relative(workspaceRoot, fullPath).replace(/\\/g, "/");
          const match = scoreWorkspaceSearchMatch(relativePath, query);
          if (match) {
            results.push({
              path: relativePath,
              name: dirent.name,
              score: match.score,
              matched_indices: match.matched_indices,
              kind: "folder",
            });
          }
        }
        await visitDirectory(fullPath, depth + 1);
        if (truncated) break;
        continue;
      }

      if (!dirent.isFile() || searchKind === "folder") {
        continue;
      }

      const relativePath = path.relative(workspaceRoot, fullPath).replace(/\\/g, "/");
      const match = scoreWorkspaceSearchMatch(relativePath, query);
      if (!match) {
        continue;
      }

      results.push({
        path: relativePath,
        name: dirent.name,
        score: match.score,
        matched_indices: match.matched_indices,
        kind: "file",
      });
    }
  }

  await visitDirectory(workspaceRoot);
  results.sort((a, b) => b.score - a.score || a.path.localeCompare(b.path));

  return {
    query: String(query || ""),
    results: results.slice(0, maxResults),
    truncated,
    visited_entries: visitedEntries,
  };
}

// ---------------------------------------------------------------------------
// Environment detection
// ---------------------------------------------------------------------------

function checkCommand(command) {
  return new Promise((resolve) => {
    const executable = typeof command === "string" ? command.trim() : "";
    if (!executable || /[\\/\s]/.test(executable)) {
      resolve(false);
      return;
    }
    execFile(executable, ["--version"], { windowsHide: true }, (error) => {
      resolve(!error);
    });
  });
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

function registerIpcHandlers() {
  if (ipcHandlersRegistered) return;
  ipcHandlersRegistered = true;

  const {
    setupWindowsSandbox,
    popupApplicationMenu,
    setApplicationMenuState,
    executeApplicationMenuAction,
    getWindowForSender,
    getWindowRecord,
    showDesktopNotification,
    hideDesktopNotifications,
    dispatchDeepLink,
    attemptAppStartup,
    getDesktopLogPath,
    appendDesktopLog,
    exportDesktopDiagnostics,
    discoverChromeCdp,
    assessBrowserNavigationPolicy,
    captureChromeTargetScreenshot,
    navigateChromeTarget,
    clickChromeTarget,
    typeIntoChromeTarget,
    ptyManager,
    getLastPickedWorkspaceRoot,
    setLastPickedWorkspaceRoot,
  } = ctx;

  const getWin = (event) => getWindowForSender(event.sender);
  const getWindowInstanceId = (event) => getWindowRecord(getWin(event)).windowInstanceId;
  const browserPayload = (event, payload) => ({
    ...payload,
    window_instance_id: getWindowInstanceId(event),
  });
  const getStartupWin = () => {
    const fn = typeof ctx.getStartupFailureWindow === "function"
      ? ctx.getStartupFailureWindow
      : () => null;
    return fn();
  };

  const senderUrl = (event) => {
    if (event?.senderFrame?.url) {
      return event.senderFrame.url;
    }
    if (event?.sender && typeof event.sender.getURL === "function") {
      return event.sender.getURL();
    }
    return "";
  };

  const assertWindowSender = (event, win, channel) => {
    const webContents = win && !win.isDestroyed() ? win.webContents : null;
    const actualUrl = senderUrl(event);
    // Preload runs before navigation commits, when both URL snapshots can be
    // empty. Authenticate the owned window and its main frame; navigation to
    // other origins is restricted by window-manager, not by comparing a frame
    // URL to another snapshot of that same frame's URL.
    const isTrusted =
      Boolean(webContents) &&
      event?.sender === webContents &&
      event?.senderFrame === webContents.mainFrame;
    if (isTrusted) {
      return;
    }

    if (typeof appendDesktopLog === "function") {
      appendDesktopLog(`[desktop] blocked IPC ${channel} from ${actualUrl || "unknown sender"}`);
    }
    const error = new Error(`Untrusted IPC sender for ${channel}`);
    error.code = "ERR_UNTRUSTED_IPC_SENDER";
    throw error;
  };

  const assertMainSender = (event, channel) => {
    assertWindowSender(event, getWin(event), channel);
  };

  const assertStartupSender = (event, channel) => {
    assertWindowSender(event, getStartupWin(), channel);
  };

  const withMainSender = (channel, handler) => (event, ...args) => {
    assertMainSender(event, channel);
    assertIpcCapability(channel, { logger: appendDesktopLog });
    const record = getWindowRecord(getWin(event));
    if (record.remoteHostId) {
      const remote = ctx.getRemoteDesktop();
      if (channel === "minicode:sandbox:setup") return { ok: false, error: "请在远端 Windows 主机上初始化沙箱。" };
      if (remote.invokes(channel)) return remote.invoke(record, channel, args);
      if (["minicode:embeddedBrowser:create", "minicode:embeddedBrowser:navigate"].includes(channel)) {
        return remote.mapUrl(record, args[0].url).then((url) => handler(event, { ...args[0], url }));
      }
      if (["minicode:pickDirectory", "minicode:workspace:pickDirectory"].includes(channel)) {
        throw new Error("Choose a directory on the connected host in the remote folder selector");
      }
    }
    return handler(event, ...args);
  };

  const withStartupSender = (channel, handler) => (event, ...args) => {
    assertStartupSender(event, channel);
    assertIpcCapability(channel, { logger: appendDesktopLog });
    return handler(event, ...args);
  };

  // -----------------------------------------------------------------------
  // Window controls
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:personalizations:listHosts", withMainSender("minicode:personalizations:listHosts", () =>
    ctx.getRemoteDesktop().personalizationHosts()));
  ipcMain.handle("minicode:personalizations:request", withMainSender("minicode:personalizations:request", (_event, hostId, action, payload) =>
    ctx.getRemoteDesktop().personalizationRequest(hostId, action, payload, ctx.getBackendRuntimeConfig())));

  for (const action of ["claim", "update", "release", "ready", "reply"]) {
    const channel = `minicode:realtimeVoice:${action}`;
    ipcMain.handle(channel, withMainSender(channel, (event, payload) =>
      ctx.realtimeVoiceManager[action](getWindowRecord(getWin(event)), payload)));
  }

  ipcMain.handle("minicode:pets:getState", withMainSender("minicode:pets:getState", async () => (await ctx.getPetManager()).getState()));
  ipcMain.handle("minicode:pets:refresh", withMainSender("minicode:pets:refresh", () => ctx.refreshPets()));
  ipcMain.handle("minicode:pets:update", withMainSender("minicode:pets:update", async (_event, patch) => (await ctx.getPetManager()).update(patch)));
  ipcMain.handle("minicode:pets:toggleMini", withMainSender("minicode:pets:toggleMini", async () => (await ctx.getPetManager()).toggleMini()));
  ipcMain.handle("minicode:pets:setNotificationMuted", withMainSender("minicode:pets:setNotificationMuted", async (_event, request) => (await ctx.getPetManager()).setNotificationMuted(request)));
  ipcMain.handle("minicode:pets:openFolder", withMainSender("minicode:pets:openFolder", async () => (await ctx.getPetManager()).openFolder()));
  ipcMain.handle("minicode:pets:getSprite", withMainSender("minicode:pets:getSprite", async (_event, id) => (await ctx.getPetManager()).getSprite(id)));
  ipcMain.handle("minicode:pets:publishActivity", withMainSender("minicode:pets:publishActivity", async (event, activity) => {
    const owner = ctx.getWindowRecord(ctx.getWindowForSender(event.sender)).windowInstanceId;
    return (await ctx.getPetManager()).publishActivity(activity, owner);
  }));
  ipcMain.handle("minicode:pets:completeAction", withMainSender("minicode:pets:completeAction", (event, result) => {
    const owner = ctx.getWindowRecord(ctx.getWindowForSender(event.sender)).windowInstanceId;
    return ctx.completePetAction(owner, result);
  }));

  ipcMain.on("minicode:runtime:get", withMainSender("minicode:runtime:get", (event) => {
    const win = getWin(event);
    const record = getWindowRecord(win);
    event.returnValue = { ...ctx.getRuntimeConfig(win), initialNavigation: record.initialNavigation };
  }));

  ipcMain.on("minicode:window:initialNavigation:consume", withMainSender("minicode:window:initialNavigation:consume", event => {
    getWindowRecord(getWin(event)).initialNavigation = null;
    event.returnValue = true;
  }));

  ipcMain.handle("minicode:window:openConversation", withMainSender("minicode:window:openConversation", async (_event, conversationId) => {
    const source = getWindowRecord(getWin(_event));
    const win = await ctx.createNewWindow({ conversationId,
      ...(source.remoteHostId ? { remoteHostId: source.remoteHostId, runtime: source.runtime } : {}) });
    ctx.focusMainWindow(win);
    const record = getWindowRecord(win);
    return { windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId };
  }));

  for (const action of ["list", "discover", "save", "saveMany", "connect", "disconnect", "remove", "open"]) {
    const channel = `minicode:connections:${action}`;
    ipcMain.handle(channel, withMainSender(channel, async (_event, ...args) => {
      try {
        const value = await ctx.getRemoteDesktop()[action](...args);
        return { ok: true, value: ["connect", "disconnect", "open"].includes(action) ? undefined : value };
      } catch (error) {
        appendDesktopLog(`[desktop] ${channel}: ${error.message}`);
        return { ok: false, error: error.message };
      }
    }));
  }

  ipcMain.on("minicode:window:unload:capture", withMainSender("minicode:window:unload:capture", (event) => {
    event.returnValue = ctx.captureUnloadIntent(getWin(event));
  }));
  ipcMain.handle("minicode:window:unload:resume", withMainSender("minicode:window:unload:resume", (event, intentId) => {
    return ctx.resumeUnload(getWin(event), intentId);
  }));

  ipcMain.handle("minicode:window:minimize", withMainSender("minicode:window:minimize", (event) => {
    const win = getWin(event);
    if (!win || win.isDestroyed()) return false;
    win.minimize();
    return true;
  }));
  ipcMain.handle("minicode:window:state", withMainSender("minicode:window:state", event => ({ isMaximized: getWin(event).isMaximized(), isFocused: getWin(event).isFocused() })));
  ipcMain.handle("minicode:window:appearance:get", withMainSender("minicode:window:appearance:get", event => windowAppearance.get(getWin(event))));
  ipcMain.handle("minicode:window:fontFamilies", withMainSender("minicode:window:fontFamilies", () => systemFonts.listFontFamilies()));
  ipcMain.handle("minicode:window:fonts", withMainSender("minicode:window:fonts", () => new Promise((resolve, reject) => {
    execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command",
      "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Add-Type -AssemblyName System.Drawing; $fonts=[Drawing.Text.InstalledFontCollection]::new(); ConvertTo-Json -Compress -InputObject @($fonts.Families.Name | Sort-Object -Unique)"],
    { windowsHide: true, encoding: "utf8" }, (error, stdout) => {
      if (error) reject(error);
      else resolve(JSON.parse(stdout));
    });
  })));
  ipcMain.handle("minicode:window:appearance:set", withMainSender("minicode:window:appearance:set", (event, options) => windowAppearance.set(getWin(event), options)));

  ipcMain.handle("minicode:window:maximize", withMainSender("minicode:window:maximize", (event) => {
    const win = getWin(event);
    if (!win || win.isDestroyed()) return false;
    if (win.isMaximized()) {
      win.unmaximize();
    } else {
      win.maximize();
    }
    return true;
  }));

  ipcMain.handle("minicode:window:close", withMainSender("minicode:window:close", (event) => {
    const win = getWin(event);
    if (!win || win.isDestroyed()) return false;
    return ctx.requestCloseWindow(win, "button");
  }));

  // -----------------------------------------------------------------------
  // Notifications & deep links
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:notify", withMainSender("minicode:notify", (_event, payload) => {
    return typeof showDesktopNotification === "function"
      ? showDesktopNotification(payload, getWin(_event))
      : false;
  }));
  ipcMain.handle("minicode:notifications:hide", withMainSender("minicode:notifications:hide", (_event, selector) => {
    return hideDesktopNotifications(selector, getWin(_event));
  }));
  const preferencesSnapshot = async () => {
    const fileOpenTargets = await Promise.all(ctx.desktopPreferences.fileOpenTargets().map(async ({ id, label, icon, iconPath, kind, hidden }) => {
      const nativeIcon = iconPath ? await app.getFileIcon(iconPath, { size: "small" }) : null;
      return { id, label, kind, hidden, icon: nativeIcon && !nativeIcon.isEmpty() ? nativeIcon.toDataURL() : icon };
    }));
    return { ...ctx.desktopPreferences.getPreferences(), systemLocale: app.getLocale(), fileOpenTargets,
      terminalShellTargets: ctx.desktopPreferences.terminalShellTargets().map(({ id, label }) => ({ id, label })) };
  };
  ipcMain.handle("minicode:preferences:get", withMainSender("minicode:preferences:get", () => preferencesSnapshot()));
  ipcMain.handle("minicode:dictation:get", withMainSender("minicode:dictation:get", () => ctx.dictationHotkeys.get()));
  ipcMain.handle("minicode:dictation:getLocalRuntime", withMainSender("minicode:dictation:getLocalRuntime", () => {
    const { apiBaseUrl, runtimeToken, revision } = ctx.getBackendRuntimeConfig();
    return { apiBaseUrl, runtimeToken, revision };
  }));
  ipcMain.handle("minicode:dictation:openProviderSettings", withMainSender("minicode:dictation:openProviderSettings", async event => {
    const source = getWin(event);
    let target = !getWindowRecord(source).remoteHostId ? source
      : ctx.getMainWindows().find(window => !getWindowRecord(window).remoteHostId);
    if (target) {
      getWindowRecord(target).pendingProviderSettings = true;
      target.webContents.send("minicode:menu:settings", { tab: "provider" });
    }
    else target = await ctx.createNewWindow({ settingsTab: "provider" });
    ctx.focusMainWindow(target);
    return true;
  }));
  ipcMain.handle("minicode:dictation:consumeProviderSettings", withMainSender("minicode:dictation:consumeProviderSettings", event => {
    const record = getWindowRecord(getWin(event));
    const pending = record.pendingProviderSettings;
    record.pendingProviderSettings = false;
    return pending;
  }));
  ipcMain.handle("minicode:dictation:migratePreferences", withMainSender("minicode:dictation:migratePreferences", async (event, legacy) => {
    const record = getWindowRecord(getWin(event));
    ctx.desktopPreferences.migrateDictationPreferences(record.remoteHostId ? `remote:${record.remoteHostId}` : "local", legacy);
    const snapshot = await preferencesSnapshot();
    for (const window of ctx.getMainWindows()) window.webContents.send("minicode:preferences:changed", snapshot);
    return snapshot;
  }));
  ipcMain.handle("minicode:dictation:configure", withMainSender("minicode:dictation:configure", (_event, patch) => {
    if (Object.keys(patch).some(key => !["holdHotkey", "toggleHotkey"].includes(key)) || Object.values(patch).some(value => typeof value !== "string")) throw new Error("Invalid dictation shortcut configuration");
    return ctx.dictationHotkeys.configure(patch);
  }));
  ipcMain.handle("minicode:dictation:suspend", withMainSender("minicode:dictation:suspend", (event, value) => ctx.dictationHotkeys.suspend(event.sender.id, value === true)));
  ipcMain.handle("minicode:dictation:insert", withMainSender("minicode:dictation:insert", (event, token, text) => ctx.dictationHotkeys.insert(event.sender.id, token, text)));
  ipcMain.handle("minicode:dictation:complete", withMainSender("minicode:dictation:complete", (event, token) => ctx.dictationHotkeys.complete(event.sender.id, token)));
  ipcMain.handle("minicode:preferences:getStartupStatus", withMainSender("minicode:preferences:getStartupStatus", () => systemSettings.getStartupStatus(app)));
  ipcMain.handle("minicode:preferences:openStartupSettings", withMainSender("minicode:preferences:openStartupSettings", () => systemSettings.openStartupSettings(shell)));
  ipcMain.handle("minicode:preferences:getOpenSourceLicenses", withMainSender("minicode:preferences:getOpenSourceLicenses", () => systemSettings.getOpenSourceLicenses(app)));
  ipcMain.handle("minicode:preferences:pickNotificationSound", withMainSender("minicode:preferences:pickNotificationSound", event => notificationSounds.pickSound(dialog, getWin(event))));
  ipcMain.handle("minicode:preferences:previewNotificationSound", withMainSender("minicode:preferences:previewNotificationSound", (_event, sound) => {
    if (!notificationSounds.isNotificationSound(sound)) throw new TypeError("Unsupported notification sound.");
    return notificationSounds.playSound(sound, app.getPath("userData"), { restart: true });
  }));
  ipcMain.handle("minicode:preferences:importNotificationSound", withMainSender("minicode:preferences:importNotificationSound", async (_event, payload) => {
    const sound = notificationSounds.importSound(payload, app.getPath("userData"));
    ctx.desktopPreferences.setPreferences({ notificationSound: sound });
    const snapshot = await preferencesSnapshot();
    for (const window of ctx.getMainWindows()) window.webContents.send("minicode:preferences:changed", snapshot);
    return snapshot;
  }));
  ipcMain.handle("minicode:preferences:set", withMainSender("minicode:preferences:set", async (_event, patch) => {
    if ("popoutWindowShortcut" in patch) ctx.popoutWindow.update(patch);
    else ctx.desktopPreferences.setPreferences(patch);
    if ("localeOverride" in patch) ctx.onLocaleChanged();
    const snapshot = await preferencesSnapshot();
    for (const window of ctx.getMainWindows()) window.webContents.send("minicode:preferences:changed", snapshot);
    return snapshot;
  }));
  ipcMain.handle("minicode:preferences:chooseTaskDirectory", withMainSender("minicode:preferences:chooseTaskDirectory", async (event) => {
    const result = await dialog.showOpenDialog(getWin(event), { title: "无项目任务文件夹", properties: ["openDirectory", "createDirectory"], defaultPath: ctx.desktopPreferences.getPreferences().defaultTaskDirectory });
    if (result.canceled) return null;
    const selected = fs.realpathSync(result.filePaths[0]);
    ctx.desktopPreferences.setPreferences({ defaultTaskDirectory: selected });
    const snapshot = await preferencesSnapshot();
    for (const window of ctx.getMainWindows()) window.webContents.send("minicode:preferences:changed", snapshot);
    return snapshot;
  }));
  ipcMain.handle("minicode:browser:history", withMainSender("minicode:browser:history", (_event, query) => ctx.browserHistory.search(query)));
  ipcMain.handle("minicode:browser:removeHistory", withMainSender("minicode:browser:removeHistory", (_event, ids) => ctx.browserHistory.remove(ids)));
  ipcMain.handle("minicode:browserProfileImport:list", withMainSender("minicode:browserProfileImport:list", () => ctx.browserProfileImport.listImportableBrowserProfiles()));
  ipcMain.handle("minicode:browserProfileImport:import", withMainSender("minicode:browserProfileImport:import", (_event, request) => ctx.browserProfileImport.importBrowserProfile(request)));
  for (const method of ["listPasswords", "getPassword", "savePassword", "removePassword", "listContacts", "saveContact", "removeContact", "setAutofillSettings"]) {
    const channel = `minicode:browserPersonalData:${method}`;
    ipcMain.handle(channel, withMainSender(channel, (_event, request) => ctx.browserPersonalData[method](request)));
  }
  ipcMain.handle("minicode:embeddedBrowser:listSitePermissions", withMainSender("minicode:embeddedBrowser:listSitePermissions", () => ctx.embeddedBrowserManager.listSitePermissions()));
  ipcMain.handle("minicode:embeddedBrowser:getSitePolicy", withMainSender("minicode:embeddedBrowser:getSitePolicy", () => ctx.embeddedBrowserManager.getSitePolicy()));
  ipcMain.handle("minicode:embeddedBrowser:setSitePolicy", withMainSender("minicode:embeddedBrowser:setSitePolicy", (_event, request) => ctx.embeddedBrowserManager.setSitePolicy(request)));
  ipcMain.handle("minicode:embeddedBrowser:removeSiteDevice", withMainSender("minicode:embeddedBrowser:removeSiteDevice", (_event, request) => ctx.embeddedBrowserManager.removeSiteDevice(request)));
  ipcMain.on("minicode:browserPassword:submitted", (event, request) => { void ctx.embeddedBrowserManager.handlePasswordSubmission(event, request); });
  ipcMain.on("minicode:browserLink:click", (event, request) => { event.returnValue = ctx.embeddedBrowserManager.handleLinkClick(event, request); });
  ipcMain.handle("minicode:embeddedBrowser:removeSitePermissions", withMainSender("minicode:embeddedBrowser:removeSitePermissions", (_event, request) => ctx.embeddedBrowserManager.removeSitePermissions(request)));
  ipcMain.handle("minicode:mcpApp:prepare", withMainSender("minicode:mcpApp:prepare", (event, frameUrl) => {
    return ctx.getMcpAppSandbox().prepare(event.sender.id, frameUrl, ctx.getRuntimeConfig(getWin(event)).apiBaseUrl);
  }));
  ipcMain.handle("minicode:mcpApp:release", withMainSender("minicode:mcpApp:release", (event, id) => ctx.getMcpAppSandbox().release(event.sender.id, id)));
  ipcMain.handle("minicode:mcpApp:clearData", withMainSender("minicode:mcpApp:clearData", () => ctx.getMcpAppSandbox().clearData()));
  ipcMain.handle("minicode:embeddedBrowser:clearBrowsingData", withMainSender("minicode:embeddedBrowser:clearBrowsingData", (_event, dataTypes) => ctx.embeddedBrowserManager.clearBrowsingData(dataTypes)));
  ipcMain.handle("minicode:embeddedBrowser:capture", withMainSender("minicode:embeddedBrowser:capture", (event, payload) => ctx.embeddedBrowserManager.capture(browserPayload(event, payload))));
  ipcMain.handle("minicode:openPreferredPath", withMainSender("minicode:openPreferredPath", async (_event, target, line, options = {}) => {
    const resolved = assertReadablePath(target, "File open target");
    const workspaceRoot = options.workspaceRoot ? assertReadablePath(options.workspaceRoot, "File open workspace") : undefined;
    let targetId = options.targetId ?? ctx.desktopPreferences.preferredFileOpenTarget(workspaceRoot);
    const persistPreferredTarget = async () => {
      const preferredPath = assertReadablePath(options.persistPreferredTargetPath, "Preferred file open workspace");
      ctx.desktopPreferences.setPreferredFileOpenTarget(preferredPath, targetId);
      const snapshot = await preferencesSnapshot();
      for (const window of ctx.getMainWindows()) window.webContents.send("minicode:preferences:changed", snapshot);
    };
    if (targetId === "minicode") {
      if (options.persistPreferredTargetPath) await persistPreferredTarget();
      return { openInEditor: true, path: resolved };
    }
    if (options.targetId == null) {
      if (fs.statSync(resolved).isDirectory()) targetId = "fileManager";
      else if (nativeFileExtensions.includes(path.extname(resolved).slice(1).toLowerCase())) targetId = "system";
    }
    const opener = ctx.desktopPreferences.fileOpenTargets().find(item => item.id === targetId);
    if (!opener) throw new Error("The selected file application is no longer installed.");
    if (options.persistPreferredTargetPath && opener.kind === "editor") await persistPreferredTarget();
    await openInTarget(opener, resolved, line, options.column);
    return { openInEditor: false, path: resolved };
  }));

  ipcMain.handle("minicode:saveFile", withMainSender("minicode:saveFile", async (event, filename, bytes) => {
    if (typeof filename !== "string" || !filename.trim() || !(bytes instanceof Uint8Array)) {
      throw new Error("Saving a file requires a filename and its original bytes.");
    }
    const result = await dialog.showSaveDialog(getWin(event), {
      defaultPath: path.join(app.getPath("downloads"), path.basename(filename)),
    });
    if (result.canceled) return false;
    await fs.promises.writeFile(result.filePath, bytes);
    return true;
  }));

  ipcMain.handle("minicode:pickDirectory", withMainSender("minicode:pickDirectory", async (event) => {
    const owner = getWin(event);
    const result = await dialog.showOpenDialog(owner, {
      properties: ["openDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return "";
    }
    return path.resolve(result.filePaths[0]);
  }));

  ipcMain.handle("minicode:workspace:pickDirectory", withMainSender("minicode:workspace:pickDirectory", async (event) => {
    const owner = getWin(event);
    const result = await dialog.showOpenDialog(owner, {
      title: "新建或选择项目文件夹",
      buttonLabel: "选择项目",
      properties: ["openDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return "";
    }
    const selected = path.resolve(result.filePaths[0]);
    if (typeof setLastPickedWorkspaceRoot === "function") {
      setLastPickedWorkspaceRoot(selected);
    }
    return rememberTrustedWorkspaceRoot(selected);
  }));

  ipcMain.handle("minicode:workspace:directoryEntries", withMainSender("minicode:workspace:directoryEntries", () => {
    throw new Error("Directory browsing requires a connected remote host");
  }));

  ipcMain.handle("minicode:attachments:importRemoteFile", withMainSender("minicode:attachments:importRemoteFile", () => {
    throw new Error("Remote attachments require a connected remote host");
  }));

  ipcMain.handle("minicode:workspace:trust", withMainSender("minicode:workspace:trust", (_event, targetPath) => {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return "";
    }
    const resolved = path.resolve(targetPath);
    const lastPicked = typeof getLastPickedWorkspaceRoot === "function" ? getLastPickedWorkspaceRoot() : "";
    const cameFromNativePicker = isSamePath(resolved, lastPicked);
    const alreadyInsideTrustedRoot = isWithinTrustedWorkspace(resolved);
    if (!cameFromNativePicker && !alreadyInsideTrustedRoot) {
      return restoreTrustedWorkspaceRoot(resolved);
    }
    return rememberTrustedWorkspaceRoot(resolved);
  }));

  ipcMain.handle("minicode:openExternal", withMainSender("minicode:openExternal", async (_event, target) => {
    if (typeof target !== "string" || !target.trim() || !isHttpUrl(target)) {
      return false;
    }
    await shell.openExternal(target.trim());
    return true;
  }));

  ipcMain.handle("minicode:openPath", withMainSender("minicode:openPath", async (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    try {
      const resolved = path.resolve(target.trim());
      if (!isReadablePath(resolved)) {
        throw new Error("Path is outside trusted or approved user-output locations.");
      }
      const error = await shell.openPath(resolved);
      if (error) throw new Error(error);
      return true;
    } catch (error) {
      if (typeof appendDesktopLog === "function") {
        appendDesktopLog(`[desktop] openPath denied: ${error.message}`);
      }
      return false;
    }
  }));

  ipcMain.handle("minicode:revealPath", withMainSender("minicode:revealPath", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    try {
      const resolved = path.resolve(target.trim());
      if (!isReadablePath(resolved)) {
        throw new Error("Path is outside trusted or approved user-output locations.");
      }
      shell.showItemInFolder(resolved);
      return true;
    } catch (error) {
      if (typeof appendDesktopLog === "function") {
        appendDesktopLog(`[desktop] revealPath denied: ${error.message}`);
      }
      return false;
    }
  }));

  ipcMain.handle("minicode:settings:openTarget", withMainSender("minicode:settings:openTarget", async (event, request) => {
    if (!request || !["config", "hook", "data", "import-item"].includes(request.kind)
      || (request.workspaceRoot !== undefined && typeof request.workspaceRoot !== "string")
      || (request.scopeId !== undefined && (request.kind !== "config" || typeof request.scopeId !== "string"))
      || (request.sessionId !== undefined && typeof request.sessionId !== "string")
      || (request.conversationId !== undefined && typeof request.conversationId !== "string")
      || (request.kind === "import-item" && (typeof request.importId !== "string" || typeof request.itemId !== "string"))
      || (request.kind === "hook" && typeof request.hookKey !== "string")) {
      throw new TypeError("A registered settings action is required.");
    }
    const window = getWin(event);
    if (getWindowRecord(window).remoteHostId) {
      throw new Error("Remote settings paths cannot be opened by the local file manager.");
    }
    const runtime = ctx.getRuntimeConfig(window);
    const url = new URL("/api/settings/open-target", runtime.apiBaseUrl);
    url.searchParams.set("kind", request.kind);
    url.searchParams.set("workspace_root", request.workspaceRoot ?? "");
    if (request.kind === "config" && request.scopeId !== undefined) url.searchParams.set("scope_id", request.scopeId);
    if (request.kind === "import-item") {
      url.searchParams.set("import_id", request.importId);
      url.searchParams.set("item_id", request.itemId);
    }
    if (["config", "import-item"].includes(request.kind) && request.sessionId !== undefined) url.searchParams.set("session_id", request.sessionId);
    if (["config", "import-item"].includes(request.kind) && request.conversationId !== undefined) url.searchParams.set("conversation_id", request.conversationId);
    if (request.kind === "hook") url.searchParams.set("hook_key", request.hookKey);
    const response = await fetch(url, { headers: { "X-MiniCode-Token": runtime.runtimeToken }, cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail);
    const target = await response.json();
    if (typeof target.path !== "string" || !path.isAbsolute(target.path)) {
      throw new Error("The settings service did not return an absolute registered path.");
    }
    if (target.workspace_root !== undefined) {
      const targetId = ctx.desktopPreferences.preferredFileOpenTarget(target.workspace_root || undefined);
      if (targetId === "minicode") return { openInEditor: true, path: target.path, workspaceRoot: target.workspace_root };
      const opener = ctx.desktopPreferences.fileOpenTargets().find(item => item.id === targetId);
      if (!opener) throw new Error("The selected file application is no longer installed.");
      await openInTarget(opener, target.path);
      return true;
    }
    const error = await shell.openPath(target.path);
    if (error) throw new Error(error);
    return true;
  }));

  ipcMain.handle("minicode:skills:reveal", withMainSender("minicode:skills:reveal", async (event, request) => {
    if (!request || typeof request.path !== "string" || !path.isAbsolute(request.path)
      || typeof request.workspaceRoot !== "string") {
      throw new TypeError("A Skill path and its workspace owner are required.");
    }
    const window = getWin(event);
    if (getWindowRecord(window).remoteHostId) {
      throw new Error("Skill files on a remote host cannot be revealed in the local file manager.");
    }
    const runtime = ctx.getRuntimeConfig(window);
    const url = new URL("/api/skills/detail", runtime.apiBaseUrl);
    url.searchParams.set("skill_path", request.path);
    url.searchParams.set("workspace_root", request.workspaceRoot);
    const response = await fetch(url, { headers: { "X-MiniCode-Token": runtime.runtimeToken }, cache: "no-store" });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail);
    }
    const skill = await response.json();
    if (typeof skill.path !== "string" || !path.isAbsolute(skill.path) || !isSamePath(skill.path, request.path)) {
      throw new Error("The Skill catalog did not return the requested file.");
    }
    shell.showItemInFolder(skill.path);
    return true;
  }));

  ipcMain.handle("minicode:deepLink:open", withMainSender("minicode:deepLink:open", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    return typeof dispatchDeepLink === "function" ? dispatchDeepLink(target, getWin(_event)) : false;
  }));

  // -----------------------------------------------------------------------
  // Startup / diagnostics
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:startup:getState", withStartupSender("minicode:startup:getState", () => ctx.startupFailureState || {}));
  ipcMain.handle("minicode:startup:retry", withStartupSender("minicode:startup:retry", async () => {
    return typeof attemptAppStartup === "function" ? attemptAppStartup("startup-retry") : false;
  }));
  ipcMain.handle("minicode:startup:quit", withStartupSender("minicode:startup:quit", () => {
    app.quit();
    return true;
  }));
  ipcMain.handle("minicode:startup:openLogs", withStartupSender("minicode:startup:openLogs", () => {
    const logPath = typeof getDesktopLogPath === "function" ? getDesktopLogPath() : "";
    if (typeof appendDesktopLog === "function") {
      appendDesktopLog("[desktop] open logs requested from startup failure surface");
    }
    shell.showItemInFolder(logPath);
    return true;
  }));
  ipcMain.handle("minicode:diagnostics:export", withMainSender("minicode:diagnostics:export", () => {
    return typeof exportDesktopDiagnostics === "function" ? exportDesktopDiagnostics() : {};
  }));

  // -----------------------------------------------------------------------
  // Browser / CDP
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:browser:discover", withMainSender("minicode:browser:discover", async (_event, endpoint) => {
    return typeof discoverChromeCdp === "function" ? discoverChromeCdp(endpoint) : { status: "error" };
  }));
  ipcMain.handle("minicode:browser:captureScreenshot", withMainSender("minicode:browser:captureScreenshot", async (_event, endpoint, targetId) => {
    return typeof captureChromeTargetScreenshot === "function" ? captureChromeTargetScreenshot(endpoint, targetId) : null;
  }));
  ipcMain.handle("minicode:browser:navigate", withMainSender("minicode:browser:navigate", async (event, endpoint, targetId, url) => {
    if (typeof navigateChromeTarget !== "function") {
      return null;
    }

    let targetUrl = url;
    let allowPrivateNetwork = false;
    if (typeof assessBrowserNavigationPolicy === "function") {
      const assessment = assessBrowserNavigationPolicy(url);
      targetUrl = assessment.url;
      if (assessment.requiresPrivateNetworkApproval) {
        const owner = getWin(event);
        const result = await dialog.showMessageBox(owner, {
          type: "warning",
          buttons: ["Cancel", "Open"],
          defaultId: 0,
          cancelId: 0,
          title: "Open local browser target?",
          message: "Chrome navigation is targeting a local or private network address.",
          detail: `${assessment.host} can expose services on this computer or private network. Continue only if you trust this target.`,
          noLink: true,
        });
        if (result.response !== 1) {
          throw new Error("Local or private browser navigation was cancelled.");
        }
        allowPrivateNetwork = true;
      }
    }

    return navigateChromeTarget(endpoint, targetId, targetUrl, { allowPrivateNetwork });
  }));
  ipcMain.handle("minicode:browser:click", withMainSender("minicode:browser:click", async (_event, endpoint, targetId, selector) => {
    return typeof clickChromeTarget === "function" ? clickChromeTarget(endpoint, targetId, selector) : null;
  }));
  ipcMain.handle("minicode:browser:type", withMainSender("minicode:browser:type", async (_event, endpoint, targetId, selector, text) => {
    return typeof typeIntoChromeTarget === "function" ? typeIntoChromeTarget(endpoint, targetId, selector, text) : null;
  }));

  const embeddedBrowser = ctx.embeddedBrowserManager;
  ipcMain.handle("minicode:embeddedBrowser:create", withMainSender("minicode:embeddedBrowser:create", async (_event, payload) => {
    return embeddedBrowser?.create(browserPayload(_event, payload));
  }));
  ipcMain.handle("minicode:embeddedBrowser:list", withMainSender("minicode:embeddedBrowser:list", (_event, payload) => {
    return embeddedBrowser?.listTargets(payload?.conversationId || payload?.conversation_id, getWindowInstanceId(_event)) ?? [];
  }));
  ipcMain.handle("minicode:embeddedBrowser:activate", withMainSender("minicode:embeddedBrowser:activate", (_event, payload) => {
    return embeddedBrowser?.activate(payload?.id, payload?.conversationId || payload?.conversation_id, getWindowInstanceId(_event)) ?? false;
  }));
  ipcMain.handle("minicode:embeddedBrowser:setBounds", withMainSender("minicode:embeddedBrowser:setBounds", (_event, payload) => {
    return embeddedBrowser?.setBounds(browserPayload(_event, payload)) ?? false;
  }));
  ipcMain.handle("minicode:embeddedBrowser:navigate", withMainSender("minicode:embeddedBrowser:navigate", async (_event, payload) => {
    return embeddedBrowser?.navigate(browserPayload(_event, payload));
  }));
  ipcMain.handle("minicode:embeddedBrowser:runAction", withMainSender("minicode:embeddedBrowser:runAction", (_event, payload) => {
    return embeddedBrowser?.runNavigationAction(browserPayload(_event, payload)) ?? false;
  }));
  ipcMain.handle("minicode:embeddedBrowser:pageCommand", withMainSender("minicode:embeddedBrowser:pageCommand", (event, payload) => embeddedBrowser.runPageCommand(browserPayload(event, payload))));
  ipcMain.handle("minicode:embeddedBrowser:downloads", withMainSender("minicode:embeddedBrowser:downloads", () => embeddedBrowser.listDownloads()));
  ipcMain.handle("minicode:embeddedBrowser:downloadAction", withMainSender("minicode:embeddedBrowser:downloadAction", (_event, payload) => embeddedBrowser.runDownloadAction(payload)));
  ipcMain.handle("minicode:embeddedBrowser:inspect", withMainSender("minicode:embeddedBrowser:inspect", async (_event, payload) => {
    const action = payload?.kind === "network"
      ? "get_network_logs"
      : payload?.kind === "element"
        ? "pick_element"
        : payload?.kind === "region"
          ? "pick_region"
        : "get_console_logs";
    return embeddedBrowser?.executeControlCommand({
      action,
      target_id: payload?.id,
      conversation_id: payload?.conversationId || payload?.conversation_id,
      window_instance_id: getWindowInstanceId(_event),
    }) ?? { ok: false, value: [] };
  }));
  ipcMain.handle("minicode:embeddedBrowser:getSettings", withMainSender("minicode:embeddedBrowser:getSettings", (_event, payload) => {
    return embeddedBrowser?.getBrowserSettings(payload?.url) ?? { downloadPolicy: "block", origin: "", permissions: [] };
  }));
  ipcMain.handle("minicode:embeddedBrowser:setSettings", withMainSender("minicode:embeddedBrowser:setSettings", (_event, payload) => {
    return embeddedBrowser?.setBrowserSettings(payload) ?? { downloadPolicy: "block", origin: "", permissions: [] };
  }));
  ipcMain.handle("minicode:embeddedBrowser:clearSiteData", withMainSender("minicode:embeddedBrowser:clearSiteData", async (_event, payload) => {
    return embeddedBrowser?.clearSiteData(browserPayload(_event, payload)) ?? false;
  }));
  ipcMain.handle("minicode:embeddedBrowser:close", withMainSender("minicode:embeddedBrowser:close", (_event, payload) => {
    return embeddedBrowser?.close(browserPayload(_event, payload)) ?? false;
  }));
  ipcMain.handle("minicode:embeddedBrowser:closeConversation", withMainSender("minicode:embeddedBrowser:closeConversation", (_event, conversationId) => {
    return embeddedBrowser?.closeConversation(conversationId, getWindowInstanceId(_event)) ?? 0;
  }));

  // -----------------------------------------------------------------------
  // Filesystem
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:fs:listTree", withMainSender("minicode:fs:listTree", async (_event, targetPath) => {
    let rootPath;
    try {
      rootPath = assertTrustedPath(targetPath, "Directory");
    } catch (error) {
      // A restored backend conversation can rebind the renderer to a previous
      // workspace before the main process has seen a native picker event. Only
      // allow this narrow recovery for an existing directory requested as a
      // tree root; child reads and every write still require an established
      // trusted root.
      rootPath = restoreTrustedWorkspaceRoot(targetPath);
      if (!rootPath) throw error;
      if (typeof appendDesktopLog === "function") {
        appendDesktopLog(`[desktop] restored trusted workspace root: ${rootPath}`);
      }
    }
    if (!fs.existsSync(rootPath)) {
      throw new Error(`Directory not found: ${targetPath}`);
    }
    const stat = await fs.promises.stat(rootPath);
    if (!stat.isDirectory()) {
      throw new Error(`Path is not a directory: ${targetPath}`);
    }

    const dirents = await fs.promises.readdir(rootPath, { withFileTypes: true });
    const entries = [];
    for (const dirent of dirents) {
      if (dirent.isSymbolicLink()) continue;
      const fullPath = path.join(rootPath, dirent.name);
      if (!isWithinTrustedWorkspace(fullPath)) continue;
      try {
        const childStat = await fs.promises.lstat(fullPath);
        entries.push({
          name: dirent.name,
          path: fullPath,
          is_dir: dirent.isDirectory(),
          size_bytes: dirent.isFile() ? childStat.size : null,
          modified_at: childStat.mtime.toISOString(),
        });
      } catch (e) {
        // Skip inaccessible files
      }
    }

    entries.sort((a, b) => {
      if (a.is_dir === b.is_dir) return a.name.localeCompare(b.name);
      return a.is_dir ? -1 : 1;
    });

    return {
      workspace_root: rootPath,
      requested_path: rootPath,
      entries: entries,
    };
  }));

  ipcMain.handle("minicode:fs:searchFiles", withMainSender("minicode:fs:searchFiles", async (_event, rootPath, query, limit, kind) => {
    return searchWorkspaceFiles(rootPath, query, limit, kind);
  }));

  ipcMain.handle("minicode:fs:readFile", withMainSender("minicode:fs:readFile", async (_event, targetPath) => {
    const fullPath = assertReadablePath(targetPath, "File");
    const stat = await fs.promises.stat(fullPath);
    const MAX_READ_SIZE = 2 * 1024 * 1024; // Keep desktop editor reads aligned with the backend workspace API.
    if (stat.size > MAX_READ_SIZE) {
      throw new Error(`File too large (${(stat.size / 1024 / 1024).toFixed(1)}MB). Max ${MAX_READ_SIZE / 1024 / 1024}MB.`);
    }
    const raw = await fs.promises.readFile(fullPath);
    if (!isProbablyTextBuffer(raw, fullPath)) {
      throw new Error("Only UTF-8 text files are supported.");
    }
    const content = raw.toString("utf8");
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      encoding: "utf-8",
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text",
      read_only: !isWithinTrustedWorkspace(fullPath),
    };
  }));

  ipcMain.handle("minicode:fs:deletePath", withMainSender("minicode:fs:deletePath", async (_event, targetPath, recursive, confirm) => {
    const fullPath = assertMutableTrustedPath(targetPath, "Path");
    return withFileMutationQueue(fullPath, async () => {
    const stat = await fs.promises.stat(fullPath);
    const isDir = stat.isDirectory();

    if (isDir && recursive && !confirm) {
      const count = await countDirEntries(fullPath, 51);
      if (count > 50) {
        return { needsConfirmation: true, path: fullPath, entryCount: count };
      }
    }

    await shell.trashItem(fullPath);

    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      deleted: true,
      is_dir: isDir,
    };
    });
  }));

  // -----------------------------------------------------------------------
  // PTY
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:pty:spawn", withMainSender("minicode:pty:spawn", (_event, cwd, conversationId) => {
    return ptyManager.spawnSession(cwd, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:write", withMainSender("minicode:pty:write", (_event, sessionId, data, conversationId) => {
    return ptyManager.writeToSession(sessionId, data, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:resize", withMainSender("minicode:pty:resize", (_event, sessionId, cols, rows, conversationId) => {
    ptyManager.resizeSession(sessionId, cols, rows, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:kill", withMainSender("minicode:pty:kill", (_event, sessionId, conversationId) => {
    return ptyManager.killSession(sessionId, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:restart", withMainSender("minicode:pty:restart", (_event, sessionId, conversationId) => {
    return ptyManager.restartSession(sessionId, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:killConversation", withMainSender("minicode:pty:killConversation", (_event, conversationId, preserveHistory) => {
    return ptyManager.killConversation(conversationId, preserveHistory === true, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:list", withMainSender("minicode:pty:list", (_event, conversationId) => {
    return ptyManager.listSessions(conversationId, undefined, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:snapshot", withMainSender("minicode:pty:snapshot", (_event, sessionId, maxChars, conversationId) => {
    return ptyManager.snapshotSession(sessionId, maxChars, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:pty:clear", withMainSender("minicode:pty:clear", (_event, sessionId, conversationId) => {
    return ptyManager.clearSession(sessionId, conversationId, getWindowInstanceId(_event));
  }));

  ipcMain.handle("minicode:deepLink:pending", withMainSender("minicode:deepLink:pending", (event) => ctx.getPendingDeepLink(getWin(event))));

  ipcMain.handle("minicode:deepLink:ack", withMainSender("minicode:deepLink:ack", (_event, id) => {
    return typeof ctx.acknowledgeDeepLink === "function" ? ctx.acknowledgeDeepLink(id, getWin(_event)) : false;
  }));

  ipcMain.handle("minicode:update:check", withMainSender("minicode:update:check", () => ctx.updater?.check?.() ?? false));
  ipcMain.handle("minicode:update:download", withMainSender("minicode:update:download", () => ctx.updater?.download?.() ?? false));
  ipcMain.handle("minicode:update:activity", withMainSender("minicode:update:activity", (_event, payload) => ctx.updater?.updateActivity?.(getWindowInstanceId(_event), payload) ?? { accepted: false, revision: 0 }));
  ipcMain.handle("minicode:update:preflight", withMainSender("minicode:update:preflight", () => ctx.updater?.preflight?.() ?? { allowed: false, checks: [], fingerprint: "", version: "" }));
  ipcMain.handle("minicode:update:install", withMainSender("minicode:update:install", (_event, payload) => ctx.updater?.install?.(payload) ?? { installed: false, reason: "unavailable" }));
  ipcMain.handle("minicode:update:status:get", withMainSender("minicode:update:status:get", () => ctx.updater?.getStatus?.() ?? { status: "idle" }));

  ipcMain.handle("minicode:pty:ackExit", withMainSender("minicode:pty:ackExit", (_event, sessionId, conversationId) => {
    return ptyManager.acknowledgeExitedSession(sessionId, conversationId, getWindowInstanceId(_event));
  }));

  // -----------------------------------------------------------------------
  // Environment detection
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:env:detect", withMainSender("minicode:env:detect", async () => {
    const [hasGit, hasPython, hasNode, hasDocker, hasOllama] = await Promise.all([
      checkCommand("git"),
      checkCommand("python"),
      checkCommand("node"),
      checkCommand("docker"),
      checkCommand("ollama"),
    ]);
    return {
      git: hasGit,
      python: hasPython,
      node: hasNode,
      docker: hasDocker,
      ollama: hasOllama,
      home: require("node:os").homedir(),
    };
  }));
  ipcMain.handle("minicode:sandbox:setup", withMainSender("minicode:sandbox:setup", () => setupWindowsSandbox()));
  ipcMain.handle("minicode:menu:popup", withMainSender("minicode:menu:popup", (_event, key) => {
    if (!NATIVE_MENU_KEYS.has(key)) throw new TypeError("请选择文件、编辑、视图或帮助菜单。");
    return popupApplicationMenu(key, getWin(_event));
  }));
  ipcMain.handle("minicode:menu:setState", withMainSender("minicode:menu:setState", (event, state) => {
    const keys = ["terminalEnabled", "bottomPanelToggleEnabled", "newProjectEnabled", "editorFocused"];
    if (!state || Object.keys(state).length !== keys.length + 5 || keys.some(key => typeof state[key] !== "boolean") || typeof state.shortcutRecording !== "boolean" || typeof state.workspaceRoot !== "string"
      || !state.accelerators || typeof state.accelerators !== "object" || Array.isArray(state.accelerators)
      || Object.values(state.accelerators).some(value => value !== null && typeof value !== "string")
      || !state.acceleratorBindings || typeof state.acceleratorBindings !== "object" || Array.isArray(state.acceleratorBindings)
      || Object.values(state.acceleratorBindings).some(value => !Array.isArray(value) || value.some(chord => typeof chord !== "string"))
      || !Array.isArray(state.shortcutOverrides) || state.shortcutOverrides.some(value => typeof value !== "string")) {
      throw new TypeError("Native menu state requires capability fields, the current accelerator map and its recording state.");
    }
    return setApplicationMenuState(getWin(event), state);
  }));
  ipcMain.handle("minicode:menu:execute", withMainSender("minicode:menu:execute", (event, action) => {
    if (typeof action !== "string") throw new TypeError("Native menu command must be an action id.");
    return executeApplicationMenuAction(getWin(event), action);
  }));
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  registerIpcHandlers,
};

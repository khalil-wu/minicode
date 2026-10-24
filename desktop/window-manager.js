"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { BrowserWindow, shell, dialog } = require("electron");
const desktopPreferences = require("./preferences");

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_WINDOW_STATE = {
  width: 1440,
  height: 920,
  minWidth: 390,
  minHeight: 700,
  x: undefined,
  y: undefined,
  isMaximized: false,
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const mainWindows = new Map();
const windowStateSaveTimers = new Map();

// Dependencies injected via init()
let appendDesktopLog = () => {};
let isHttpUrl = () => false;
let getRendererAdditionalArguments = () => [];
let preloadFile = "";
let desktopIconPath;
let frontendDevUrl = "";
let getAppRoot = () => process.cwd();
let onMainWindowCreated = null;
let onMainWindowClosed = null;
let onMainWindowCloseCancelled = () => {};
let onDiagnosticIncident = null;

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
  if (typeof deps.isHttpUrl === "function") isHttpUrl = deps.isHttpUrl;
  if (typeof deps.getRendererAdditionalArguments === "function") getRendererAdditionalArguments = deps.getRendererAdditionalArguments;
  if (typeof deps.preloadFile === "string") preloadFile = deps.preloadFile;
  if (deps.desktopIconPath !== undefined) desktopIconPath = deps.desktopIconPath;
  if (typeof deps.frontendDevUrl === "string") frontendDevUrl = deps.frontendDevUrl;
  if (typeof deps.getAppRoot === "function") getAppRoot = deps.getAppRoot;
  if (typeof deps.onMainWindowCreated === "function") onMainWindowCreated = deps.onMainWindowCreated;
  if (typeof deps.onMainWindowClosed === "function") onMainWindowClosed = deps.onMainWindowClosed;
  if (typeof deps.onMainWindowCloseCancelled === "function") onMainWindowCloseCancelled = deps.onMainWindowCloseCancelled;
  if (typeof deps.onDiagnosticIncident === "function") onDiagnosticIncident = deps.onDiagnosticIncident;
}

// ---------------------------------------------------------------------------
// Window state persistence
// ---------------------------------------------------------------------------

function getWindowStateFilePath(windowOwnerId = "primary") {
  const { app } = require("electron");
  return path.join(app.getPath("userData"), windowOwnerId === "primary" ? "window-state.json" : `window-state-${windowOwnerId}.json`);
}

function sanitizeWindowState(rawState) {
  if (!rawState || typeof rawState !== "object") {
    return { ...DEFAULT_WINDOW_STATE };
  }

  const nextState = { ...DEFAULT_WINDOW_STATE };
  for (const key of ["width", "height", "minWidth", "minHeight", "x", "y"]) {
    const value = rawState[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      nextState[key] = Math.floor(value);
    }
  }

  if (typeof rawState.isMaximized === "boolean") {
    nextState.isMaximized = rawState.isMaximized;
  }

  if (nextState.x !== undefined && nextState.y !== undefined) {
    const { screen } = require("electron");
    const displays = screen.getAllDisplays();
    const isOnScreen = displays.some((display) => {
      const { x: dx, y: dy, width: dw, height: dh } = display.workArea;
      return (
        nextState.x >= dx &&
        nextState.x < dx + dw - 64 &&
        nextState.y >= dy &&
        nextState.y < dy + dh - 64
      );
    });
    if (!isOnScreen) {
      nextState.x = undefined;
      nextState.y = undefined;
    }
  }

  return nextState;
}

function readWindowState(windowOwnerId = "primary") {
  try {
    const statePath = getWindowStateFilePath(windowOwnerId);
    if (!fs.existsSync(statePath)) {
      return { ...DEFAULT_WINDOW_STATE };
    }
    const raw = fs.readFileSync(statePath, "utf8");
    return sanitizeWindowState(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_WINDOW_STATE };
  }
}

function persistWindowState(window = null) {
  const windows = window ? [window] : getMainWindows();
  for (const win of windows) persistOneWindowState(win);
}

function persistOneWindowState(win) {
  if (win.isDestroyed()) return;
  const record = getWindowRecord(win);

  try {
    const bounds = win.isMaximized()
      ? win.getNormalBounds()
      : win.getBounds();
    const payload = {
      ...DEFAULT_WINDOW_STATE,
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      isMaximized: win.isMaximized(),
    };
    const statePath = getWindowStateFilePath(record.windowOwnerId);
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify(payload, null, 2), "utf8");
  } catch (error) {
    appendDesktopLog(`[desktop] failed to persist window state: ${error.message}`);
  }
}

function queueWindowStatePersist(win = getMainWindow()) {
  if (!win) return;
  clearWindowStateSaveTimer(win);
  windowStateSaveTimers.set(win, setTimeout(() => {
    windowStateSaveTimers.delete(win);
    persistWindowState(win);
  }, 180));
}

function clearWindowStateSaveTimer(window = null) {
  for (const [win, timer] of windowStateSaveTimers) {
    if (window && win !== window) continue;
    clearTimeout(timer);
    windowStateSaveTimers.delete(win);
  }
}

// ---------------------------------------------------------------------------
// Renderer diagnostics
// ---------------------------------------------------------------------------

function attachRendererDiagnostics(window, label) {
  window.webContents.on("console-message", ({ level, message, lineNumber: line, sourceId }) => {
    appendDesktopLog(`[renderer:console][${label}] level=${level} ${sourceId}:${line} ${message}`);
  });
  window.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    appendDesktopLog(
      `[renderer:did-fail-load][${label}] code=${errorCode} mainFrame=${isMainFrame} url=${validatedURL} ${errorDescription}`
    );
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    appendDesktopLog(`[renderer:gone][${label}] reason=${details.reason} exitCode=${details.exitCode}`);
    if (onDiagnosticIncident) {
      onDiagnosticIncident("render-process-gone", {
        label,
        reason: details.reason,
        exitCode: details.exitCode,
      });
    }
  });
}

// ---------------------------------------------------------------------------
// Frontend target resolution
// ---------------------------------------------------------------------------

function getFrontendDistFile() {
  return path.join(getAppRoot(), "frontend", "dist", "index.html");
}

function getFrontendTarget() {
  if (frontendDevUrl) {
    return { type: "url", value: frontendDevUrl };
  }

  const frontendDistFile = getFrontendDistFile();
  if (!fs.existsSync(frontendDistFile)) {
    throw new Error(
      "Missing frontend build output. Run: npm --prefix frontend run build"
    );
  }

  return { type: "file", value: frontendDistFile };
}

// ---------------------------------------------------------------------------
// Main window
// ---------------------------------------------------------------------------

function focusMainWindow(mainWindow = getMainWindow()) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return false;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  if (!mainWindow.isVisible()) {
    mainWindow.show();
  }
  mainWindow.focus();
  return true;
}

async function requestCloseWindow(window, source) {
  const record = getWindowRecord(window);
  record.closeSource = source;
  window.close();
  return true;
}

function isTrustedRendererUrl(target, candidateUrl) {
  try {
    const expected = target.type === "url"
      ? new URL(target.value)
      : new URL(`file:///${path.resolve(target.value).replace(/\\/g, "/")}`);
    const candidate = new URL(candidateUrl);
    if (expected.protocol === "file:") {
      return candidate.protocol === "file:" && candidate.pathname === expected.pathname;
    }
    return candidate.origin === expected.origin;
  } catch {
    return false;
  }
}

async function createMainWindow() {
  const existing = getMainWindow();
  if (existing) {
    focusMainWindow(existing);
    return existing;
  }
  return createNewWindow({ restoreLastConversation: true });
}

function nextWindowOwnerId() {
  const owners = new Set(Array.from(mainWindows.values(), (record) => record.windowOwnerId));
  if (!owners.has("primary")) return "primary";
  let slot = 1;
  while (owners.has(`secondary-${slot}`)) slot += 1;
  return `secondary-${slot}`;
}

async function createNewWindow({ conversationId = null, restoreLastConversation = false, remoteHostId = null, runtime = null, settingsTab,
  windowOwnerId: requestedOwnerId, popout = false, initialWorkspaceRoot = "" } = {}) {
  const { app } = require("electron");
  const target = getFrontendTarget();
  const windowOwnerId = requestedOwnerId ?? nextWindowOwnerId();
  const windowInstanceId = crypto.randomUUID();
  const windowBindingToken = crypto.randomBytes(32).toString("hex");
  const savedWindowState = readWindowState(windowOwnerId);
  const mainWindow = new BrowserWindow({
    x: savedWindowState.x,
    y: savedWindowState.y,
    width: savedWindowState.width,
    height: savedWindowState.height,
    minWidth: DEFAULT_WINDOW_STATE.minWidth,
    minHeight: DEFAULT_WINDOW_STATE.minHeight,
    resizable: true,
    maximizable: true,
    show: false,
    frame: false,
    autoHideMenuBar: true,
    backgroundColor: "#080808",
    title: "MiniCode",
    icon: desktopIconPath,
    webPreferences: {
      ...(remoteHostId ? { partition: `persist:minicode-host-${remoteHostId}` } : {}),
      zoomFactor: 1,
      preload: preloadFile,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
      sandbox: true,
      additionalArguments: [
        ...getRendererAdditionalArguments(),
        `--minicode-window-owner-id=${windowOwnerId}`,
        `--minicode-window-instance-id=${windowInstanceId}`,
        ...(remoteHostId ? [`--minicode-remote-host-id=${remoteHostId}`] : []),
      ],
    },
  });
  const record = { window: mainWindow, webContents: mainWindow.webContents, windowOwnerId, windowInstanceId, windowBindingToken,
    remoteHostId, runtime, initialNavigation: popout ? { conversationId, popout, workspaceRoot: initialWorkspaceRoot, restoreLastConversation }
      : restoreLastConversation ? null : { conversationId },
    pendingProviderSettings: settingsTab === "provider",
    nativeCloseAttempt: false, requestedReload: null, unloadIntent: null };
  mainWindows.set(record.webContents, record);

  if (onMainWindowCreated) onMainWindowCreated(mainWindow, record);

  mainWindow.on("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("move", () => queueWindowStatePersist(mainWindow));
  mainWindow.on("resize", () => queueWindowStatePersist(mainWindow));
  mainWindow.on("maximize", () => queueWindowStatePersist(mainWindow));
  mainWindow.on("unmaximize", () => queueWindowStatePersist(mainWindow));
  mainWindow.on("close", (event) => {
    const source = record.closeSource ?? "native";
    record.closeSource = undefined;
    if (!record.closeConfirmed && desktopPreferences.needsCloseConfirmation(source)) {
      event.preventDefault();
      const locale = desktopPreferences.getPreferences().localeOverride ?? require("electron").app.getLocale();
      const chinese = /^zh(?:[-_](?:cn|sg|hans.*))?$/i.test(locale);
      void dialog.showMessageBox(mainWindow, {
        type: "question", title: chinese ? "关闭窗口" : "Close window", message: chinese ? "关闭这个 MiniCode 窗口？" : "Close this MiniCode window?", buttons: chinese ? ["取消", "关闭"] : ["Cancel", "Close"], defaultId: 0, cancelId: 0,
      }).then(({ response }) => {
        if (response === 1) { record.closeConfirmed = true; mainWindow.close(); }
        else onMainWindowCloseCancelled(mainWindow);
      });
      return;
    }
    record.nativeCloseAttempt = true;
    record.requestedReload = null;
    clearWindowStateSaveTimer(mainWindow);
    persistWindowState(mainWindow);
  });

  mainWindow.on("closed", () => {
    clearWindowStateSaveTimer(mainWindow);
    mainWindows.delete(record.webContents);
    if (onMainWindowClosed) onMainWindowClosed(mainWindow, record);
  });

  attachRendererDiagnostics(mainWindow, `${windowOwnerId}/${windowInstanceId}`);

  mainWindow.webContents.on("will-prevent-unload", () => {
    record.nativeCloseAttempt = false;
    record.closeConfirmed = false;
  });
  mainWindow.webContents.on("did-navigate", () => {
    record.nativeCloseAttempt = false;
    record.requestedReload = null;
    record.unloadIntent = null;
    record.closeConfirmed = false;
    record.closeSource = undefined;
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttpUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  const publishWindowState = () => mainWindow.webContents.send("minicode:window:state", { isMaximized: mainWindow.isMaximized(), isFocused: mainWindow.isFocused() });
  mainWindow.on("maximize", publishWindowState);
  mainWindow.on("unmaximize", publishWindowState);
  mainWindow.on("focus", publishWindowState);
  mainWindow.on("blur", publishWindowState);

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (isTrustedRendererUrl(target, url)) return;
    appendDesktopLog(`[desktop] blocked renderer navigation to ${url || "unknown URL"}`);
    event.preventDefault();
  });

  try {
    if (target.type === "url") {
      await mainWindow.loadURL(target.value);
    } else {
      await mainWindow.loadFile(target.value);
    }
  } catch (error) {
    mainWindow.destroy();
    throw error;
  }

  if (!app.isPackaged && process.env.MINICODE_OPEN_DEVTOOLS === "1") {
    mainWindow.webContents.openDevTools();
  }

  if (savedWindowState.isMaximized) {
    mainWindow.maximize();
  }

  return mainWindow;
}

// ---------------------------------------------------------------------------
// Accessors
// ---------------------------------------------------------------------------

function getMainWindow() {
  const records = Array.from(mainWindows.values());
  return (records.find((record) => record.windowOwnerId === "primary") || records[0])?.window || null;
}

function setMainWindow(win) {
  const current = getMainWindow();
  if (current) mainWindows.delete(current.webContents);
  if (win) mainWindows.set(win.webContents, { window: win, webContents: win.webContents, windowOwnerId: "primary", windowInstanceId: crypto.randomUUID(), windowBindingToken: crypto.randomBytes(32).toString("hex") });
}

function getMainWindows() {
  return Array.from(mainWindows.values(), (record) => record.window);
}

function getWindowRecord(window) {
  return mainWindows.get(window.webContents) || null;
}

function getWindowForSender(sender) {
  return mainWindows.get(sender)?.window || null;
}

function getWindowByInstanceId(instanceId) {
  return getWindowRecordByInstanceId(instanceId)?.window || null;
}

function getWindowRecordByInstanceId(instanceId) {
  return Array.from(mainWindows.values()).find((record) => record.windowInstanceId === instanceId) || null;
}

function getFocusedMainWindow() {
  const focused = BrowserWindow.getFocusedWindow();
  return focused && getWindowRecord(focused) ? focused : getMainWindow();
}

function captureUnloadIntent(window) {
  const record = getWindowRecord(window);
  const intent = { id: crypto.randomUUID(), action: record.nativeCloseAttempt ? "close" : "reload",
    ignoreCache: record.nativeCloseAttempt ? false : record.requestedReload === true };
  record.requestedReload = null;
  record.unloadIntent = { ...intent, closeConfirmed: record.closeConfirmed };
  return intent;
}

function reloadWindow(window, ignoreCache = false) {
  const record = getWindowRecord(window);
  record.nativeCloseAttempt = false;
  record.requestedReload = ignoreCache;
  if (ignoreCache) window.webContents.reloadIgnoringCache();
  else window.webContents.reload();
}

function resumeUnload(window, intentId) {
  const record = getWindowRecord(window);
  const intent = record.unloadIntent;
  if (!intent || intent.id !== intentId) return false;
  record.unloadIntent = null;
  if (intent.action === "close") { record.closeConfirmed = intent.closeConfirmed; window.close(); }
  else reloadWindow(window, intent.ignoreCache);
  return true;
}

function readWindowStatePublic() {
  return readWindowState();
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  DEFAULT_WINDOW_STATE,

  // Window state
  readWindowState: readWindowStatePublic,
  persistWindowState,
  queueWindowStatePersist,
  clearWindowStateSaveTimer,

  // Window management
  createMainWindow,
  createNewWindow,
  focusMainWindow,
  getMainWindow,
  getMainWindows,
  getWindowRecord,
  getWindowForSender,
  getWindowByInstanceId,
  getWindowRecordByInstanceId,
  getFocusedMainWindow,
  captureUnloadIntent,
  resumeUnload,
  reloadWindow,
  setMainWindow,
  requestCloseWindow,

  // Frontend target
  getFrontendTarget,
  isTrustedRendererUrl,
};

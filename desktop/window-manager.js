"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { BrowserWindow, shell } = require("electron");

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

let mainWindow = null;
let windowStateSaveTimer = null;

// Dependencies injected via init()
let appendDesktopLog = () => {};
let isHttpUrl = () => false;
let getRendererAdditionalArguments = () => [];
let preloadFile = "";
let desktopIconPath;
let frontendDevUrl = "";
let getAppRoot = () => process.cwd();
let onMainWindowCreated = null;
let onDeepLink = null;
let getPendingDeepLink = () => null;

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
  if (typeof deps.onDeepLink === "function") onDeepLink = deps.onDeepLink;
  if (typeof deps.getPendingDeepLink === "function") getPendingDeepLink = deps.getPendingDeepLink;
}

// ---------------------------------------------------------------------------
// Window state persistence
// ---------------------------------------------------------------------------

function getWindowStateFilePath() {
  const { app } = require("electron");
  return path.join(app.getPath("userData"), "window-state.json");
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

function readWindowState() {
  try {
    const statePath = getWindowStateFilePath();
    if (!fs.existsSync(statePath)) {
      return { ...DEFAULT_WINDOW_STATE };
    }
    const raw = fs.readFileSync(statePath, "utf8");
    return sanitizeWindowState(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_WINDOW_STATE };
  }
}

function persistWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }

  try {
    const bounds = mainWindow.isMaximized()
      ? mainWindow.getNormalBounds()
      : mainWindow.getBounds();
    const payload = {
      ...DEFAULT_WINDOW_STATE,
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      isMaximized: mainWindow.isMaximized(),
    };
    const statePath = getWindowStateFilePath();
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify(payload, null, 2), "utf8");
  } catch (error) {
    appendDesktopLog(`[desktop] failed to persist window state: ${error.message}`);
  }
}

function queueWindowStatePersist() {
  if (windowStateSaveTimer) {
    clearTimeout(windowStateSaveTimer);
  }
  windowStateSaveTimer = setTimeout(() => {
    windowStateSaveTimer = null;
    persistWindowState();
  }, 180);
}

function clearWindowStateSaveTimer() {
  if (windowStateSaveTimer) {
    clearTimeout(windowStateSaveTimer);
    windowStateSaveTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Renderer diagnostics
// ---------------------------------------------------------------------------

function attachRendererDiagnostics(window, label) {
  window.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    appendDesktopLog(`[renderer:console][${label}] level=${level} ${sourceId}:${line} ${message}`);
  });
  window.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    appendDesktopLog(
      `[renderer:did-fail-load][${label}] code=${errorCode} mainFrame=${isMainFrame} url=${validatedURL} ${errorDescription}`
    );
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    appendDesktopLog(`[renderer:gone][${label}] reason=${details.reason} exitCode=${details.exitCode}`);
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

function focusMainWindow() {
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
  const { app } = require("electron");
  const target = getFrontendTarget();
  const savedWindowState = readWindowState();

  if (mainWindow && !mainWindow.isDestroyed()) {
    focusMainWindow();
    return mainWindow;
  }

  mainWindow = new BrowserWindow({
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
      preload: preloadFile,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      additionalArguments: getRendererAdditionalArguments(),
    },
  });

  mainWindow.on("ready-to-show", () => {
    mainWindow?.show();
  });

  mainWindow.on("move", queueWindowStatePersist);
  mainWindow.on("resize", queueWindowStatePersist);
  mainWindow.on("maximize", queueWindowStatePersist);
  mainWindow.on("unmaximize", queueWindowStatePersist);

  mainWindow.on("closed", () => {
    persistWindowState();
    mainWindow = null;
  });

  attachRendererDiagnostics(mainWindow, "main");

  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    // Ctrl+` toggles terminal panel
    if (input.control && !input.alt && input.key === "`") {
      mainWindow.webContents.send("minicode:shortcut:terminal");
      event.preventDefault();
      return;
    }
    // Ctrl+P opens quick-open (intentional override of browser print shortcut)
    if (input.control && !input.shift && !input.alt && input.key === "p") {
      mainWindow.webContents.send("minicode:shortcut:import");
      event.preventDefault();
      return;
    }
    // Zoom: Ctrl+= / Ctrl+Plus -> zoom in, Ctrl+- -> zoom out, Ctrl+0 -> reset
    if (input.control && !input.alt) {
      const wc = mainWindow.webContents;
      if (input.key === "=" || input.key === "+") {
        wc.setZoomLevel(wc.getZoomLevel() + 0.5);
        event.preventDefault();
        return;
      }
      if (input.key === "-") {
        wc.setZoomLevel(wc.getZoomLevel() - 0.5);
        event.preventDefault();
        return;
      }
      if (input.key === "0") {
        wc.setZoomLevel(0);
        event.preventDefault();
        return;
      }
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttpUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });

  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (isTrustedRendererUrl(target, url)) return;
    appendDesktopLog(`[desktop] blocked renderer navigation to ${url || "unknown URL"}`);
    event.preventDefault();
  });

  if (target.type === "url") {
    await mainWindow.loadURL(target.value);
  } else {
    await mainWindow.loadFile(target.value);
  }

  if (!app.isPackaged && process.env.MINICODE_OPEN_DEVTOOLS === "1") {
    mainWindow.webContents.openDevTools();
  }

  if (savedWindowState.isMaximized) {
    mainWindow.maximize();
  }

  const pendingDeepLink = getPendingDeepLink();
  if (pendingDeepLink) {
    mainWindow.webContents.once("did-finish-load", () => {
      if (getPendingDeepLink()) {
        mainWindow?.webContents.send("minicode:deep-link", { target: getPendingDeepLink() });
      }
    });
  }

  if (typeof onMainWindowCreated === "function") {
    onMainWindowCreated();
  }

  return mainWindow;
}

// ---------------------------------------------------------------------------
// Accessors
// ---------------------------------------------------------------------------

function getMainWindow() {
  return mainWindow;
}

function setMainWindow(win) {
  mainWindow = win;
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
  focusMainWindow,
  getMainWindow,
  setMainWindow,

  // Frontend target
  getFrontendTarget,
  isTrustedRendererUrl,
};

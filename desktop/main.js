const { app, BrowserWindow, crashReporter, dialog, Menu, Notification, powerMonitor, protocol, safeStorage, session, shell, webContents } = require("electron");
const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");

// ---------------------------------------------------------------------------
// Extracted modules
// ---------------------------------------------------------------------------

const utils = require("./utils");
const security = require("./security");
const backendSidecar = require("./backend-sidecar");
const cdpBridge = require("./cdp-bridge");
const embeddedBrowserManager = require("./embedded-browser-manager");
const embeddedBrowserBridge = require("./embedded-browser-bridge");
const ptyManager = require("./pty-manager");
const windowManager = require("./window-manager");
const { releaseEditorWindow, closeWindowsAfterDraftCommit } = require("./window-lifecycle");
const { externalBackendHostContract, verifyExternalBackendHostContract } = require("./backend-host-contract");
const ipcHandlers = require("./ipc-handlers");
const updater = require("./updater");
const crashReporting = require("./crash-reporter");
const { setupWindowsSandbox } = require("./windows-sandbox-setup");
const { popupApplicationMenu } = require("./menu-popup");
const desktopPreferences = require("./preferences");
const browserHistory = require("./browser-history");
const { createBrowserProfileImporter } = require("./browser-profile-import");
const { createBrowserPersonalData } = require("./browser-personal-data");
const { registerMcpAppScheme, createMcpAppSandbox } = require("./mcp-app-sandbox");
const { startGeneralMaintenance } = require("./general-maintenance");
const notificationSounds = require("./notification-sounds");
const { createPetManager } = require("./pet-manager");
const { createPetActionBridge } = require("./pet-action-bridge");
const { createDictationHotkeys } = require("./dictation-hotkeys");
const { RemoteDesktop } = require("./remote-desktop");
let remoteDesktop = null;
let petManager = null;
let petInitialization = null;
let mcpAppSandbox = null;
let generalMaintenance = null;
registerMcpAppScheme(protocol);
const petActionBridge = createPetActionBridge(windowManager.getWindowByInstanceId);
const dictationHotkeys = createDictationHotkeys({ app, getMainWindows: windowManager.getMainWindows,
  getFocusedMainWindow: windowManager.getFocusedMainWindow, getFocusedWebContents: () => webContents.getFocusedWebContents() });
app.on("browser-window-focus", (_event, window) => dictationHotkeys.noteFocusedWindow(window));

// ---------------------------------------------------------------------------
// Optional native PTY
// ---------------------------------------------------------------------------

let pty;
try {
  pty = require("node-pty");
} catch (e) {
  // node-pty might fail to load if not built properly
}

// ---------------------------------------------------------------------------
// File path constants
// ---------------------------------------------------------------------------

const PRELOAD_FILE = path.join(__dirname, "preload.js");
const STARTUP_ERROR_FILE = path.join(__dirname, "startup-error.html");
const STARTUP_ERROR_PRELOAD_FILE = path.join(__dirname, "startup-error-preload.js");

// ---------------------------------------------------------------------------
// Backend / runtime configuration
// ---------------------------------------------------------------------------

const BACKEND_HOST = utils.BACKEND_HOST;
const BACKEND_PORT = Number(process.env.MINICODE_BACKEND_PORT || "8000");
let resolvedBackendPort = BACKEND_PORT;
let resolvedApiBaseUrl =
  process.env.MINICODE_API_BASE_URL || `http://${BACKEND_HOST}:${BACKEND_PORT}`;
let resolvedWsBaseUrl =
  process.env.MINICODE_WS_BASE_URL || `ws://${BACKEND_HOST}:${BACKEND_PORT}`;
let backendRuntimeRevision = 0;
const FRONTEND_DEV_URL = (process.env.MINICODE_FRONTEND_URL || "").trim();
let resolvedFrontendUrl = FRONTEND_DEV_URL;
const MANAGE_BACKEND = process.env.MINICODE_SKIP_BACKEND !== "1";
const RUNTIME_TOKEN =
  process.env.MINICODE_RUNTIME_TOKEN ||
  crypto.randomBytes(32).toString("hex");
// Embedded tool admission is a backend capability. The general API token is
// visible to the main renderer, so it cannot attest backend-owned navigation.
const EMBEDDED_BROWSER_TOKEN = MANAGE_BACKEND ? crypto.randomBytes(32).toString("hex") : String(process.env.MINICODE_EMBEDDED_BROWSER_TOKEN || "");

function resolvePythonCommand() {
  if (process.env.MINICODE_PYTHON) return process.env.MINICODE_PYTHON;
  if (app.isPackaged && process.platform === "win32") {
    const bundled = path.join(process.resourcesPath, "python-runtime", "python.exe");
    if (fs.existsSync(bundled)) return bundled;
  }
  return process.platform === "win32" ? "py" : "python3";
}

const PYTHON_COMMAND = resolvePythonCommand();
const GITHUB_CLI_COMMAND = process.env.MINICODE_GH_COMMAND || path.join(
  app.isPackaged ? process.resourcesPath : path.join(getAppRoot(), "desktop"),
  "github-runtime", "bin", "gh.exe",
);
process.env.MINICODE_GH_COMMAND = GITHUB_CLI_COMMAND;
if (path.isAbsolute(GITHUB_CLI_COMMAND)) {
  const pathKey = Object.keys(process.env).find((name) => name.toLowerCase() === "path") || "PATH";
  process.env[pathKey] = [path.dirname(GITHUB_CLI_COMMAND), process.env[pathKey]].filter(Boolean).join(path.delimiter);
}

const RENDERER_CSP = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https: http://localhost:* http://127.0.0.1:*",
  "font-src 'self' data:",
  "connect-src 'self' blob: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:* https://localhost:* https://127.0.0.1:* wss://localhost:* wss://127.0.0.1:*",
  "frame-src 'self' data: blob: http://localhost:* http://127.0.0.1:* http://*.localhost:*",
  "worker-src 'self' blob:",
  "media-src 'self' data: blob: http://localhost:* http://127.0.0.1:*",
  "form-action 'self'",
].join("; ");

// Dev variant: the Vite react-refresh preamble is an inline module script, so
// script-src must allow inline (and localhost) content when serving from the
// dev server. Production (file:) keeps the strict policy above.
const RENDERER_CSP_DEV = RENDERER_CSP.replace(
  "script-src 'self'",
  "script-src 'self' 'unsafe-inline' http://localhost:* http://127.0.0.1:*",
);

function installRendererCspHeaders(targetSession = session.defaultSession) {
  targetSession.webRequest.onHeadersReceived((details, callback) => {
    const isDevDocument =
      Boolean(FRONTEND_DEV_URL) && details.url.startsWith(FRONTEND_DEV_URL);
    const isRendererDocument =
      details.resourceType === "mainFrame" &&
      (details.url.startsWith("file:") || isDevDocument);
    if (!isRendererDocument) {
      callback({ responseHeaders: details.responseHeaders });
      return;
    }
    const responseHeaders = { ...(details.responseHeaders || {}) };
    for (const name of Object.keys(responseHeaders)) {
      if (name.toLowerCase() === "content-security-policy") delete responseHeaders[name];
    }
    responseHeaders["Content-Security-Policy"] = [isDevDocument ? RENDERER_CSP_DEV : RENDERER_CSP];
    callback({ responseHeaders });
  });
}

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

let pendingDeepLink = null;
let startupFailureWindow = null;
let startupRetryInFlight = false;
let lastPickedWorkspaceRoot = "";
const recentDiagnosticIncidents = [];
let startupFailureState = {
  title: "MiniCode Desktop couldn't finish startup",
  message: "The desktop shell could not reach the backend sidecar yet.",
  detail: "",
  logsPath: "",
};

// ---------------------------------------------------------------------------
// App initialization
// ---------------------------------------------------------------------------

if (
  process.env.MINICODE_DISABLE_HARDWARE_ACCELERATION === "1"
  || process.env.MINICODE_ENABLE_HARDWARE_ACCELERATION === "0"
) {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu");
  app.commandLine.appendSwitch("disable-gpu-compositing");
  app.commandLine.appendSwitch("disable-gpu-rasterization");
}
if (process.env.MINICODE_DISABLE_CHROMIUM_SANDBOX === "1") {
  app.commandLine.appendSwitch("no-sandbox");
}
if (process.env.MINICODE_ENABLE_EMBEDDED_BROWSER_CDP === "1") {
  app.commandLine.appendSwitch("remote-debugging-address", "127.0.0.1");
  app.commandLine.appendSwitch(
    "remote-debugging-port",
    process.env.MINICODE_BROWSER_DEBUG_PORT || "9222",
  );
}
if (process.env.MINICODE_USER_DATA_DIR) {
  app.setPath("userData", process.env.MINICODE_USER_DATA_DIR);
} else {
  app.setPath("userData", path.join(app.getPath("appData"), require("./package.json").name));
}
desktopPreferences.init(path.join(app.getPath("userData"), "desktop-preferences.json"), {
  defaultTaskDirectory: process.env.MINICODE_DEFAULT_TASK_DIRECTORY || path.join(app.getPath("documents"), "MiniCode"),
});
browserHistory.init(path.join(app.getPath("userData"), "browser-history.json"));
const browserPersonalData = createBrowserPersonalData({ filePath: path.join(app.getPath("userData"), "browser-personal-data.enc"), safeStorage });
crashReporting.init({ crashReporter, app, logger: appendDesktopLog });

const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
  app.quit();
}

// ---------------------------------------------------------------------------
// Core functions retained in main.js
// ---------------------------------------------------------------------------

function getAppRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function resolveDesktopIconPath() {
  const iconCandidates = [
    path.join(__dirname, "build", "icon.ico"),
    path.join(process.resourcesPath || "", "build", "icon.ico"),
  ];
  for (const iconPath of iconCandidates) {
    if (iconPath && fs.existsSync(iconPath)) {
      return iconPath;
    }
  }
  return undefined;
}

const DESKTOP_ICON_PATH = resolveDesktopIconPath();

// Logging wrapper that also tracks the log path in startupFailureState
function appendDesktopLog(message) {
  utils.appendDesktopLog(message);
  try {
    startupFailureState = {
      ...startupFailureState,
      logsPath: utils.getDesktopLogPath(),
    };
  } catch {
    // noop
  }
}

function recordDiagnosticIncident(kind, details = {}) {
  const incident = {
    kind: String(kind || "unknown"),
    at: new Date().toISOString(),
    details: details && typeof details === "object" ? details : { message: String(details) },
  };
  recentDiagnosticIncidents.push(incident);
  if (recentDiagnosticIncidents.length > 50) recentDiagnosticIncidents.shift();
  appendDesktopLog(`[desktop:incident] ${JSON.stringify(incident)}`);
  return incident;
}

function isBenignPipeError(error) {
  if (!error) return false;
  const message = error instanceof Error ? error.message : String(error);
  const code = typeof error === "object" && error ? error.code : "";
  return code === "EPIPE" || /broken pipe/i.test(message);
}

function writeStdout(message) {
  appendDesktopLog(message.trimEnd());
  try {
    if (!process.stdout.destroyed && process.stdout.writable) {
      process.stdout.write(message);
    }
  } catch (error) {
    if (error?.code !== "EPIPE") {
      appendDesktopLog(`[desktop:stdout-error] ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

function writeStderr(message) {
  appendDesktopLog(message.trimEnd());
  try {
    if (!process.stderr.destroyed && process.stderr.writable) {
      process.stderr.write(message);
    }
  } catch (error) {
    if (error?.code !== "EPIPE") {
      appendDesktopLog(`[desktop:stderr-error] ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Process-level error handlers
// ---------------------------------------------------------------------------

process.on("uncaughtException", (error) => {
  if (isBenignPipeError(error)) {
    appendDesktopLog(`[desktop] suppressed uncaughtException: ${error.message}`);
    return;
  }
  recordDiagnosticIncident("uncaughtException", {
    message: error instanceof Error ? error.message : String(error),
    stack: error instanceof Error ? error.stack : undefined,
  });
});

process.on("unhandledRejection", (reason) => {
  if (isBenignPipeError(reason)) {
    appendDesktopLog(`[desktop] suppressed unhandledRejection: ${reason.message}`);
    return;
  }
  recordDiagnosticIncident("unhandledRejection", {
    message: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : undefined,
  });
});

// ---------------------------------------------------------------------------
// Error serialization & diagnostics
// ---------------------------------------------------------------------------

function serializeError(error, contextLabel) {
  const baseMessage = error instanceof Error ? error.message : String(error);
  const detail = error instanceof Error && error.stack ? error.stack : baseMessage;
  return {
    title: "MiniCode Desktop couldn't finish startup",
    message: contextLabel || baseMessage,
    detail,
    logsPath: utils.getDesktopLogPath(),
  };
}

function buildDiagnosticsPayload() {
  const logPath = utils.getDesktopLogPath();
  return {
    generatedAt: new Date().toISOString(),
    app: {
      name: app.getName(),
      version: app.getVersion(),
      isPackaged: app.isPackaged,
      userData: app.getPath("userData"),
      appRoot: getAppRoot(),
    },
    release: {
      channel: "windows_private_beta",
      // Permission decisions and enforced filesystem/network capabilities
      // come from the live harness/execution plane, not desktop constants.
      capabilitySource: "runtime.capabilities.inspect",
    },
    runtime: {
      platform: process.platform,
      arch: process.arch,
      node: process.versions.node,
      electron: process.versions.electron,
      chrome: process.versions.chrome,
    },
    backend: {
      apiBaseUrl: resolvedApiBaseUrl,
      wsBaseUrl: resolvedWsBaseUrl,
      port: resolvedBackendPort,
      managedByApp: backendSidecar.isBackendManagedByApp(),
      hasProcess: Boolean(backendSidecar.getBackendProcess()),
      restartAttempt: backendSidecar.getBackendRestartAttempt(),
      manageBackend: MANAGE_BACKEND,
      pythonCommand: PYTHON_COMMAND,
      sandboxRuntime: process.env.MINICODE_SANDBOX_RUNTIME || "auto",
      sandboxImage: process.env.MINICODE_SANDBOX_IMAGE || "minicode-agent-sandbox:latest",
    },
    windows: {
      hasMainWindow: Boolean(windowManager.getMainWindow() && !windowManager.getMainWindow().isDestroyed()),
      registered: windowManager.getMainWindows().map((window) => {
        const record = windowManager.getWindowRecord(window);
        return { windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId };
      }),
      hasStartupFailureWindow: Boolean(startupFailureWindow && !startupFailureWindow.isDestroyed()),
      savedState: windowManager.readWindowState(),
    },
    startupFailure: startupFailureState,
    logs: {
      desktopLogPath: logPath,
      desktopLogExists: fs.existsSync(logPath),
    },
    recentIncidents: recentDiagnosticIncidents.slice(),
  };
}

function exportDesktopDiagnostics() {
  const outputPath = path.join(app.getPath("userData"), "desktop.diagnostics.json");
  const payload = buildDiagnosticsPayload();
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(payload, null, 2), "utf8");
  appendDesktopLog(`[desktop] diagnostics exported: ${outputPath}`);
  return {
    path: outputPath,
    payload,
  };
}

// ---------------------------------------------------------------------------
// Backend runtime resolution
// ---------------------------------------------------------------------------

async function resolveBackendRuntime() {
  let nextBackendPort = resolvedBackendPort;
  if (MANAGE_BACKEND && !backendSidecar.getBackendProcess()) {
    nextBackendPort = await utils.findAvailablePort(BACKEND_PORT);
    if (nextBackendPort !== BACKEND_PORT) {
      appendDesktopLog(`[backend] port ${BACKEND_PORT} is busy; using ${nextBackendPort}`);
    }
  }

  let nextApiBaseUrl;
  let nextWsBaseUrl;
  if (MANAGE_BACKEND) {
    nextApiBaseUrl = `http://${BACKEND_HOST}:${nextBackendPort}`;
    nextWsBaseUrl = `ws://${BACKEND_HOST}:${nextBackendPort}`;
  } else {
    nextApiBaseUrl =
      process.env.MINICODE_API_BASE_URL || `http://${BACKEND_HOST}:${nextBackendPort}`;
    nextWsBaseUrl =
      process.env.MINICODE_WS_BASE_URL || `ws://${BACKEND_HOST}:${nextBackendPort}`;
  }

  const runtimeChanged =
    nextBackendPort !== resolvedBackendPort
    || nextApiBaseUrl !== resolvedApiBaseUrl
    || nextWsBaseUrl !== resolvedWsBaseUrl;
  resolvedBackendPort = nextBackendPort;
  resolvedApiBaseUrl = nextApiBaseUrl;
  resolvedWsBaseUrl = nextWsBaseUrl;
  if (runtimeChanged) backendRuntimeRevision += 1;
  process.env.MINICODE_BACKEND_PORT = String(resolvedBackendPort);
  process.env.MINICODE_API_BASE_URL = resolvedApiBaseUrl;
  process.env.MINICODE_WS_BASE_URL = resolvedWsBaseUrl;
  resolvedFrontendUrl = FRONTEND_DEV_URL || process.env.MINICODE_FRONTEND_URL || "";
  if (resolvedFrontendUrl) {
    process.env.MINICODE_FRONTEND_URL = resolvedFrontendUrl;
  }
  process.env.MINICODE_RUNTIME_TOKEN = RUNTIME_TOKEN;
  return getBackendRuntimeConfig();
}

function getBackendRuntimeConfig() {
  return {
    revision: backendRuntimeRevision,
    backendPort: resolvedBackendPort,
    apiBaseUrl: resolvedApiBaseUrl,
    wsBaseUrl: resolvedWsBaseUrl,
    runtimeToken: RUNTIME_TOKEN,
  };
}

function getWindowRuntimeConfig(window) {
  const record = windowManager.getWindowRecord(window);
  return { ...(record.remoteHostId ? record.runtime : getBackendRuntimeConfig()), windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId, windowBindingToken: record.windowBindingToken };
}

const windowCleanupTasks = new Map();
let desktopCleanupFailed = false;

function releaseWindowResources(record, retry = false) {
  if (record.cleanupTask && !retry) return record.cleanupTask;
  const runtime = { ...record.runtime, windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId,
    desktopLifecycleToken: EMBEDDED_BROWSER_TOKEN };
  embeddedBrowserBridge.releaseWindow(record.windowInstanceId);
  embeddedBrowserManager.releaseWindow(record.windowInstanceId);
  const cleanupTask = record.remoteHostId ? remoteDesktop.releaseWindow(record, retry) : Promise.all([
    ptyManager.releaseWindow(record.windowInstanceId),
    releaseEditorWindow(runtime),
  ]);
  record.cleanupTask = cleanupTask;
  record.cleanupError = null;
  windowCleanupTasks.set(record, cleanupTask);
  void cleanupTask.then(() => {
    if (windowCleanupTasks.get(record) === cleanupTask) windowCleanupTasks.delete(record);
  }, (error) => {
    // Retain the rejected operation until the next explicit shutdown attempt
    // retries this same owner. The error must remain observable by drain.
    if (record.cleanupTask === cleanupTask) record.cleanupError = error;
    appendDesktopLog(`[desktop] window ${record.windowInstanceId} cleanup failed: ${error.message}`);
  });
  return record.cleanupTask;
}

async function launchManagedBackend() {
  return backendSidecar.launchBackendSidecar({
    resolveRuntime: resolveBackendRuntime,
    getRuntime: getBackendRuntimeConfig,
    timeoutMs: BACKEND_STARTUP_TIMEOUT_MS,
  });
}

function publishBackendRuntimeChange(previousRuntime, nextRuntime) {
  for (const win of windowManager.getMainWindows()) {
    const record = windowManager.getWindowRecord(win);
    if (!record.remoteHostId) record.runtime = { ...nextRuntime };
  }
  if (
    previousRuntime.apiBaseUrl === nextRuntime.apiBaseUrl
    && previousRuntime.wsBaseUrl === nextRuntime.wsBaseUrl
  ) {
    return;
  }
  for (const win of windowManager.getMainWindows()) {
    if (windowManager.getWindowRecord(win).remoteHostId) continue;
    win.webContents.send("minicode:runtime:changed", getWindowRuntimeConfig(win));
  }
}

async function restartManagedBackend(reason) {
  if (!MANAGE_BACKEND) return;
  const previousRuntime = getBackendRuntimeConfig();
  appendDesktopLog(`[backend] restarting sidecar after ${reason}`);
  const nextRuntime = await launchManagedBackend();
  publishBackendRuntimeChange(previousRuntime, nextRuntime);
  if (startupFailureWindow && !windowManager.getMainWindow()) {
    await windowManager.createMainWindow();
    closeStartupFailureWindow();
  }
}

function getRendererAdditionalArguments() {
  return [
    `--minicode-api-base-url=${resolvedApiBaseUrl}`,
    `--minicode-ws-base-url=${resolvedWsBaseUrl}`,
  ];
}

// ---------------------------------------------------------------------------
// Deep link & menu helpers
// ---------------------------------------------------------------------------

function normalizeDeepLinkTarget(target) {
  if (!target) return false;

  if (typeof target === "object" && target.kind === "conversation") {
    const conversationId = String(target.conversationId || "").trim();
    return conversationId ? { kind: "conversation", conversationId } : null;
  }

  const urlStr = String(target);
  const configuredDeepLinkHosts = new Set(
    String(process.env.MINICODE_DEEP_LINK_ALLOWED_HOSTS || "")
      .split(",")
      .map((host) => host.trim().toLowerCase())
      .filter(Boolean),
  );
  try {
    const parsed = new URL(urlStr);
    const protocol = parsed.protocol.toLowerCase();
    if (protocol !== "minicode:" && protocol !== "https:") {
      console.warn("[desktop] Blocked deep link with disallowed protocol:", parsed.protocol);
      return null;
    }
    if (protocol === "https:" && !configuredDeepLinkHosts.has(parsed.hostname.toLowerCase())) {
      console.warn("[desktop] Blocked deep link for untrusted host:", parsed.hostname);
      return null;
    }
  } catch {
    // Not a valid URL — block it
    console.warn("[desktop] Blocked malformed deep link:", urlStr.slice(0, 100));
    return null;
  }

  const parsed = new URL(urlStr);
  if (parsed.protocol.toLowerCase() === "minicode:") {
    const route = [parsed.hostname, ...parsed.pathname.split("/").filter(Boolean)];
    const conversationIndex = route.findIndex((part) => part === "conversation" || part === "task");
    const conversationId = parsed.searchParams.get("conversation_id") || parsed.searchParams.get("task_id") ||
      (conversationIndex >= 0 ? route[conversationIndex + 1] : "");
    if (conversationId) return { kind: "conversation", conversationId };
  }
  return { kind: "url", url: urlStr };
}

function dispatchDeepLink(target, win = windowManager.getFocusedMainWindow()) {
  const normalizedTarget = normalizeDeepLinkTarget(target);
  if (!normalizedTarget) return false;

  pendingDeepLink = { id: crypto.randomUUID(), target: normalizedTarget, windowInstanceId: win ? windowManager.getWindowRecord(win).windowInstanceId : null };
  if (win && !win.isDestroyed()) {
    windowManager.focusMainWindow(win);
    win.webContents.send("minicode:deep-link", { id: pendingDeepLink.id, target: pendingDeepLink.target });
  }
  return true;
}

function getPendingDeepLink(win) {
  if (!pendingDeepLink) return null;
  const { windowInstanceId } = windowManager.getWindowRecord(win);
  if (!pendingDeepLink.windowInstanceId) pendingDeepLink.windowInstanceId = windowInstanceId;
  return pendingDeepLink.windowInstanceId === windowInstanceId ? { id: pendingDeepLink.id, target: pendingDeepLink.target } : null;
}

function acknowledgeDeepLink(id, win) {
  if (!pendingDeepLink || pendingDeepLink.id !== String(id || "")) return false;
  if (pendingDeepLink.windowInstanceId !== windowManager.getWindowRecord(win).windowInstanceId) return false;
  pendingDeepLink = null;
  return true;
}

function sendWorkbenchMenuEvent(channel, win = windowManager.getFocusedMainWindow()) {
  if (!win || win.isDestroyed()) return false;
  win.webContents.send(channel);
  windowManager.focusMainWindow(win);
  return true;
}

const desktopNotifications = new Map();

function hideDesktopNotifications(selector, owner) {
  if (selector.onlyIfFocused && !owner.isFocused()) return 0;
  let hidden = 0;
  for (const [notification, receipt] of desktopNotifications) {
    if (receipt.conversationId !== selector.conversationId) continue;
    if (selector.requestId != null && receipt.requestId !== selector.requestId) continue;
    if (["turnId", "messageId", "taskId"].some((key) => selector[key] != null && receipt[key] !== selector[key])) continue;
    notification.close();
    desktopNotifications.delete(notification);
    hidden += 1;
  }
  return hidden;
}

function showDesktopNotification(payload, owner) {
  const focused = windowManager.getMainWindows().some((window) => window.isFocused());
  if (!desktopPreferences.shouldNotify(payload.kind, focused)) return false;
  if ((payload.kind === "permission" || payload.kind === "question")
    && payload.conversationVisible && owner.isFocused()) return false;
  if (!Notification.isSupported()) return false;

  const notificationTarget = normalizeDeepLinkTarget(payload?.target);
  if (payload.kind === "completion" && payload.messageId && [...desktopNotifications.values()].some((receipt) =>
    receipt.kind === "completion" && receipt.messageId === payload.messageId
    && receipt.conversationId === notificationTarget?.conversationId)) return false;
  if (payload.requestId && [...desktopNotifications.values()].some((receipt) =>
    receipt.requestId === payload.requestId && receipt.conversationId === notificationTarget?.conversationId
    && ["turnId", "messageId", "taskId"].every((key) => receipt[key] === payload[key]))) return false;

  const notification = new Notification({
    title:
      typeof payload?.title === "string" && payload.title.trim()
        ? payload.title.trim()
        : "MiniCode",
    body: typeof payload?.body === "string" ? payload.body : "",
    silent: true,
    icon: DESKTOP_ICON_PATH,
  });
  notification.on("click", () => {
    const win = owner && !owner.isDestroyed() ? owner : windowManager.getFocusedMainWindow();
    windowManager.focusMainWindow(win);
    if (notificationTarget) dispatchDeepLink(notificationTarget, win);
  });
  notification.on("close", () => desktopNotifications.delete(notification));

  notification.show();
  void notificationSounds.playSound(desktopPreferences.getPreferences().notificationSound, app.getPath("userData"))
    .catch(error => appendDesktopLog(`[notifications] sound failed: ${error.message}`));
  desktopNotifications.set(notification, {
    kind: payload.kind,
    requestId: payload.requestId,
    conversationId: notificationTarget?.conversationId,
    turnId: payload.turnId,
    messageId: payload.messageId,
    taskId: payload.taskId,
  });
  return true;
}

// ---------------------------------------------------------------------------
// Application menu
// ---------------------------------------------------------------------------

const applicationMenuStates = new WeakMap();

function setApplicationMenuState(window, state) {
  applicationMenuStates.set(window, state);
  void dictationHotkeys.suspend(window.webContents.id, state.shortcutRecording === true, "application-menu")
    .catch(error => appendDesktopLog(`[desktop] dictation shortcut capture: ${error.message}`));
  window.webContents.setIgnoreMenuShortcuts(state.shortcutRecording === true);
  if (window === windowManager.getFocusedMainWindow()) buildApplicationMenu(window);
}

function buildApplicationMenu(owner = windowManager.getFocusedMainWindow()) {
  const locale = desktopPreferences.getPreferences().localeOverride ?? app.getLocale();
  const chinese = /^zh(?:[-_](?:cn|sg|hans.*))?$/i.test(locale);
  const label = (zh, en) => chinese ? zh : en;
  const state = owner && applicationMenuStates.get(owner);
  const targetWindow = () => owner ?? windowManager.getFocusedMainWindow();
  const send = (event) => sendWorkbenchMenuEvent(event, targetWindow());
  const browserCommand = (action) => {
    const window = targetWindow();
    return window && embeddedBrowserManager.runMenuCommand(window, webContents.getFocusedWebContents(), action);
  };
  const find = (action, event) => {
    if (!browserCommand(action)) send(event);
  };
  const editHistory = (action) => {
    if (browserCommand(action)) return;
    send(`minicode:menu:${action}-app-action`);
  };
  const stepZoom = (delta) => {
    if (browserCommand(delta === 0 ? "reset-zoom" : delta > 0 ? "zoom-in" : "zoom-out")) return;
    const window = targetWindow();
    if (window && !window.isDestroyed()) {
      window.webContents.setZoomLevel(delta === 0 ? 0 : window.webContents.getZoomLevel() + delta * 0.5);
    }
  };
  const close = () => {
    if (!browserCommand("close")) send("minicode:menu:close-tab");
  };
  const canReloadBrowser = owner && embeddedBrowserManager.canReloadVisiblePage(owner, webContents.getFocusedWebContents());
  const hidden = (title, accelerator, click) => ({
    label: title, accelerator, acceleratorWorksWhenHidden: true, visible: false, click,
  });
  const sidebarAccelerator = state?.editorFocused ? "Ctrl+B" : "Ctrl+Shift+S";
  const editorWorkspace = state?.bottomPanelToggleEnabled === true;
  const template = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
    {
      id: "minicode-menu-file",
      label: label("文件", "File"),
      submenu: [
        {
          label: label("新建窗口", "New Window"),
          visible: false, acceleratorWorksWhenHidden: true,
          click: () => {
            if (!windowManager.getMainWindow()) { void attemptAppStartup("new-window"); return; }
            void windowManager.createNewWindow().catch((error) => {
              appendDesktopLog("[desktop] new window failed: " + error.message);
              dialog.showErrorBox("New window failed", error.message);
            });
          },
        },
        {
          label: label("新聊天", "New Chat"), accelerator: "Ctrl+N",
          click: () => send("minicode:menu:new-chat"),
        },
        { label: label("新建临时聊天", "New Temporary Chat"), accelerator: "Ctrl+Shift+N", enabled: false },
        {
          label: label("新建项目…", "New Project…"), enabled: state?.newProjectEnabled === true,
          click: () => send("minicode:menu:new-project"),
        },
        hidden("New Chat", "Ctrl+Shift+O", () => send("minicode:menu:new-chat")),
        ...editorWorkspace ? [{ type: "separator" }] : [],
        {
          label: label("打开文件夹…", "Open Folder…"), accelerator: "Ctrl+O",
          visible: editorWorkspace, acceleratorWorksWhenHidden: true,
          click: () => send("minicode:menu:open-folder"),
        },
        { type: "separator" },
        { label: label("关闭", "Close"), accelerator: "Ctrl+W", click: close },
        hidden("Close", "Ctrl+F4", close),
        { type: "separator" },
        { role: "quit", label: label("退出 " + app.getName(), "Quit " + app.getName()), accelerator: "Ctrl+Q" },
      ],
    },
    {
      id: "minicode-menu-edit",
      label: label("编辑", "Edit"),
      role: "editMenu",
      submenu: [
        { label: label("撤销", "Undo"), accelerator: "CmdOrCtrl+Z", click: () => editHistory("undo") },
        { label: label("重做", "Redo"), accelerator: process.platform === "darwin" ? "Command+Shift+Z" : "Ctrl+Y", click: () => editHistory("redo") },
        { type: "separator" },
        { role: "cut", label: label("剪切", "Cut") },
        { role: "copy", label: label("复制", "Copy") },
        { role: "paste", label: label("粘贴", "Paste") },
        { role: "delete", label: label("删除", "Delete") },
        { type: "separator" },
        { role: "selectAll", label: label("全选", "Select All") },
        { type: "separator" },
        {
          label: label("设置…", "Settings…"), accelerator: "Ctrl+,",
          click: () => send("minicode:menu:settings"),
        },
      ],
    },
    {
      id: "minicode-menu-view",
      label: label("视图", "View"),
      submenu: [
        {
          label: label("显示/隐藏侧边栏", "Toggle Sidebar"), accelerator: sidebarAccelerator,
          click: () => send("minicode:menu:toggle-sidebar"),
        },
        ...state?.editorFocused ? [] : [hidden("Toggle Sidebar", "Ctrl+B", () => send("minicode:menu:toggle-sidebar"))],
        {
          label: label("显示/隐藏底部面板", "Toggle Bottom Panel"), accelerator: "Ctrl+J",
          visible: editorWorkspace, acceleratorWorksWhenHidden: true,
          enabled: state?.bottomPanelToggleEnabled === true,
          click: () => send("minicode:menu:toggle-bottom-panel"),
        },
        {
          label: label("切换摘要面板固定状态", "Toggle Pinned Summary"),
          click: () => send("minicode:menu:toggle-pinned-summary"),
        },
        {
          label: label("打开终端", "Open Terminal"), accelerator: "Ctrl+\u0060",
          visible: editorWorkspace, acceleratorWorksWhenHidden: true,
          enabled: state?.terminalEnabled === true,
          click: () => send("minicode:shortcut:terminal"),
        },
        {
          label: label("显示/隐藏导航面板", "Toggle navigation panel"), accelerator: "Ctrl+Shift+E",
          click: () => send("minicode:menu:toggle-file-tree"),
        },
        {
          label: label("显示/隐藏“变更”面板", "Toggle Changes Panel"), accelerator: "Ctrl+Alt+B",
          click: () => send("minicode:menu:toggle-side-panel"),
        },
        hidden("Open Browser Tab", "Ctrl+T", () => send("minicode:menu:open-browser-tab")),
        hidden("Focus Browser Address Bar", undefined, () => send("minicode:menu:focus-browser-address-bar")),
        { ...hidden("Reload Browser Page", "Ctrl+R", () => browserCommand("reload")), enabled: Boolean(canReloadBrowser) },
        { ...hidden("Force Reload Browser Page", "Ctrl+Shift+R", () => browserCommand("hard-reload")), enabled: Boolean(canReloadBrowser) },
        hidden("Browser Back", "Alt+Left", () => browserCommand("back")),
        hidden("Browser Forward", "Alt+Right", () => browserCommand("forward")),
        { type: "separator" },
        {
          label: label("查找", "Find"), accelerator: "Ctrl+F",
          click: () => find("open-find", "minicode:menu:find-in-thread"),
        },
        hidden("Find Next", "Ctrl+G", () => find("find-next", "minicode:menu:find-next-in-thread")),
        hidden("Find Previous", "Shift+F3", () => find("find-previous", "minicode:menu:find-previous-in-thread")),
        { type: "separator" },
        {
          label: label("返回", "Back"), accelerator: "Ctrl+[",
          click: () => send("minicode:menu:navigate-back"),
        },
        { label: label("前进", "Forward"), click: () => send("minicode:menu:navigate-forward") },
        { type: "separator" },
        {
          label: label("侧边栏中的上一项", "Previous Sidebar Item"), accelerator: "Ctrl+Alt+[",
          click: () => send("minicode:menu:previous-thread"),
        },
        hidden("Previous Sidebar Item", "Ctrl+Alt+Left", () => send("minicode:menu:previous-thread")),
        {
          label: label("下一个侧边栏项目", "Next Sidebar Item"), accelerator: "Ctrl+Alt+]",
          click: () => send("minicode:menu:next-thread"),
        },
        hidden("Next Sidebar Item", "Ctrl+Alt+Right", () => send("minicode:menu:next-thread")),
        { type: "separator" },
        {
          label: label("上一个标签页", "Previous Tab"), accelerator: "Ctrl+Shift+[",
          click: () => send("minicode:menu:previous-tab"),
        },
        hidden("Previous Tab", "Ctrl+PageUp", () => send("minicode:menu:previous-tab")),
        {
          label: label("下一个标签页", "Next Tab"), accelerator: "Ctrl+Shift+]",
          click: () => send("minicode:menu:next-tab"),
        },
        hidden("Next Tab", "Ctrl+PageDown", () => send("minicode:menu:next-tab")),
        { type: "separator" },
        { label: label("放大", "Zoom In"), accelerator: "Ctrl+Plus", click: () => stepZoom(1) },
        hidden("Zoom In", "Ctrl+=", () => stepZoom(1)),
        { label: label("缩小", "Zoom Out"), accelerator: "Ctrl+-", click: () => stepZoom(-1) },
        { label: label("实际大小", "Actual Size"), accelerator: "Ctrl+0", click: () => stepZoom(0) },
        { type: "separator" },
        {
          label: label("切换全屏", "Toggle Full Screen"), accelerator: "F11",
          click: () => {
            const window = targetWindow();
            if (window && !window.isDestroyed()) window.setFullScreen(!window.isFullScreen());
          },
        },
        hidden("Open command menu", "Ctrl+K", () => send("minicode:menu:open-command-menu")),
        hidden("Search Files…", "Ctrl+P", () => send("minicode:menu:search-files")),
      ],
    },
    ...(process.platform === "darwin" ? [{
      role: "windowMenu", label: label("窗口", "Window"),
      submenu: [{ role: "minimize" }, { role: "zoom" }, { type: "separator" }, { role: "front" }],
    }] : []),
    {
      id: "minicode-menu-help", role: "help",
      label: label("帮助", "Help"),
      submenu: [
        { label: label("文档", "Documentation"), click: () => shell.openExternal("https://developers.openai.com/codex/app") },
        {
          label: label("显示键盘快捷键", "Keyboard Shortcuts"), accelerator: "Ctrl+/",
          click: () => send("minicode:menu:open-shortcuts"),
        },
        { label: label("新功能", "What's New"), click: () => shell.openExternal("https://developers.openai.com/codex/changelog") },
        { type: "separator" },
        { label: label("故障排除", "Troubleshooting"), click: () => shell.openExternal("https://developers.openai.com/codex/app/troubleshooting") },
        { label: label("系统状态", "System Status"), click: () => shell.openExternal("https://status.openai.com") },
        { type: "separator" },
        ...updater.isAvailable() ? [{
          label: label("检查更新…", "Check for Updates…"), click: () => { void updater.check(); },
        }] : [],
        {
          label: label("关于 " + app.getName(), "About " + app.getName()),
          click: () => {
            app.setAboutPanelOptions({ applicationName: app.getName(), applicationVersion: app.getVersion() });
            app.showAboutPanel();
          },
        },
      ],
    },
  ];
  const commands = [
    ["undoAppAction", ["撤销", "Undo"]], ["redoAppAction", ["重做", "Redo"]],
    ["newWindow", ["新建窗口", "New Window"]], ["newConversation", ["新聊天", "New Chat"]], ["newProject", ["新建项目…", "New Project…"]],
    ["openFolder", ["打开文件夹…", "Open Folder…"]], ["closeTab", ["关闭", "Close"]], ["quit", ["退出 " + app.getName(), "Quit " + app.getName()]],
    ["settings", ["设置…", "Settings…"]], ["leftSidebar", ["显示/隐藏侧边栏", "Toggle Sidebar"]], ["bottomPanel", ["显示/隐藏底部面板", "Toggle Bottom Panel"]],
    ["pinnedSummary", ["切换摘要面板固定状态", "Toggle Pinned Summary"]], ["terminal", ["打开终端", "Open Terminal"]],
    ["fileTree", ["显示/隐藏导航面板", "Toggle navigation panel"]], ["sidePanel", ["显示/隐藏“变更”面板", "Toggle Changes Panel"]],
    ["browserTab", ["打开浏览器标签页", "Open Browser Tab"]], ["browserAddress", ["聚焦浏览器地址栏", "Focus Browser Address Bar"]],
    ["browserReload", ["重新加载浏览器页面", "Reload Browser Page"]], ["browserHardReload", ["Force Reload Browser Page"]],
    ["browserBack", ["Browser Back"]], ["browserForward", ["Browser Forward"]], ["find", ["查找", "Find"]], ["findNext", ["Find Next"]], ["findPrevious", ["Find Previous"]],
    ["navigateBack", ["返回", "Back"]], ["navigateForward", ["前进", "Forward"]],
    ["previousConversation", ["侧边栏中的上一项", "Previous Sidebar Item"]], ["nextConversation", ["下一个侧边栏项目", "Next Sidebar Item"]],
    ["previousTab", ["上一个标签页", "Previous Tab"]], ["nextTab", ["下一个标签页", "Next Tab"]],
    ["zoomIn", ["放大", "Zoom In"]], ["zoomOut", ["缩小", "Zoom Out"]], ["zoomReset", ["实际大小", "Actual Size"]],
    ["fullScreen", ["切换全屏", "Toggle Full Screen"]], ["commandPalette", ["Open command menu"]], ["globalSearch", ["Search Files…"]],
    ["shortcutHelp", ["显示键盘快捷键", "Keyboard Shortcuts"]],
  ];
  const commandByLabel = new Map(commands.flatMap(([action, labels]) => labels.map(title => [title, action])));
  const assigned = new Set();
  const additionalShortcuts = [];
  const applyKeymap = (items) => {
    for (const item of items) {
      const action = commandByLabel.get(item.label);
      if (action) {
        if (!assigned.has(action)) {
          assigned.add(action);
          item.id = "minicode-shortcut-" + action;
          if (state?.accelerators) item.accelerator = state.accelerators[action] ?? undefined;
          if (action === "closeTab" && state?.editorFocused && state.accelerators?.closeEditor) item.accelerator = state.accelerators.closeEditor;
          if (action === "leftSidebar" && state?.editorFocused && state.accelerators?.leftSidebar
            && state.accelerators.leftSidebar === state.accelerators.saveAllFiles) {
            item.accelerator = state.acceleratorBindings
              ? state.acceleratorBindings.leftSidebar.find(chord => !state.acceleratorBindings.saveAllFiles.includes(chord))
              : state.shortcutOverrides.includes("leftSidebar") ? undefined : "Ctrl+B";
          }
          for (const accelerator of (state?.acceleratorBindings?.[action] || []).slice(1)) {
            if (accelerator === item.accelerator || action === "leftSidebar" && state.editorFocused && state.acceleratorBindings.saveAllFiles.includes(accelerator)) continue;
            additionalShortcuts.push({ ...item, id: undefined, accelerator, visible: false, acceleratorWorksWhenHidden: true });
          }
        } else if (state?.acceleratorBindings || state?.shortcutOverrides?.includes(action) || state?.accelerators && !state.accelerators[action]) {
          item.accelerator = undefined;
        }
      }
      if (item.submenu) applyKeymap(item.submenu);
    }
  };
  applyKeymap(template);
  template.find(item => item.id === "minicode-menu-file").submenu.push(...additionalShortcuts);
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function executeApplicationMenuAction(window, action) {
  if (action === "nativeTextUndo" || action === "nativeTextRedo") {
    window.webContents[action === "nativeTextUndo" ? "undo" : "redo"]();
    return true;
  }
  buildApplicationMenu(window);
  const item = Menu.getApplicationMenu().getMenuItemById("minicode-shortcut-" + action);
  if (!item) throw new TypeError("Unknown native menu command: " + action);
  if (!item.enabled) return false;
  if (item.role === "quit") app.quit();
  else item.click(item, window, {});
  return true;
}

// ---------------------------------------------------------------------------
// Startup failure window
// ---------------------------------------------------------------------------

function closeStartupFailureWindow() {
  if (!startupFailureWindow || startupFailureWindow.isDestroyed()) {
    startupFailureWindow = null;
    return;
  }
  startupFailureWindow.close();
  startupFailureWindow = null;
}

function broadcastStartupFailureState() {
  if (!startupFailureWindow || startupFailureWindow.isDestroyed()) return;
  startupFailureWindow.webContents.send("minicode:startup:state", startupFailureState);
}

async function createStartupFailureWindow(errorPayload) {
  startupFailureState = {
    ...startupFailureState,
    ...errorPayload,
    logsPath: errorPayload.logsPath || utils.getDesktopLogPath(),
  };

  if (startupFailureWindow && !startupFailureWindow.isDestroyed()) {
    startupFailureWindow.show();
    startupFailureWindow.focus();
    broadcastStartupFailureState();
    return startupFailureWindow;
  }

  startupFailureWindow = new BrowserWindow({
    width: 720,
    height: 560,
    minWidth: 640,
    minHeight: 480,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: "#f3f6fa",
    title: "MiniCode Startup Recovery",
    icon: DESKTOP_ICON_PATH,
    webPreferences: {
      preload: STARTUP_ERROR_PRELOAD_FILE,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  startupFailureWindow.on("ready-to-show", () => {
    startupFailureWindow?.show();
    broadcastStartupFailureState();
  });

  startupFailureWindow.on("closed", () => {
    startupFailureWindow = null;
  });

  await startupFailureWindow.loadFile(STARTUP_ERROR_FILE);
  return startupFailureWindow;
}

// ---------------------------------------------------------------------------
// Timing configuration
// ---------------------------------------------------------------------------

function readPositiveIntFromEnv(name, fallback) {
  const rawValue = process.env[name];
  if (!rawValue) return fallback;
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
  return Math.floor(parsed);
}

const BACKEND_STARTUP_TIMEOUT_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_STARTUP_TIMEOUT_MS", 90000,
);
const BACKEND_RESTART_INITIAL_DELAY_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_RESTART_INITIAL_DELAY_MS", 800,
);
const BACKEND_RESTART_MAX_DELAY_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_RESTART_MAX_DELAY_MS", 20000,
);

// ---------------------------------------------------------------------------
// Module wiring — pass dependencies to each extracted module
// ---------------------------------------------------------------------------

const trustedWorkspaceRoots = new Set([
  path.resolve(process.cwd()),
  getAppRoot(),
]);
const trustedWorkspaceLedgerPath = path.join(
  app.getPath("userData"),
  "data",
  "trusted_workspaces.json",
);
security.init({
  initialRoots: trustedWorkspaceRoots,
  readOnlyRoots: [path.join(app.getPath("userData"), "data", "tool-results")],
  userOutputRoots: [
    app.getPath("desktop"),
    app.getPath("documents"),
    app.getPath("downloads"),
  ],
  trustedRootsFile: trustedWorkspaceLedgerPath,
  logger: appendDesktopLog,
});

backendSidecar.init({
  writeStdout,
  writeStderr,
  getAppRoot,
  sleep: utils.sleep,
  restartBackend: restartManagedBackend,
  config: {
    manageBackend: MANAGE_BACKEND,
    pythonCommand: PYTHON_COMMAND,
    githubCliCommand: GITHUB_CLI_COMMAND,
    backendHost: BACKEND_HOST,
    get resolvedBackendPort() { return resolvedBackendPort; },
    get resolvedApiBaseUrl() { return resolvedApiBaseUrl; },
    get resolvedWsBaseUrl() { return resolvedWsBaseUrl; },
    get resolvedFrontendUrl() { return resolvedFrontendUrl; },
    runtimeToken: RUNTIME_TOKEN,
    stateRoot: app.getPath("userData"),
    appResourcesDir: app.isPackaged ? process.resourcesPath : getAppRoot(),
    editorLanguageServicesDir: app.isPackaged ? path.join(process.resourcesPath, "language-services") : path.join(__dirname, "language-services"),
    desktopDir: app.getPath("desktop"),
    documentsDir: app.getPath("documents"),
    downloadsDir: app.getPath("downloads"),
    restartInitialDelayMs: BACKEND_RESTART_INITIAL_DELAY_MS,
    restartMaxDelayMs: BACKEND_RESTART_MAX_DELAY_MS,
    restartJitterRatio: 0.15,
  },
});

cdpBridge.init({
  logger: appendDesktopLog,
});

ptyManager.init({
  pty,
  sanitizedPtyEnv: utils.sanitizedPtyEnv,
  appendDesktopLog,
  getWindowByInstanceId: windowManager.getWindowByInstanceId,
  assertTrustedPath: security.assertTrustedPath,
});

windowManager.init({
  appendDesktopLog,
  isHttpUrl: utils.isHttpUrl,
  getRendererAdditionalArguments,
  preloadFile: PRELOAD_FILE,
  desktopIconPath: DESKTOP_ICON_PATH,
  frontendDevUrl: FRONTEND_DEV_URL,
  getAppRoot,
  onMainWindowCreated: (mainWindow, record) => {
    mcpAppSandbox.attachOwner(mainWindow.webContents);
    record.dictationSenderId = mainWindow.webContents.id;
    if (!record.remoteHostId) record.runtime = getBackendRuntimeConfig();
    else installRendererCspHeaders(mainWindow.webContents.session);
    updater.registerWindow(record.windowInstanceId);
    mainWindow.on("focus", () => buildApplicationMenu(mainWindow));
    mainWindow.webContents.on("did-navigate", () => {
      dictationHotkeys.releaseWindow(record.dictationSenderId);
      updater.invalidateActivity(record.windowInstanceId, "activity.renderer_navigation");
      petActionBridge.release(record.windowInstanceId);
    });
    mainWindow.webContents.on("render-process-gone", () => {
      dictationHotkeys.releaseWindow(record.dictationSenderId);
      updater.invalidateActivity(record.windowInstanceId, "activity.renderer_gone");
      petActionBridge.release(record.windowInstanceId);
    });
  },
  onMainWindowClosed: (_mainWindow, record) => {
    updater.unregisterWindow(record.windowInstanceId);
    petManager?.releaseActivity(record.windowInstanceId);
    petActionBridge.release(record.windowInstanceId);
    dictationHotkeys.releaseWindow(record.dictationSenderId);
    void releaseWindowResources(record);
    if (pendingDeepLink?.windowInstanceId === record.windowInstanceId) pendingDeepLink.windowInstanceId = null;
  },
  onMainWindowCloseCancelled: () => { appQuitRequested = false; backendSidecar.resetStopRequested(); },
  onDiagnosticIncident: recordDiagnosticIncident,
});

embeddedBrowserManager.init({
  appendDesktopLog,
  getLocale: () => desktopPreferences.getPreferences().localeOverride ?? app.getLocale(),
  getShortcutPreferences: desktopPreferences.getPreferences,
  getWindowByInstanceId: windowManager.getWindowByInstanceId,
  getMainWindows: windowManager.getMainWindows,
  assessBrowserNavigationPolicy: cdpBridge.assessBrowserNavigationPolicy,
  browserHistory,
  browserPersonalData,
  onMenuStateChanged: (window) => {
    if (window === windowManager.getFocusedMainWindow()) buildApplicationMenu(window);
  },
});

embeddedBrowserBridge.init({
  manager: embeddedBrowserManager,
  getWindowRecordByInstanceId: windowManager.getWindowRecordByInstanceId,
  token: EMBEDDED_BROWSER_TOKEN,
  endpoint: MANAGE_BACKEND ? "" : process.env.MINICODE_EMBEDDED_BROWSER_ENDPOINT,
  appendDesktopLog,
});

ipcHandlers.init({
  dictationHotkeys,
  getBackendRuntimeConfig,
  browserPersonalData,
  getMcpAppSandbox: () => mcpAppSandbox,
  getPetManager: async () => { await petInitialization; return petManager; },
  refreshPets: async () => {
    petInitialization = petManager.initialize();
    const state = await petInitialization;
    for (const window of windowManager.getMainWindows()) window.webContents.send("minicode:pets:refreshed");
    return state;
  },
  completePetAction: (ownerId, result) => petActionBridge.complete(ownerId, result),
  getRemoteDesktop: () => {
    if (!remoteDesktop) remoteDesktop = new RemoteDesktop({ userData: app.getPath("userData"),
      bridgeEndpoint: process.env.MINICODE_EMBEDDED_BROWSER_ENDPOINT, windowManager,
      browserBridge: embeddedBrowserBridge, log: appendDesktopLog });
    return remoteDesktop;
  },
  desktopPreferences,
  onLocaleChanged: buildApplicationMenu,
  browserHistory,
  getMainWindows: windowManager.getMainWindows,
  browserProfileImport: createBrowserProfileImporter({ history: browserHistory }),
  createNewWindow: windowManager.createNewWindow,
  focusMainWindow: windowManager.focusMainWindow,
  requestCloseWindow: windowManager.requestCloseWindow,
  popupApplicationMenu: (key, window) => {
    buildApplicationMenu(window);
    return popupApplicationMenu(Menu.getApplicationMenu(), key, window);
  },
  setApplicationMenuState,
  executeApplicationMenuAction,
  setupWindowsSandbox: () => setupWindowsSandbox({
    scriptPath: app.isPackaged
      ? path.join(process.resourcesPath, "windows-sandbox", "setup-desktop-user-sandbox.ps1")
      : path.join(__dirname, "scripts", "setup-desktop-user-sandbox.ps1"),
    apiBaseUrl: resolvedApiBaseUrl,
    runtimeToken: RUNTIME_TOKEN,
    shellProcessId: process.pid,
    powershellCommand: process.platform === "win32"
      ? path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe") : "",
  }),
  getWindowForSender: windowManager.getWindowForSender,
  getWindowRecord: windowManager.getWindowRecord,
  captureUnloadIntent: windowManager.captureUnloadIntent,
  resumeUnload: windowManager.resumeUnload,
  getStartupFailureWindow: () => startupFailureWindow,
  showDesktopNotification,
  hideDesktopNotifications,
  dispatchDeepLink,
  acknowledgeDeepLink,
  getPendingDeepLink,
  attemptAppStartup,
  get startupFailureState() { return startupFailureState; },
  getDesktopLogPath: utils.getDesktopLogPath,
  appendDesktopLog,
  exportDesktopDiagnostics,
  discoverChromeCdp: cdpBridge.discoverChromeCdp,
  assessBrowserNavigationPolicy: cdpBridge.assessBrowserNavigationPolicy,
  captureChromeTargetScreenshot: cdpBridge.captureChromeTargetScreenshot,
  navigateChromeTarget: cdpBridge.navigateChromeTarget,
  clickChromeTarget: cdpBridge.clickChromeTarget,
  typeIntoChromeTarget: cdpBridge.typeIntoChromeTarget,
  embeddedBrowserManager,
  ptyManager,
  updater,
  getRuntimeConfig: getWindowRuntimeConfig,
  getLastPickedWorkspaceRoot: () => lastPickedWorkspaceRoot,
  setLastPickedWorkspaceRoot: (v) => { lastPickedWorkspaceRoot = v; },
});

// ---------------------------------------------------------------------------
// App startup orchestration
// ---------------------------------------------------------------------------

async function attemptAppStartup(source = "startup") {
  if (rollbackPreparationInProgress) return false;
  if (startupRetryInFlight) return false;

  startupRetryInFlight = true;
  appendDesktopLog(`[desktop] startup attempt (${source})`);
  try {
    if (desktopCleanupFailed) await drainDesktopResources();
    backendSidecar.resetStopRequested();
    const externalContract = MANAGE_BACKEND ? null : externalBackendHostContract();
    // Desktop WebSocket admission proves its window binding through this
    // native endpoint before the backend can publish a connected session.
    process.env.MINICODE_EMBEDDED_BROWSER_ENDPOINT = await embeddedBrowserBridge.start();
    process.env.MINICODE_EMBEDDED_BROWSER_TOKEN = EMBEDDED_BROWSER_TOKEN;
    if (MANAGE_BACKEND) {
      await launchManagedBackend();
    } else {
      const runtime = await resolveBackendRuntime();
      await backendSidecar.waitForBackendReady(runtime.apiBaseUrl, BACKEND_STARTUP_TIMEOUT_MS);
      await verifyExternalBackendHostContract(runtime.apiBaseUrl, RUNTIME_TOKEN, externalContract);
    }
    if (!petManager) {
      petManager = createPetManager({
        configRoot: process.env.MINICODE_CONFIG_DIR || path.join(app.getPath("home"), ".minicode"),
        builtinAssetRoot: app.isPackaged
          ? path.join(process.resourcesPath, "frontend", "dist", "pets")
          : path.join(getAppRoot(), "frontend", "public", "pets"),
        getMainWindow: windowManager.getFocusedMainWindow,
        getLocale: () => desktopPreferences.getPreferences().localeOverride ?? app.getLocale(),
        onOpenSettings: async window => {
          const target = window || await windowManager.createMainWindow();
          sendWorkbenchMenuEvent("minicode:menu:open-pets-settings", target);
        },
        onOpenMain: async () => {
          const target = windowManager.getFocusedMainWindow() || await windowManager.createMainWindow();
          sendWorkbenchMenuEvent("minicode:menu:focus-composer", target);
        },
        onOpenTask: async (ownerId, conversationId) => {
          const target = windowManager.getWindowByInstanceId(ownerId) || await windowManager.createMainWindow();
          dispatchDeepLink({ kind: "conversation", conversationId }, target);
        },
        onTaskAction: (ownerId, taskId, actionId) => petActionBridge.request(ownerId, taskId, actionId),
        onTaskFollowup: (ownerId, taskId, prompt) => petActionBridge.requestFollowup(ownerId, taskId, prompt),
        onStateChanged: state => {
          for (const window of windowManager.getMainWindows()) window.webContents.send("minicode:pets:stateChanged", state);
        },
      });
    }
    petInitialization = petManager.initialize();
    const initializedPets = petManager;
    // The workspace can open while this optional local feature reports its
    // own load error. Pet requests retain the rejected initialization receipt.
    const mainWindowReady = windowManager.createMainWindow();
    void petInitialization.then(async () => {
      await mainWindowReady;
      if (petManager === initializedPets) return initializedPets.restoreMini();
    }).catch(error => appendDesktopLog(`[pets] restore failed: ${error.message}`));
    await mainWindowReady;
    generalMaintenance?.dispose();
    generalMaintenance = startGeneralMaintenance({
      getPreferences: desktopPreferences.getPreferences,
      getIdleSeconds: () => powerMonitor.getSystemIdleTime(),
      runMaintenance: async payload => {
        const response = await fetch(`${resolvedApiBaseUrl}/api/settings/general/maintenance`, {
          method: "POST", headers: { "Content-Type": "application/json", "X-MiniCode-Token": RUNTIME_TOKEN },
          body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error(await response.text());
        return response.json();
      },
      onError: error => appendDesktopLog(`[history maintenance] ${error.message}`),
    });
    closeStartupFailureWindow();
    appendDesktopLog(`[desktop] startup attempt succeeded (${source})`);
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendDesktopLog(`[desktop] startup attempt failed (${source}): ${message}`);
    await createStartupFailureWindow(
      serializeError(error, "MiniCode could not reach the backend or load the app shell."),
    );
    return false;
  } finally {
    startupRetryInFlight = false;
  }
}

// ---------------------------------------------------------------------------
// App event handlers
// ---------------------------------------------------------------------------

app.on("second-instance", (_event, argv) => {
  const deepLink = argv.find(
    (arg) => typeof arg === "string" && arg.startsWith("minicode://"),
  );
  if (deepLink) {
    dispatchDeepLink(deepLink);
  }
  windowManager.focusMainWindow(windowManager.getFocusedMainWindow());
});

app.on("open-url", (event, url) => {
  event.preventDefault();
  dispatchDeepLink(url);
});

let quitCleanupComplete = false;
let quitCleanupStarted = false;
let appQuitRequested = false;
let rollbackPreparationInProgress = false;

async function drainDesktopResources() {
  try {
    await dictationHotkeys.dispose();
    generalMaintenance?.dispose();
    generalMaintenance = null;
    windowManager.clearWindowStateSaveTimer();
    windowManager.persistWindowState();
    const pending = Array.from(windowCleanupTasks, ([record, task]) =>
      desktopCleanupFailed && record.cleanupError ? releaseWindowResources(record, true) : task);
    await Promise.all(pending);
    await ptyManager.killAllSessions();
    petManager?.dispose();
    petManager = null;
    petInitialization = null;
    if (remoteDesktop) await remoteDesktop.close();
    await backendSidecar.stopBackendSidecar();
    if (backendSidecar.getBackendProcess()) throw new Error("The backend is still running; rollback data cannot be prepared.");
    const bridgeStop = embeddedBrowserBridge.stop();
    try {
      embeddedBrowserManager.disposeAll();
    } finally {
      await bridgeStop;
    }
    desktopCleanupFailed = false;
  } catch (error) {
    desktopCleanupFailed = true;
    throw error;
  }
}

async function prepareAutomaticRollback() {
  rollbackPreparationInProgress = true;
  backendSidecar.requestBackendStop();
  try {
    await closeWindowsAfterDraftCommit(windowManager.getMainWindows());
    await drainDesktopResources();
    const pythonCommand = app.isPackaged && process.platform === "win32"
      ? path.join(process.resourcesPath, "python-runtime", "python.exe") : PYTHON_COMMAND;
    return await updater.prepareRollbackData({ pythonCommand, backendRoot: getAppRoot(), stateRoot: app.getPath("userData") });
  } catch (error) {
    rollbackPreparationInProgress = false;
    throw error;
  }
}
app.on("before-quit", (event) => {
  if (quitCleanupComplete) return;
  event.preventDefault();
  if (rollbackPreparationInProgress) return;
  appQuitRequested = true;
  backendSidecar.requestBackendStop();
  const windows = windowManager.getMainWindows();
  if (windows.length > 0) {
    for (const window of windows) window.close();
    return;
  }
  if (quitCleanupStarted) return;
  quitCleanupStarted = true;
  void drainDesktopResources().then(() => {
    quitCleanupComplete = true;
    app.quit();
  }).catch(async (error) => {
    quitCleanupStarted = false;
    appendDesktopLog(`[desktop] shutdown failed: ${error.message}`);
    await createStartupFailureWindow(serializeError(error, "MiniCode could not finish shutting down its owned resources."));
  });
});

app.on("window-all-closed", () => {
  if (rollbackPreparationInProgress) return;
  if (appQuitRequested || process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", async () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    await attemptAppStartup("activate");
  }
});

app.on("child-process-gone", (_event, details) => {
  recordDiagnosticIncident("child-process-gone", {
    type: details?.type,
    reason: details?.reason,
    exitCode: details?.exitCode,
    serviceName: details?.serviceName,
    name: details?.name,
  });
});

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

app.whenReady().then(async () => {
  app.setAppUserModelId("MiniCode.Desktop");
  mcpAppSandbox = createMcpAppSandbox({ session });
  installRendererCspHeaders();
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient("minicode", process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient("minicode");
  }
  buildApplicationMenu();
  ipcHandlers.registerIpcHandlers();
  appendDesktopLog("[desktop] app ready");
  const initialDeepLink = process.argv.find(
    (arg) => typeof arg === "string" && arg.startsWith("minicode://"),
  );
  if (initialDeepLink) dispatchDeepLink(initialDeepLink);
  let rollbackLaunched;
  try {
    rollbackLaunched = await updater.init({
      app,
      getMainWindows: windowManager.getMainWindows,
      getWindowRecord: windowManager.getWindowRecord,
      logger: appendDesktopLog,
      getActivePtySessions: () => ptyManager.listActiveSessions(),
      getActiveDownloadIds: embeddedBrowserManager.getActiveDownloadIds,
      prepareRollback: prepareAutomaticRollback,
      completeRollback: () => { rollbackPreparationInProgress = false; },
    });
  } catch (error) {
    await createStartupFailureWindow(serializeError(error, "MiniCode could not safely prepare the previous version. Current user content was preserved."));
    return;
  }
  if (rollbackLaunched) return;
  const startupSucceeded = await attemptAppStartup("when-ready");
  if (startupSucceeded) updater.markHealthy();
});

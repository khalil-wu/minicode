const { app, BrowserWindow, crashReporter, dialog, Menu, Notification, session, shell } = require("electron");
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
const ipcHandlers = require("./ipc-handlers");
const updater = require("./updater");
const crashReporting = require("./crash-reporter");
const { setupWindowsSandbox } = require("./windows-sandbox-setup");
const { popupApplicationMenu } = require("./menu-popup");

// html.to.design capture is an explicitly enabled development aid. Keep the
// dependency out of the packaged startup path and save captures locally.
const FIGMA_CAPTURE_ENABLED =
  !app.isPackaged && process.env.MINICODE_ENABLE_FIGMA_CAPTURE === "1";
let figmaCaptureSdk = null;
if (FIGMA_CAPTURE_ENABLED) {
  try {
    figmaCaptureSdk = require("@divriots/h2d-electron-sdk");
    figmaCaptureSdk.mountCapture(app);
  } catch (error) {
    console.warn("[desktop] html.to.design capture could not be initialized", error);
  }
}

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
const EMBEDDED_BROWSER_TOKEN = crypto.randomBytes(32).toString("hex");

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
  "connect-src 'self' http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:* https://localhost:* https://127.0.0.1:* wss://localhost:* wss://127.0.0.1:*",
  "frame-src 'self' data: blob: http://localhost:* http://127.0.0.1:*",
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

function installRendererCspHeaders() {
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
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

async function launchManagedBackend() {
  return backendSidecar.launchBackendSidecar({
    resolveRuntime: resolveBackendRuntime,
    getRuntime: getBackendRuntimeConfig,
    timeoutMs: BACKEND_STARTUP_TIMEOUT_MS,
  });
}

function publishBackendRuntimeChange(previousRuntime, nextRuntime) {
  if (
    previousRuntime.apiBaseUrl === nextRuntime.apiBaseUrl
    && previousRuntime.wsBaseUrl === nextRuntime.wsBaseUrl
  ) {
    return;
  }
  const win = windowManager.getMainWindow();
  if (!win || win.isDestroyed()) return;
  win.webContents.send("minicode:runtime:changed", {
    revision: nextRuntime.revision,
    apiBaseUrl: nextRuntime.apiBaseUrl,
    wsBaseUrl: nextRuntime.wsBaseUrl,
  });
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

function dispatchDeepLink(target) {
  const normalizedTarget = normalizeDeepLinkTarget(target);
  if (!normalizedTarget) return false;

  pendingDeepLink = { id: crypto.randomUUID(), target: normalizedTarget };
  const win = windowManager.getMainWindow();
  if (win && !win.isDestroyed()) {
    windowManager.focusMainWindow();
    win.webContents.send("minicode:deep-link", pendingDeepLink);
  }
  return true;
}

function acknowledgeDeepLink(id) {
  if (!pendingDeepLink || pendingDeepLink.id !== String(id || "")) return false;
  pendingDeepLink = null;
  return true;
}

function sendWorkbenchMenuEvent(channel) {
  const win = windowManager.getMainWindow();
  if (!win || win.isDestroyed()) return false;
  win.webContents.send(channel);
  windowManager.focusMainWindow();
  return true;
}

function showDesktopNotification(payload) {
  if (!Notification.isSupported()) return false;

  const notification = new Notification({
    title:
      typeof payload?.title === "string" && payload.title.trim()
        ? payload.title.trim()
        : "MiniCode",
    body: typeof payload?.body === "string" ? payload.body : "",
    silent: false,
    icon: DESKTOP_ICON_PATH,
  });
  const notificationTarget = normalizeDeepLinkTarget(payload?.target);

  notification.on("click", () => {
    windowManager.focusMainWindow();
    if (notificationTarget) dispatchDeepLink(notificationTarget);
  });

  notification.show();
  return true;
}

async function captureCurrentWindowForFigma() {
  const win = BrowserWindow.getFocusedWindow() || windowManager.getMainWindow();
  if (!figmaCaptureSdk || !win || win.isDestroyed()) {
    dialog.showErrorBox(
      "Figma capture unavailable",
      "Start MiniCode with MINICODE_ENABLE_FIGMA_CAPTURE=1 and try again.",
    );
    return;
  }

  try {
    const { bytes, filename } = await figmaCaptureSdk.captureWebContents(win.webContents);
    const suggestedName = `${filename || `minicode-${Date.now()}`}.h2d`;
    const result = await dialog.showSaveDialog(win, {
      title: "Save current window for Figma",
      defaultPath: path.join(app.getPath("documents"), suggestedName),
      filters: [{ name: "html.to.design capture", extensions: ["h2d"] }],
      properties: ["createDirectory", "showOverwriteConfirmation"],
    });
    if (result.canceled || !result.filePath) return;
    const outputPath = result.filePath.toLowerCase().endsWith(".h2d")
      ? result.filePath
      : `${result.filePath}.h2d`;
    await fs.promises.writeFile(outputPath, Buffer.from(bytes));
    appendDesktopLog(`[desktop] Figma capture saved: ${outputPath}`);
    shell.showItemInFolder(outputPath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendDesktopLog(`[desktop] Figma capture failed: ${message}`);
    dialog.showErrorBox("Figma capture failed", message);
  }
}

// ---------------------------------------------------------------------------
// Application menu
// ---------------------------------------------------------------------------

function buildApplicationMenu() {
  const template = [
    {
      id: "minicode-menu-file",
      label: "文件",
      submenu: [
        {
          label: "新建窗口",
          accelerator: "Ctrl+Shift+N",
          click: () => { void windowManager.createMainWindow(); },
        },
        {
          label: "新建聊天",
          accelerator: "Ctrl+N",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:new-chat"); },
        },
        {
          label: "快速聊天",
          accelerator: "Alt+Ctrl+N",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:quick-chat"); },
        },
        {
          label: "打开文件夹…",
          accelerator: "Ctrl+O",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:open-folder"); },
        },
        {
          label: "扩展市场",
          accelerator: "Ctrl+Shift+X",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:extensions-marketplace"); },
        },
        {
          label: "设置…",
          accelerator: "Ctrl+,",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:settings"); },
        },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ],
    },
    {
      id: "minicode-menu-edit",
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      id: "minicode-menu-view",
      label: "视图",
      submenu: [
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { role: "resetZoom", label: "恢复实际大小" },
        { type: "separator" },
        {
          label: "切换侧边栏",
          accelerator: "Ctrl+B",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:toggle-sidebar"); },
        },
        {
          label: "切换上下文面板",
          accelerator: "Ctrl+\\",
          click: () => { sendWorkbenchMenuEvent("minicode:menu:toggle-context"); },
        },
        {
          label: "切换终端",
          accelerator: "Ctrl+`",
          click: () => { sendWorkbenchMenuEvent("minicode:shortcut:terminal"); },
        },
        {
          label: "重新加载",
          accelerator: "F5",
          click: () => {
            const win = windowManager.getMainWindow();
            if (win && !win.isDestroyed()) win.reload();
          },
        },
        { role: "forceReload", label: "强制重新加载" },
        { role: "toggleDevTools", label: "切换开发者工具" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
        ...(FIGMA_CAPTURE_ENABLED
          ? [
              { type: "separator" },
              {
                label: "捕获当前窗口到 Figma…",
                accelerator: "Ctrl+Shift+F12",
                click: () => { void captureCurrentWindowForFigma(); },
              },
            ]
          : []),
      ],
    },
    {
      label: "窗口",
      submenu: [{ role: "minimize", label: "最小化" }, { role: "close", label: "关闭窗口" }],
    },
    {
      id: "minicode-menu-help",
      label: "帮助",
      submenu: [
        {
          label: "显示桌面日志",
          click: () => {
            const logPath = utils.getDesktopLogPath();
            appendDesktopLog("[desktop] reveal log requested");
            shell.showItemInFolder(logPath);
          },
        },
        {
          label: "导出诊断信息",
          click: () => {
            const result = exportDesktopDiagnostics();
            shell.showItemInFolder(result.path);
          },
        },
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
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
  getMainWindow: () => windowManager.getMainWindow(),
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
  onMainWindowCreated: (mainWindow) => {
    closeStartupFailureWindow();
    updater.invalidateActivity("activity.renderer_created");
    mainWindow.webContents.on("did-start-navigation", (_event, _url, isInPlace, isMainFrame) => {
      if (isMainFrame && !isInPlace) updater.invalidateActivity("activity.renderer_navigation");
    });
    mainWindow.webContents.on("render-process-gone", () => {
      updater.invalidateActivity("activity.renderer_gone");
    });
    mainWindow.on("closed", () => {
      updater.invalidateActivity("activity.renderer_closed");
    });
  },
  onDiagnosticIncident: recordDiagnosticIncident,
});

embeddedBrowserManager.init({
  appendDesktopLog,
  getMainWindow: () => windowManager.getMainWindow(),
  assessBrowserNavigationPolicy: cdpBridge.assessBrowserNavigationPolicy,
});

embeddedBrowserBridge.init({
  manager: embeddedBrowserManager,
  token: EMBEDDED_BROWSER_TOKEN,
  appendDesktopLog,
});

ipcHandlers.init({
  popupApplicationMenu: (key) => popupApplicationMenu(Menu.getApplicationMenu(), key, windowManager.getMainWindow()),
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
  getMainWindow: () => windowManager.getMainWindow(),
  getStartupFailureWindow: () => startupFailureWindow,
  showDesktopNotification,
  dispatchDeepLink,
  acknowledgeDeepLink,
  getPendingDeepLink: () => pendingDeepLink,
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
  getRuntimeConfig: getBackendRuntimeConfig,
  getLastPickedWorkspaceRoot: () => lastPickedWorkspaceRoot,
  setLastPickedWorkspaceRoot: (v) => { lastPickedWorkspaceRoot = v; },
});

// ---------------------------------------------------------------------------
// App startup orchestration
// ---------------------------------------------------------------------------

async function attemptAppStartup(source = "startup") {
  if (startupRetryInFlight) return false;

  startupRetryInFlight = true;
  appendDesktopLog(`[desktop] startup attempt (${source})`);
  try {
    backendSidecar.resetStopRequested();
    if (MANAGE_BACKEND) {
      await launchManagedBackend();
    } else {
      const runtime = await resolveBackendRuntime();
      await backendSidecar.waitForBackendReady(runtime.apiBaseUrl, BACKEND_STARTUP_TIMEOUT_MS);
    }
    await windowManager.createMainWindow();
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
  windowManager.focusMainWindow();
});

app.on("open-url", (event, url) => {
  event.preventDefault();
  dispatchDeepLink(url);
});

let quitCleanupComplete = false;
let quitCleanupStarted = false;
app.on("before-quit", (event) => {
  if (quitCleanupComplete) return;
  event.preventDefault();
  if (quitCleanupStarted) return;
  quitCleanupStarted = true;
  windowManager.clearWindowStateSaveTimer();
  windowManager.persistWindowState();
  void Promise.allSettled([
    ptyManager.killAllSessions(),
    backendSidecar.stopBackendSidecar(),
    (async () => {
      const bridgeStop = embeddedBrowserBridge.stop();
      try {
        embeddedBrowserManager.disposeAll();
      } finally {
        await bridgeStop;
      }
    })(),
  ]).finally(() => {
    quitCleanupComplete = true;
    app.quit();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
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
  installRendererCspHeaders();
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient("minicode", process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient("minicode");
  }
  buildApplicationMenu();
  ipcHandlers.registerIpcHandlers();
  // The embedded browser bridge is an optional control surface.  A listen
  // failure here must be visible evidence and must not silently kill the
  // rest of startup (updater + backend + window never ran when this await
  // rejected, leaving the app hung with no UI).
  let embeddedBrowserEndpoint = "";
  try {
    embeddedBrowserEndpoint = await embeddedBrowserBridge.start();
  } catch (error) {
    appendDesktopLog(
      `[desktop] embedded browser bridge failed to start: ${
        error instanceof Error ? error.stack || error.message : String(error)
      }`,
    );
    appendDesktopLog("[desktop] continuing startup without the embedded browser bridge");
  }
  process.env.MINICODE_EMBEDDED_BROWSER_ENDPOINT = embeddedBrowserEndpoint;
  process.env.MINICODE_EMBEDDED_BROWSER_TOKEN = EMBEDDED_BROWSER_TOKEN;
  appendDesktopLog("[desktop] app ready");
  const initialDeepLink = process.argv.find(
    (arg) => typeof arg === "string" && arg.startsWith("minicode://"),
  );
  if (initialDeepLink) dispatchDeepLink(initialDeepLink);
  const rollbackLaunched = await updater.init({
    app,
    getMainWindow: () => windowManager.getMainWindow(),
    logger: appendDesktopLog,
    getActivePtySessions: () => ptyManager.listActiveSessions(),
  });
  if (rollbackLaunched) return;
  const startupSucceeded = await attemptAppStartup("when-ready");
  if (startupSucceeded) updater.markHealthy();
});

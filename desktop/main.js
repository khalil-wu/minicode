const { app, BrowserWindow, dialog, ipcMain, Menu, Notification, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const crypto = require("node:crypto");
const net = require("node:net");
const path = require("node:path");
let pty;
try {
  pty = require("node-pty");
} catch (e) {
  // node-pty might fail to load if not built properly
}

const PRELOAD_FILE = path.join(__dirname, "preload.js");
const STARTUP_ERROR_FILE = path.join(__dirname, "startup-error.html");
const STARTUP_ERROR_PRELOAD_FILE = path.join(__dirname, "startup-error-preload.js");

const BACKEND_HOST = process.env.MINICODE_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.MINICODE_BACKEND_PORT || "8000");
let resolvedBackendPort = BACKEND_PORT;
let resolvedApiBaseUrl =
  process.env.MINICODE_API_BASE_URL || `http://${BACKEND_HOST}:${BACKEND_PORT}`;
let resolvedWsBaseUrl =
  process.env.MINICODE_WS_BASE_URL || `ws://${BACKEND_HOST}:${BACKEND_PORT}`;
const FRONTEND_DEV_URL = (process.env.MINICODE_FRONTEND_URL || "").trim();
let resolvedFrontendUrl = FRONTEND_DEV_URL;
const MANAGE_BACKEND = process.env.MINICODE_SKIP_BACKEND !== "1";
const RUNTIME_TOKEN =
  process.env.MINICODE_RUNTIME_TOKEN ||
  crypto.randomBytes(32).toString("hex");

const BINARY_READ_DENY_EXTENSIONS = new Set([
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".avif",
  ".ico",
  ".zip",
  ".rar",
  ".7z",
  ".tar",
  ".gz",
  ".doc",
  ".docx",
  ".ppt",
  ".pptx",
  ".xls",
  ".xlsx",
  ".exe",
  ".dll",
  ".bin",
  ".wasm",
]);

function isProbablyTextBuffer(buffer, filePath) {
  const extension = path.extname(filePath).toLowerCase();
  if (BINARY_READ_DENY_EXTENSIONS.has(extension)) {
    return false;
  }
  const sample = buffer.subarray(0, Math.min(buffer.length, 4096));
  if (sample.includes(0)) {
    return false;
  }
  const decoded = sample.toString("utf8");
  return !decoded.includes("\uFFFD");
}

function hashFileContent(content) {
  return crypto.createHash("sha256").update(String(content), "utf8").digest("hex");
}

async function countDirEntries(dirPath, limit) {
  let count = 0;
  const stack = [dirPath];
  while (stack.length > 0 && count <= limit) {
    const dir = stack.pop();
    let entries;
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      count++;
      if (count > limit) return count;
      if (entry.isDirectory()) {
        stack.push(path.join(dir, entry.name));
      }
    }
  }
  return count;
}

const PYTHON_COMMAND =
  process.env.MINICODE_PYTHON || (process.platform === "win32" ? "py" : "python3");

const DEFAULT_WINDOW_STATE = {
  width: 1440,
  height: 920,
  minWidth: 390,
  minHeight: 700,
  x: undefined,
  y: undefined,
  isMaximized: false,
};

function readPositiveIntFromEnv(name, fallback) {
  const rawValue = process.env[name];
  if (!rawValue) return fallback;
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return Math.floor(parsed);
}

const BACKEND_STARTUP_TIMEOUT_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_STARTUP_TIMEOUT_MS",
  90000
);
const BACKEND_RESTART_INITIAL_DELAY_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_RESTART_INITIAL_DELAY_MS",
  800
);
const BACKEND_RESTART_MAX_DELAY_MS = readPositiveIntFromEnv(
  "MINICODE_BACKEND_RESTART_MAX_DELAY_MS",
  20000
);
const BACKEND_RESTART_JITTER_RATIO = 0.15;
const MAX_WORKSPACE_SEARCH_DEPTH = readPositiveIntFromEnv(
  "MINICODE_WORKSPACE_SEARCH_MAX_DEPTH",
  12
);

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

let backendProcess = null;
let backendManagedByApp = false;
let backendStopRequested = false;
let backendRestartAttempt = 0;
let backendRestartTimer = null;
let mainWindow = null;
let startupFailureWindow = null;
let windowStateSaveTimer = null;
let pendingDeepLink = null;
let ipcHandlersRegistered = false;
let startupRetryInFlight = false;
let trustedWorkspaceRoots = new Set([path.resolve(process.cwd()), getAppRoot()]);
let lastPickedWorkspaceRoot = "";
let startupFailureState = {
  title: "MiniCode Desktop couldn't finish startup",
  message: "The desktop shell could not reach the backend sidecar yet.",
  detail: "",
  logsPath: "",
};

if (process.env.MINICODE_ENABLE_HARDWARE_ACCELERATION !== "1") {
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu-compositing");
}

const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
  app.quit();
}

function getAppRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function getFrontendDistFile() {
  return path.join(getAppRoot(), "frontend", "dist", "index.html");
}

function normalizeWithTrailingSeparator(targetPath) {
  const resolved = path.resolve(targetPath);
  return resolved.endsWith(path.sep) ? resolved : `${resolved}${path.sep}`;
}

const _SENSITIVE_ENV_PREFIXES = ["OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_", "AWS_SECRET", "GITHUB_TOKEN", "GH_TOKEN"];
const _SENSITIVE_ENV_NAMES = new Set([
  "API_KEY", "SECRET_KEY", "PRIVATE_KEY", "ACCESS_TOKEN", "AUTH_TOKEN",
  "DATABASE_URL", "REDIS_URL", "MONGO_URI",
]);
function sanitizedPtyEnv() {
  const env = { ...process.env, TERM: "xterm-256color" };
  for (const key of Object.keys(env)) {
    const upper = key.toUpperCase();
    if (_SENSITIVE_ENV_NAMES.has(upper)) { delete env[key]; continue; }
    if (_SENSITIVE_ENV_PREFIXES.some((p) => upper.startsWith(p))) { delete env[key]; }
  }
  return env;
}

function isSafeWorkspacePath(resolved) {
  const normalized = resolved.replace(/\\/g, "/");
  if (/^[A-Za-z]:\/?$/.test(normalized) || normalized === "/") return false;
  const segments = normalized.split("/").filter(Boolean);
  if (segments.length < 2) return false;
  const systemPrefixes = [
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev",
    "/var/run", "/var/log", "/root",
  ];
  const winSystemPrefixes = [
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/recovery", "c:/system volume information",
  ];
  const lower = normalized.toLowerCase();
  for (const prefix of [...systemPrefixes, ...winSystemPrefixes]) {
    if (lower === prefix || lower.startsWith(prefix + "/")) return false;
  }
  return true;
}

function rememberTrustedWorkspaceRoot(targetPath) {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    return "";
  }
  const resolved = path.resolve(targetPath);
  if (!isSafeWorkspacePath(resolved)) {
    appendDesktopLog(`[desktop] rejected unsafe workspace path: ${resolved}`);
    return "";
  }
  trustedWorkspaceRoots.add(resolved);
  return resolved;
}

function isSamePath(left, right) {
  if (!left || !right) return false;
  return path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
}

function isTrustedWorkspaceRootPath(targetPath) {
  const resolved = path.resolve(targetPath);
  for (const root of trustedWorkspaceRoots) {
    if (isSamePath(resolved, root)) return true;
  }
  return false;
}

const PROTECTED_WRITE_FILE_NAMES = new Set([
  ".gitconfig", ".gitmodules", ".mcp.json", ".claude.json",
  ".codex.json", "settings.json", "settings.local.json",
]);
const PROTECTED_WRITE_PATH_PARTS = new Set([".git", ".claude", ".codex"]);

function isProtectedWritePath(resolvedPath) {
  const basename = path.basename(resolvedPath).toLowerCase();
  if (PROTECTED_WRITE_FILE_NAMES.has(basename)) return true;
  const parts = resolvedPath.split(path.sep).map(p => p.toLowerCase());
  for (const part of parts) {
    if (PROTECTED_WRITE_PATH_PARTS.has(part)) return true;
  }
  // Handle worktree: .git may be a file containing "gitdir: <real-path>"
  for (const root of trustedWorkspaceRoots) {
    const dotGit = path.join(root, ".git");
    try {
      const stat = fs.statSync(dotGit);
      if (stat.isFile()) {
        const content = fs.readFileSync(dotGit, "utf8").trim();
        const match = content.match(/^gitdir:\s*(.+)$/m);
        if (match) {
          const realGitDir = path.resolve(root, match[1]);
          if (resolvedPath === realGitDir || resolvedPath.startsWith(realGitDir + path.sep)) {
            return true;
          }
        }
      }
    } catch {
      // .git doesn't exist or isn't readable — skip
    }
  }
  return false;
}

function assertMutableTrustedPath(targetPath, label = "Path") {
  const resolved = assertTrustedPath(targetPath, label);
  if (isTrustedWorkspaceRootPath(resolved)) {
    throw new Error(`${label} cannot be a trusted workspace root.`);
  }
  if (isProtectedWritePath(resolved)) {
    throw new Error(`${label} targets a protected path and cannot be modified.`);
  }
  return resolved;
}

function isWithinTrustedWorkspace(targetPath) {
  const resolved = path.resolve(targetPath);
  for (const root of trustedWorkspaceRoots) {
    if (resolved === root || resolved.startsWith(normalizeWithTrailingSeparator(root))) {
      return true;
    }
  }
  return false;
}

function assertTrustedPath(targetPath, label = "Path") {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    throw new Error(`${label} is required.`);
  }
  const resolved = path.resolve(targetPath);
  if (!isWithinTrustedWorkspace(resolved)) {
    throw new Error(`${label} is outside the trusted workspace.`);
  }
  return resolved;
}

function isHttpUrl(target) {
  try {
    const parsed = new URL(String(target));
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function normalizeLoopbackCdpEndpoint(target) {
  const raw = typeof target === "string" && target.trim() ? target.trim() : "http://127.0.0.1:9222";
  const withProtocol = /^[a-z]+:\/\//i.test(raw) ? raw : `http://${raw}`;
  let parsed;
  try {
    parsed = new URL(withProtocol);
  } catch {
    throw new Error("Invalid Chrome DevTools endpoint.");
  }
  if (parsed.protocol !== "http:") {
    throw new Error("Chrome DevTools endpoint must use http://.");
  }
  const host = parsed.hostname.toLowerCase();
  const isLoopback =
    host === "127.0.0.1" ||
    host === "localhost" ||
    host === "::1" ||
    host === "[::1]";
  if (!isLoopback) {
    throw new Error("Chrome DevTools endpoint must be local.");
  }
  const port = parsed.port || "9222";
  return `http://${host === "[::1]" ? "::1" : host}:${port}`;
}

async function fetchJsonWithTimeout(url, timeoutMs = 1800) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function discoverChromeCdp(targetEndpoint) {
  const endpoint = normalizeLoopbackCdpEndpoint(targetEndpoint);
  try {
    const [version, rawTargets] = await Promise.all([
      fetchJsonWithTimeout(`${endpoint}/json/version`),
      fetchJsonWithTimeout(`${endpoint}/json/list`),
    ]);
    const targets = Array.isArray(rawTargets)
      ? rawTargets.map((target) => ({
          id: typeof target?.id === "string" ? target.id : "",
          type: typeof target?.type === "string" ? target.type : "other",
          title: typeof target?.title === "string" ? target.title : "",
          url: typeof target?.url === "string" ? target.url : "",
          attached: Boolean(target?.attached),
          faviconUrl: typeof target?.faviconUrl === "string" ? target.faviconUrl : "",
          devtoolsFrontendUrl:
            typeof target?.devtoolsFrontendUrl === "string" ? target.devtoolsFrontendUrl : "",
          webSocketDebuggerUrl:
            typeof target?.webSocketDebuggerUrl === "string" ? target.webSocketDebuggerUrl : "",
        }))
      : [];
    return {
      status: "connected",
      endpoint,
      browser: typeof version?.Browser === "string" ? version.Browser : "",
      protocolVersion: typeof version?.["Protocol-Version"] === "string" ? version["Protocol-Version"] : "",
      userAgent: typeof version?.["User-Agent"] === "string" ? version["User-Agent"] : "",
      webSocketDebuggerUrl:
        typeof version?.webSocketDebuggerUrl === "string" ? version.webSocketDebuggerUrl : "",
      targets,
    };
  } catch (error) {
    return {
      status: "error",
      endpoint,
      browser: "",
      protocolVersion: "",
      userAgent: "",
      webSocketDebuggerUrl: "",
      targets: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

async function readWebSocketEventText(data) {
  if (typeof data === "string") return data;
  if (data && typeof data.text === "function") return await data.text();
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  if (ArrayBuffer.isView(data)) {
    return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  }
  return Buffer.from(data).toString("utf8");
}

async function openWebSocketWithTimeout(url, timeoutMs = 5000) {
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timer = setTimeout(() => {
      cleanup();
      try {
        socket.close();
      } catch {
        // noop
      }
      reject(new Error("CDP WebSocket connection timed out."));
    }, timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      socket.removeEventListener("open", onOpen);
      socket.removeEventListener("error", onError);
    };
    const onOpen = () => {
      cleanup();
      resolve(socket);
    };
    const onError = () => {
      cleanup();
      reject(new Error("Failed to connect to the Chrome target."));
    };
    socket.addEventListener("open", onOpen);
    socket.addEventListener("error", onError);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeBrowserNavigationUrl(target) {
  const raw = typeof target === "string" ? target.trim() : "";
  if (!raw) {
    throw new Error("Navigation URL is required.");
  }
  const withProtocol = /^[a-z]+:\/\//i.test(raw) ? raw : `http://${raw}`;
  if (!isHttpUrl(withProtocol)) {
    throw new Error("Navigation only supports http(s) URLs.");
  }
  return withProtocol;
}

async function withCdpSession(webSocketDebuggerUrl, action) {
  const socket = await openWebSocketWithTimeout(webSocketDebuggerUrl, 5000);
  const pending = new Map();
  let nextId = 1;

  const settlePending = (error) => {
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.clear();
  };

  const onClose = () => {
    settlePending(new Error("Chrome target connection closed."));
  };
  const onError = () => {
    settlePending(new Error("Chrome target connection failed."));
  };
  const onMessage = (event) => {
    void (async () => {
      try {
        const text = await readWebSocketEventText(event.data);
        const payload = JSON.parse(text);
        if (!payload || typeof payload !== "object") return;
        if (typeof payload.id !== "number" || !pending.has(payload.id)) return;
        const request = pending.get(payload.id);
        pending.delete(payload.id);
        clearTimeout(request.timer);
        if (payload.error) {
          request.reject(new Error(payload.error.message || "CDP command failed."));
          return;
        }
        request.resolve(payload.result ?? null);
      } catch (error) {
        appendDesktopLog(`[browser] failed to parse CDP message: ${error instanceof Error ? error.message : String(error)}`);
      }
    })();
  };

  socket.addEventListener("close", onClose);
  socket.addEventListener("error", onError);
  socket.addEventListener("message", onMessage);

  const call = (method, params = {}, timeoutMs = 8000) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timed out.`));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      try {
        socket.send(JSON.stringify({ id, method, params }));
      } catch (error) {
        clearTimeout(timer);
        pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });

  try {
    return await action({ call, socket });
  } finally {
    socket.removeEventListener("close", onClose);
    socket.removeEventListener("error", onError);
    socket.removeEventListener("message", onMessage);
    settlePending(new Error("Chrome target connection closed."));
    try {
      socket.close();
    } catch {
      // noop
    }
  }
}

async function evaluateValue(call, expression, timeoutMs = 3000) {
  const result = await call(
    "Runtime.evaluate",
    {
      expression,
      returnByValue: true,
      awaitPromise: true,
    },
    timeoutMs,
  );
  if (result?.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Runtime evaluation failed.");
  }
  return result?.result?.value ?? null;
}

async function waitForDocumentReady(call, timeoutMs = 12000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const state = await evaluateValue(call, "document.readyState", 2500).catch(() => null);
    if (state === "interactive" || state === "complete") {
      return state;
    }
    await sleep(150);
  }
  throw new Error("Page did not finish loading in time.");
}

async function getPageMetadata(call) {
  const value = await evaluateValue(
    call,
    "(() => ({ title: document.title || '', url: location.href || '' }))()",
    3000,
  ).catch(() => null);
  return {
    title: typeof value?.title === "string" ? value.title : "",
    url: typeof value?.url === "string" ? value.url : "",
  };
}

async function resolveSelectorTarget(call, selector) {
  const normalized = typeof selector === "string" ? selector.trim() : "";
  if (!normalized) {
    throw new Error("CSS selector is required.");
  }
  const selectorLiteral = JSON.stringify(normalized);
  const value = await evaluateValue(
    call,
    `(() => {
      const selector = ${selectorLiteral};
      const el = document.querySelector(selector);
      if (!el) return { ok: false, error: 'Element not found for selector: ' + selector };
      el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
      const rect = el.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        return { ok: false, error: 'Element has no visible box.' };
      }
      return {
        ok: true,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        width: rect.width,
        height: rect.height,
      };
    })()`,
    4000,
  );
  if (!value?.ok) {
    throw new Error(typeof value?.error === "string" ? value.error : "Element lookup failed.");
  }
  return value;
}

async function captureScreenshotWithCall(call) {
  const metrics = await call("Page.getLayoutMetrics", {}, 3000).catch(() => null);
  const screenshot = await call(
    "Page.captureScreenshot",
    { format: "png", fromSurface: true, captureBeyondViewport: true, optimizeForSpeed: true },
    12000,
  );
  if (!screenshot || typeof screenshot.data !== "string" || !screenshot.data) {
    throw new Error("Chrome did not return screenshot data.");
  }
  const width = typeof metrics?.contentSize?.width === "number" ? Math.round(metrics.contentSize.width) : undefined;
  const height = typeof metrics?.contentSize?.height === "number" ? Math.round(metrics.contentSize.height) : undefined;
  return { data: screenshot.data, width, height };
}

async function withChromePageTarget(targetEndpoint, targetId, action) {
  const discovery = await discoverChromeCdp(targetEndpoint);
  if (discovery.status !== "connected") {
    throw new Error(discovery.error || "Chrome DevTools endpoint is unavailable.");
  }
  const target = discovery.targets.find((candidate) => candidate.id === targetId);
  if (!target) {
    throw new Error("Chrome target not found.");
  }
  if (target.type !== "page") {
    throw new Error("Browser actions currently support page targets only.");
  }
  if (!target.webSocketDebuggerUrl) {
    throw new Error("Selected target does not expose a debugger WebSocket.");
  }
  return await withCdpSession(target.webSocketDebuggerUrl, async ({ call }) => {
    await call("Page.enable", {}, 3000).catch(() => null);
    await call("Runtime.enable", {}, 3000).catch(() => null);
    await call("DOM.enable", {}, 3000).catch(() => null);
    try {
      await call("Page.bringToFront", {}, 3000);
    } catch {
      // Some pages may not support bringToFront.
    }
    return await action({ call, target, discovery });
  });
}

async function captureScreenshotViaCdp(webSocketDebuggerUrl) {
  return await withCdpSession(webSocketDebuggerUrl, async ({ call }) => {
    await call("Page.enable", {}, 3000).catch(() => null);
    try {
      await call("Page.bringToFront", {}, 3000);
    } catch {
      // Some pages may not support bringToFront; screenshot can still work.
    }
    return await captureScreenshotWithCall(call);
  });
}

async function captureChromeTargetScreenshot(targetEndpoint, targetId) {
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const screenshot = await captureScreenshotWithCall(call);
    return {
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: target.title,
      url: target.url,
      mimeType: "image/png",
      data: screenshot.data,
      width: screenshot.width,
      height: screenshot.height,
      capturedAt: Date.now(),
    };
  });
}

async function navigateChromeTarget(targetEndpoint, targetId, url) {
  const targetUrl = normalizeBrowserNavigationUrl(url);
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    await call("Page.navigate", { url: targetUrl }, 8000);
    await waitForDocumentReady(call, 15000).catch(() => null);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "navigate",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || targetUrl,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || targetUrl,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

async function clickChromeTarget(targetEndpoint, targetId, selector) {
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const point = await resolveSelectorTarget(call, selector);
    await call("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, buttons: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 }, 3000);
    await sleep(250);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "click",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || target.url,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || target.url,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

async function typeIntoChromeTarget(targetEndpoint, targetId, selector, text) {
  const value = typeof text === "string" ? text : "";
  if (!value) {
    throw new Error("Text is required.");
  }
  return await withChromePageTarget(targetEndpoint, targetId, async ({ call, target, discovery }) => {
    const selectorLiteral = JSON.stringify(typeof selector === "string" ? selector.trim() : "");
    const prepare = await evaluateValue(
      call,
      `(() => {
        const selector = ${selectorLiteral};
        if (!selector) return { ok: false, error: 'CSS selector is required.' };
        const el = document.querySelector(selector);
        if (!el) return { ok: false, error: 'Element not found for selector: ' + selector };
        el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
        const rect = el.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) {
          return { ok: false, error: 'Element has no visible box.' };
        }
        if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
          el.focus();
          el.value = '';
          el.dispatchEvent(new Event('input', { bubbles: true }));
        } else if (el.isContentEditable) {
          el.focus();
          el.textContent = '';
        } else {
          el.focus?.();
        }
        return {
          ok: true,
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
        };
      })()`,
      5000,
    );
    if (!prepare?.ok) {
      throw new Error(typeof prepare?.error === "string" ? prepare.error : "Failed to focus element.");
    }
    await call("Input.dispatchMouseEvent", { type: "mouseMoved", x: prepare.x, y: prepare.y, buttons: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mousePressed", x: prepare.x, y: prepare.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.dispatchMouseEvent", { type: "mouseReleased", x: prepare.x, y: prepare.y, button: "left", clickCount: 1 }, 3000);
    await call("Input.insertText", { text: value }, 5000);
    await sleep(200);
    const page = await getPageMetadata(call);
    const screenshot = await captureScreenshotWithCall(call);
    return {
      action: "type",
      endpoint: discovery.endpoint,
      targetId: target.id,
      title: page.title || target.title,
      url: page.url || target.url,
      screenshot: {
        endpoint: discovery.endpoint,
        targetId: target.id,
        title: page.title || target.title,
        url: page.url || target.url,
        mimeType: "image/png",
        data: screenshot.data,
        width: screenshot.width,
        height: screenshot.height,
        capturedAt: Date.now(),
      },
    };
  });
}

function getDesktopLogPath() {
  return path.join(app.getPath("userData"), "desktop.log");
}

function appendDesktopLog(message) {
  try {
    const line = `[${new Date().toISOString()}] ${String(message)}\n`;
    const logPath = getDesktopLogPath();
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    fs.appendFileSync(logPath, line, "utf8");
    startupFailureState = { ...startupFailureState, logsPath: logPath };
  } catch {
    // Best-effort logging only.
  }
}

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

function isBenignPipeError(error) {
  if (!error) {
    return false;
  }
  const message = error instanceof Error ? error.message : String(error);
  const code = typeof error === "object" && error ? error.code : "";
  return code === "EPIPE" || /broken pipe/i.test(message);
}

process.on("uncaughtException", (error) => {
  if (isBenignPipeError(error)) {
    appendDesktopLog(`[desktop] suppressed uncaughtException: ${error.message}`);
    return;
  }
  appendDesktopLog(
    `[desktop] uncaughtException: ${error instanceof Error && error.stack ? error.stack : String(error)}`
  );
});

process.on("unhandledRejection", (reason) => {
  if (isBenignPipeError(reason)) {
    appendDesktopLog(`[desktop] suppressed unhandledRejection: ${reason.message}`);
    return;
  }
  appendDesktopLog(
    `[desktop] unhandledRejection: ${reason instanceof Error && reason.stack ? reason.stack : String(reason)}`
  );
});

function serializeError(error, contextLabel) {
  const baseMessage = error instanceof Error ? error.message : String(error);
  const detail = error instanceof Error && error.stack ? error.stack : baseMessage;
  return {
    title: "MiniCode Desktop couldn't finish startup",
    message: contextLabel || baseMessage,
    detail,
    logsPath: getDesktopLogPath(),
  };
}

function buildDiagnosticsPayload() {
  const logPath = getDesktopLogPath();
  return {
    generatedAt: new Date().toISOString(),
    app: {
      name: app.getName(),
      version: app.getVersion(),
      isPackaged: app.isPackaged,
      userData: app.getPath("userData"),
      appRoot: getAppRoot(),
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
      managedByApp: backendManagedByApp,
      hasProcess: Boolean(backendProcess),
      restartAttempt: backendRestartAttempt,
      manageBackend: MANAGE_BACKEND,
      pythonCommand: PYTHON_COMMAND,
    },
    windows: {
      hasMainWindow: Boolean(mainWindow && !mainWindow.isDestroyed()),
      hasStartupFailureWindow: Boolean(startupFailureWindow && !startupFailureWindow.isDestroyed()),
      savedState: readWindowState(),
    },
    startupFailure: startupFailureState,
    logs: {
      desktopLogPath: logPath,
      desktopLogExists: fs.existsSync(logPath),
    },
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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function canUsePort(port, host = BACKEND_HOST) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ port, host }, () => {
      server.close(() => resolve(true));
    });
  });
}

async function findAvailablePort(startPort, host = BACKEND_HOST) {
  const firstPort = Number.isFinite(startPort) && startPort > 0 ? Math.floor(startPort) : 8000;
  for (let port = firstPort; port < firstPort + 100; port += 1) {
    if (await canUsePort(port, host)) {
      return port;
    }
  }
  throw new Error(`No available backend port found from ${firstPort}`);
}

async function resolveBackendRuntime() {
  if (MANAGE_BACKEND && !backendProcess) {
    resolvedBackendPort = await findAvailablePort(BACKEND_PORT);
    if (resolvedBackendPort !== BACKEND_PORT) {
      appendDesktopLog(`[backend] port ${BACKEND_PORT} is busy; using ${resolvedBackendPort}`);
    }
  }

  if (MANAGE_BACKEND) {
    resolvedApiBaseUrl = `http://${BACKEND_HOST}:${resolvedBackendPort}`;
    resolvedWsBaseUrl = `ws://${BACKEND_HOST}:${resolvedBackendPort}`;
  } else {
    resolvedApiBaseUrl =
      process.env.MINICODE_API_BASE_URL || `http://${BACKEND_HOST}:${resolvedBackendPort}`;
    resolvedWsBaseUrl =
      process.env.MINICODE_WS_BASE_URL || `ws://${BACKEND_HOST}:${resolvedBackendPort}`;
  }
  process.env.MINICODE_BACKEND_PORT = String(resolvedBackendPort);
  process.env.MINICODE_API_BASE_URL = resolvedApiBaseUrl;
  process.env.MINICODE_WS_BASE_URL = resolvedWsBaseUrl;
  resolvedFrontendUrl = FRONTEND_DEV_URL || process.env.MINICODE_FRONTEND_URL || "";
  if (resolvedFrontendUrl) {
    process.env.MINICODE_FRONTEND_URL = resolvedFrontendUrl;
  }
  process.env.MINICODE_RUNTIME_TOKEN = RUNTIME_TOKEN;
}

function getRendererAdditionalArguments() {
  return [
    `--minicode-api-base-url=${resolvedApiBaseUrl}`,
    `--minicode-ws-base-url=${resolvedWsBaseUrl}`,
    `--minicode-runtime-token=${RUNTIME_TOKEN}`,
  ];
}

function getBackendSpawn() {
  const command = PYTHON_COMMAND;
  // 通过 backend.__main__ 启动，确保 Windows 上
  // ProactorEventLoop 策略在 uvicorn 之前设置（终端子进程需要）
  const args = ["-m", "backend"];

  return { command, args };
}

function getWindowStateFilePath() {
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
    writeStderr(`[desktop] failed to persist window state: ${error.message}\n`);
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

function clearBackendRestartTimer() {
  if (!backendRestartTimer) {
    return;
  }
  clearTimeout(backendRestartTimer);
  backendRestartTimer = null;
}

function nextBackendRestartDelayMs() {
  const exponent = Math.min(backendRestartAttempt, 8);
  const baseDelay = Math.min(
    BACKEND_RESTART_INITIAL_DELAY_MS * 2 ** exponent,
    BACKEND_RESTART_MAX_DELAY_MS
  );
  backendRestartAttempt += 1;
  const jitter = baseDelay * BACKEND_RESTART_JITTER_RATIO * (Math.random() * 2 - 1);
  return Math.max(100, Math.floor(baseDelay + jitter));
}

function scheduleBackendRestart(reason) {
  if (!MANAGE_BACKEND || backendStopRequested || backendProcess || backendRestartTimer) {
    return;
  }

  const delay = nextBackendRestartDelayMs();
  writeStderr(`[backend] scheduling restart in ${delay}ms (${reason})\n`);
  backendRestartTimer = setTimeout(() => {
    backendRestartTimer = null;
    startBackendSidecar();
  }, delay);
}

function startBackendSidecar() {
  if (!MANAGE_BACKEND || backendStopRequested || backendProcess) {
    return;
  }

  clearBackendRestartTimer();

  const { command, args } = getBackendSpawn();
  const child = spawn(command, args, {
    cwd: getAppRoot(),
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      MINICODE_BACKEND_HOST: BACKEND_HOST,
      MINICODE_BACKEND_PORT: String(resolvedBackendPort),
      MINICODE_API_BASE_URL: resolvedApiBaseUrl,
      MINICODE_WS_BASE_URL: resolvedWsBaseUrl,
      MINICODE_FRONTEND_URL: resolvedFrontendUrl,
      MINICODE_RUNTIME_TOKEN: RUNTIME_TOKEN,
    },
    windowsHide: true,
  });

  backendProcess = child;
  backendManagedByApp = true;

  child.stdout?.on("data", (chunk) => {
    writeStdout(`[backend] ${chunk}`);
  });

  child.stderr?.on("data", (chunk) => {
    writeStderr(`[backend] ${chunk}`);
  });

  child.on("exit", (code, signal) => {
    const reason = signal ? `signal=${signal}` : `code=${code}`;
    writeStderr(`[backend] exited (${reason})\n`);
    const shouldRestart = backendManagedByApp && !backendStopRequested;
    backendProcess = null;
    backendManagedByApp = false;
    if (shouldRestart) {
      scheduleBackendRestart(`process exited with ${reason}`);
    }
  });

  child.on("error", (error) => {
    writeStderr(`[backend] failed to start: ${error.message}\n`);
    backendProcess = null;
    backendManagedByApp = false;
    scheduleBackendRestart("spawn error");
  });
}

function stopBackendSidecar() {
  backendStopRequested = true;
  clearBackendRestartTimer();

  if (!backendProcess || !backendManagedByApp) {
    return;
  }

  try {
    backendProcess.kill();
  } catch (error) {
    writeStderr(`[backend] failed to stop: ${error.message}\n`);
  }
}

async function waitForBackendReady(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  const healthUrl = `${resolvedApiBaseUrl}/health`;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(healthUrl);
      if (response.ok) {
        backendRestartAttempt = 0;
        return;
      }
    } catch {
      // Retry until timeout.
    }
    await sleep(400);
  }

  throw new Error(`Backend health check timed out: ${healthUrl}`);
}

function getFrontendTarget() {
  if (FRONTEND_DEV_URL) {
    return { type: "url", value: FRONTEND_DEV_URL };
  }

  const frontendDistFile = getFrontendDistFile();
  if (!fs.existsSync(frontendDistFile)) {
    throw new Error(
      "Missing frontend build output. Run: npm --prefix frontend run build"
    );
  }

  return { type: "file", value: frontendDistFile };
}

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

function closeStartupFailureWindow() {
  if (!startupFailureWindow || startupFailureWindow.isDestroyed()) {
    startupFailureWindow = null;
    return;
  }
  startupFailureWindow.close();
  startupFailureWindow = null;
}

function broadcastStartupFailureState() {
  if (!startupFailureWindow || startupFailureWindow.isDestroyed()) {
    return;
  }
  startupFailureWindow.webContents.send("minicode:startup:state", startupFailureState);
}

function dispatchDeepLink(target) {
  if (!target) {
    return false;
  }

  const urlStr = String(target);
  // Only allow safe protocols for deep links
  const ALLOWED_DEEP_LINK_PROTOCOLS = ["minicode:", "https:", "http:"];
  try {
    const parsed = new URL(urlStr);
    if (!ALLOWED_DEEP_LINK_PROTOCOLS.includes(parsed.protocol.toLowerCase())) {
      console.warn("[desktop] Blocked deep link with disallowed protocol:", parsed.protocol);
      return false;
    }
  } catch {
    // Not a valid URL — block it
    console.warn("[desktop] Blocked malformed deep link:", urlStr.slice(0, 100));
    return false;
  }

  pendingDeepLink = urlStr;
  if (mainWindow && !mainWindow.isDestroyed()) {
    focusMainWindow();
    mainWindow.webContents.send("minicode:deep-link", { target: pendingDeepLink });
  }
  return true;
}

function sendWorkbenchMenuEvent(channel) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return false;
  }
  mainWindow.webContents.send(channel);
  focusMainWindow();
  return true;
}

function buildApplicationMenu() {
  const template = [
    {
      label: "File",
      submenu: [
        {
          label: "New Window",
          accelerator: "Ctrl+Shift+N",
          click: () => {
            void createMainWindow();
          },
        },
        {
          label: "New Chat",
          accelerator: "Ctrl+N",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:new-chat");
          },
        },
        {
          label: "Quick Chat",
          accelerator: "Alt+Ctrl+N",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:quick-chat");
          },
        },
        {
          label: "Open Folder...",
          accelerator: "Ctrl+O",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:open-folder");
          },
        },
        {
          label: "Extensions Marketplace",
          accelerator: "Ctrl+Shift+X",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:extensions-marketplace");
          },
        },
        {
          label: "Settings...",
          accelerator: "Ctrl+,",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:settings");
          },
        },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        {
          label: "Toggle Sidebar",
          accelerator: "Ctrl+B",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:toggle-sidebar");
          },
        },
        {
          label: "Toggle Context Panel",
          accelerator: "Ctrl+\\",
          click: () => {
            sendWorkbenchMenuEvent("minicode:menu:toggle-context");
          },
        },
        {
          label: "Toggle Terminal",
          accelerator: "Ctrl+`",
          click: () => {
            sendWorkbenchMenuEvent("minicode:shortcut:terminal");
          },
        },
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Window",
      submenu: [{ role: "minimize" }, { role: "close" }],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "Open MiniCode Docs",
          click: () => {
            void shell.openExternal("https://github.com");
          },
        },
        {
          label: "Reveal Desktop Log",
          click: () => {
            const logPath = getDesktopLogPath();
            appendDesktopLog("[desktop] reveal log requested");
            shell.showItemInFolder(logPath);
          },
        },
        {
          label: "Export Diagnostics",
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

function showDesktopNotification(payload) {
  if (!Notification.isSupported()) {
    return false;
  }

  const notification = new Notification({
    title:
      typeof payload?.title === "string" && payload.title.trim()
        ? payload.title.trim()
        : "MiniCode",
    body: typeof payload?.body === "string" ? payload.body : "",
    silent: false,
    icon: DESKTOP_ICON_PATH,
  });

  notification.on("click", () => {
    focusMainWindow();
    if (pendingDeepLink) {
      dispatchDeepLink(pendingDeepLink);
    }
  });

  notification.show();
  return true;
}

async function createStartupFailureWindow(errorPayload) {
  startupFailureState = {
    ...startupFailureState,
    ...errorPayload,
    logsPath: errorPayload.logsPath || getDesktopLogPath(),
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

async function createMainWindow() {
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
    icon: DESKTOP_ICON_PATH,
    webPreferences: {
      preload: PRELOAD_FILE,
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

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    // Ctrl+` toggles terminal panel
    if (input.control && !input.alt && input.key === '`') {
      mainWindow.webContents.send('minicode:shortcut:terminal');
      event.preventDefault();
      return;
    }
    // Ctrl+P opens quick-open (intentional override of browser print shortcut)
    if (input.control && !input.shift && !input.alt && input.key === 'p') {
      mainWindow.webContents.send('minicode:shortcut:import');
      event.preventDefault();
      return;
    }
    // Zoom: Ctrl+= / Ctrl+Plus → zoom in, Ctrl+- → zoom out, Ctrl+0 → reset
    if (input.control && !input.alt) {
      const wc = mainWindow.webContents;
      if (input.key === '=' || input.key === '+') {
        wc.setZoomLevel(wc.getZoomLevel() + 0.5);
        event.preventDefault();
        return;
      }
      if (input.key === '-') {
        wc.setZoomLevel(wc.getZoomLevel() - 0.5);
        event.preventDefault();
        return;
      }
      if (input.key === '0') {
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

  if (pendingDeepLink) {
    mainWindow.webContents.once("did-finish-load", () => {
      if (pendingDeepLink) {
        mainWindow?.webContents.send("minicode:deep-link", { target: pendingDeepLink });
      }
    });
  }

  closeStartupFailureWindow();
  return mainWindow;
}

async function attemptAppStartup(source = "startup") {
  if (startupRetryInFlight) {
    return false;
  }

  startupRetryInFlight = true;
  appendDesktopLog(`[desktop] startup attempt (${source})`);
  try {
    backendStopRequested = false;
    await resolveBackendRuntime();
    if (MANAGE_BACKEND && !backendProcess) {
      startBackendSidecar();
    }
    await waitForBackendReady(BACKEND_STARTUP_TIMEOUT_MS);
    await createMainWindow();
    closeStartupFailureWindow();
    appendDesktopLog(`[desktop] startup attempt succeeded (${source})`);
    return true;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendDesktopLog(`[desktop] startup attempt failed (${source}): ${message}`);
    await createStartupFailureWindow(
      serializeError(error, "MiniCode could not reach the backend or load the app shell.")
    );
    return false;
  } finally {
    startupRetryInFlight = false;
  }
}

const WORKSPACE_SEARCH_IGNORED_DIRS = new Set([
  ".git",
  ".idea",
  ".vscode",
  ".venv",
  "venv",
  "node_modules",
  "__pycache__",
  "dist",
  "build",
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

  async function visitDirectory(directoryPath, depth = 0) {
    if (depth > MAX_WORKSPACE_SEARCH_DEPTH) {
      return;
    }
    const dirents = await fs.promises.readdir(directoryPath, { withFileTypes: true });
    for (const dirent of dirents) {
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
  };
}

function registerIpcHandlers() {
  if (ipcHandlersRegistered) {
    return;
  }
  ipcHandlersRegistered = true;

  ipcMain.handle("minicode:window:minimize", () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      return false;
    }
    mainWindow.minimize();
    return true;
  });

  ipcMain.handle("minicode:window:maximize", () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      return false;
    }
    if (mainWindow.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow.maximize();
    }
    return true;
  });

  ipcMain.handle("minicode:window:close", () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      return false;
    }
    mainWindow.close();
    return true;
  });

  ipcMain.handle("minicode:notify", (_event, payload) => {
    return showDesktopNotification(payload);
  });

  ipcMain.handle("minicode:pickDirectory", async () => {
    const owner = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
    const result = await dialog.showOpenDialog(owner, {
      properties: ["openDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return "";
    }
    lastPickedWorkspaceRoot = path.resolve(result.filePaths[0]);
    return rememberTrustedWorkspaceRoot(result.filePaths[0]);
  });

  ipcMain.handle("minicode:workspace:trust", (_event, targetPath) => {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return "";
    }
    const resolved = path.resolve(targetPath);
    const cameFromNativePicker = isSamePath(resolved, lastPickedWorkspaceRoot);
    const alreadyInsideTrustedRoot = isWithinTrustedWorkspace(resolved);
    if (!cameFromNativePicker && !alreadyInsideTrustedRoot) {
      appendDesktopLog(`[desktop] rejected renderer-requested workspace trust: ${resolved}`);
      return "";
    }
    return rememberTrustedWorkspaceRoot(resolved);
  });

  ipcMain.handle("minicode:openExternal", async (_event, target) => {
    if (typeof target !== "string" || !target.trim() || !isHttpUrl(target)) {
      return false;
    }
    await shell.openExternal(target.trim());
    return true;
  });

  ipcMain.handle("minicode:revealPath", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    try {
      const resolved = path.resolve(target.trim());
      if (!isWithinTrustedWorkspace(resolved)) {
        throw new Error("Path is outside the trusted workspace.");
      }
      shell.showItemInFolder(resolved);
      return true;
    } catch (error) {
      appendDesktopLog(`[desktop] revealPath denied: ${error.message}`);
      return false;
    }
  });

  ipcMain.handle("minicode:deepLink:open", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    return dispatchDeepLink(target);
  });

  ipcMain.handle("minicode:startup:getState", () => startupFailureState);
  ipcMain.handle("minicode:startup:retry", async () => {
    return attemptAppStartup("startup-retry");
  });
  ipcMain.handle("minicode:startup:quit", () => {
    app.quit();
    return true;
  });
  ipcMain.handle("minicode:startup:openLogs", () => {
    const logPath = getDesktopLogPath();
    appendDesktopLog("[desktop] open logs requested from startup failure surface");
    shell.showItemInFolder(logPath);
    return true;
  });
  ipcMain.handle("minicode:diagnostics:export", () => {
    return exportDesktopDiagnostics();
  });
  ipcMain.handle("minicode:browser:discover", async (_event, endpoint) => {
    return discoverChromeCdp(endpoint);
  });
  ipcMain.handle("minicode:browser:captureScreenshot", async (_event, endpoint, targetId) => {
    return captureChromeTargetScreenshot(endpoint, targetId);
  });
  ipcMain.handle("minicode:browser:navigate", async (_event, endpoint, targetId, url) => {
    return navigateChromeTarget(endpoint, targetId, url);
  });
  ipcMain.handle("minicode:browser:click", async (_event, endpoint, targetId, selector) => {
    return clickChromeTarget(endpoint, targetId, selector);
  });
  ipcMain.handle("minicode:browser:type", async (_event, endpoint, targetId, selector, text) => {
    return typeIntoChromeTarget(endpoint, targetId, selector, text);
  });

  // --- Local FS Integration ---
  ipcMain.handle("minicode:fs:listTree", async (_event, targetPath) => {
    const rootPath = assertTrustedPath(targetPath, "Directory");
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
      entries: entries
    };
  });

  ipcMain.handle("minicode:fs:searchFiles", async (_event, rootPath, query, limit, kind) => {
    return searchWorkspaceFiles(rootPath, query, limit, kind);
  });

  ipcMain.handle("minicode:fs:readFile", async (_event, targetPath) => {
    const fullPath = assertTrustedPath(targetPath, "File");
    const stat = await fs.promises.stat(fullPath);
    const MAX_READ_SIZE = 10 * 1024 * 1024; // 10 MB
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
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text"
    };
  });

  ipcMain.handle("minicode:fs:writeFile", async (_event, targetPath, content) => {
    const fullPath = assertMutableTrustedPath(targetPath, "File");
    if (fs.existsSync(fullPath)) {
      throw new Error("Direct writeFile cannot overwrite existing files. Use compareWriteFile with an expected hash.");
    }
    await fs.promises.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.promises.writeFile(fullPath, content, "utf8");
    const stat = await fs.promises.stat(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text"
    };
  });

  ipcMain.handle("minicode:fs:compareWriteFile", async (_event, targetPath, expectedHash, content) => {
    const fullPath = assertMutableTrustedPath(targetPath, "File");
    if (fs.existsSync(fullPath) && fs.statSync(fullPath).isDirectory()) {
      throw new Error("Cannot write content into a directory path.");
    }

    let currentHash = "";
    if (fs.existsSync(fullPath)) {
      const raw = fs.readFileSync(fullPath);
      if (!isProbablyTextBuffer(raw, fullPath)) {
        throw new Error("Only UTF-8 text files are supported.");
      }
      currentHash = hashFileContent(raw.toString("utf8"));
    }

    const normalizedExpected = String(expectedHash || "").trim().toLowerCase();
    if (currentHash !== normalizedExpected) {
      const error = new Error("File has changed on disk.");
      error.code = "ERR_FILE_CHANGED";
      error.expectedHash = normalizedExpected;
      error.actualHash = currentHash;
      throw error;
    }

    fs.mkdirSync(path.dirname(fullPath), { recursive: true });
    fs.writeFileSync(fullPath, content, "utf8");
    const stat = fs.statSync(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text"
    };
  });

  ipcMain.handle("minicode:fs:createDirectory", async (_event, targetPath) => {
    const fullPath = assertMutableTrustedPath(targetPath, "Directory");
    await fs.promises.mkdir(fullPath, { recursive: true });
    const stat = await fs.promises.stat(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      is_dir: true,
      size_bytes: null,
      modified_at: stat.mtime.toISOString()
    };
  });

  ipcMain.handle("minicode:fs:renamePath", async (_event, oldPath, newPath) => {
    const fullOldPath = assertMutableTrustedPath(oldPath, "Source path");
    const fullNewPath = assertMutableTrustedPath(newPath, "Destination path");
    await fs.promises.rename(fullOldPath, fullNewPath);
    const stat = await fs.promises.stat(fullNewPath);
    return {
      workspace_root: path.dirname(fullNewPath),
      path: fullNewPath,
      name: path.basename(fullNewPath),
      is_dir: stat.isDirectory(),
      size_bytes: stat.isFile() ? stat.size : null,
      modified_at: stat.mtime.toISOString()
    };
  });

  ipcMain.handle("minicode:fs:deletePath", async (_event, targetPath, recursive, confirmed) => {
    const fullPath = assertMutableTrustedPath(targetPath, "Path");
    const stat = await fs.promises.stat(fullPath);
    const isDir = stat.isDirectory();

    if (isDir && recursive && !confirmed) {
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
      is_dir: isDir
    };
  });

  // --- Local PTY Integration ---
  const ptySessions = new Map();
  let ptyIdCounter = 1;
  const killAllPtySessions = () => {
    for (const [sessionId, session] of ptySessions.entries()) {
      try {
        session.process.kill();
      } catch (error) {
        appendDesktopLog(`[desktop] failed to kill pty ${sessionId}: ${error.message}`);
      }
    }
    ptySessions.clear();
  };
  let ptyCleanupDone = false;
  const safeKillAllPtySessions = () => {
    if (ptyCleanupDone) return;
    ptyCleanupDone = true;
    killAllPtySessions();
  };
  app.once("before-quit", safeKillAllPtySessions);

  ipcMain.handle("minicode:pty:spawn", (_event, cwd) => {
    const shellStr = process.platform === "win32" ? "powershell.exe" : (process.env.SHELL || "bash");
    const shellArgs = process.platform === "win32" ? ["-NoLogo", "-NoProfile"] : [];
    const resolvedCwd = cwd ? assertTrustedPath(cwd, "Terminal cwd") : path.resolve(process.cwd());

    let ptyProcess;
    if (pty) {
      try {
        ptyProcess = pty.spawn(shellStr, shellArgs, {
          name: "xterm-256color",
          cols: 80,
          rows: 24,
          cwd: resolvedCwd,
          env: sanitizedPtyEnv()
        });
      } catch (err) {
        console.error("[desktop] node-pty spawn failed, falling back to child_process:", err);
      }
    }

    if (!ptyProcess) {
      try {
        const cp = require("node:child_process");
        const sub = cp.spawn(shellStr, shellArgs, {
          cwd: resolvedCwd,
          env: sanitizedPtyEnv()
        });

        ptyProcess = {
          pid: sub.pid,
          write: (data) => {
            if (sub.stdin && !sub.stdin.destroyed) {
              sub.stdin.write(data);
            }
          },
          resize: () => {},
          kill: () => {
            sub.kill();
          },
          onData: (cb) => {
            sub.stdout.on("data", (chunk) => cb(chunk.toString("utf8")));
            sub.stderr.on("data", (chunk) => cb(chunk.toString("utf8")));
          },
          onExit: (cb) => {
            sub.on("exit", (exitCode) => cb({ exitCode: exitCode ?? 0 }));
          }
        };
      } catch (cpErr) {
        throw new Error("Terminal process spawn failed: " + cpErr.message);
      }
    }

    const sessionId = `term_${ptyIdCounter++}`;
    const session = {
      process: ptyProcess,
      cwd: resolvedCwd,
      shell: shellStr,
    };
    ptySessions.set(sessionId, session);

    ptyProcess.onData((data) => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.send("minicode:pty:data", { sessionId, data });
    });

    ptyProcess.onExit(({ exitCode }) => {
      ptySessions.delete(sessionId);
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.send("minicode:pty:exit", { sessionId, exitCode });
    });

    return { session_id: sessionId, pid: ptyProcess.pid, shell: shellStr, cwd: resolvedCwd };
  });

  ipcMain.handle("minicode:pty:write", (_event, sessionId, data) => {
    if (typeof sessionId !== "string" || typeof data !== "string") {
      return false;
    }
    if (data.length > 8192 || data.includes("\0")) {
      appendDesktopLog(`[desktop] rejected invalid pty write for ${sessionId}`);
      return false;
    }
    const session = ptySessions.get(sessionId);
    if (session) {
      session.process.write(data);
      return true;
    }
    return false;
  });

  ipcMain.handle("minicode:pty:resize", (_event, sessionId, cols, rows) => {
    const session = ptySessions.get(sessionId);
    if (session) {
      try { session.process.resize(cols, rows); } catch (e) {}
    }
  });

  ipcMain.handle("minicode:pty:kill", (_event, sessionId) => {
    const session = ptySessions.get(sessionId);
    if (session) {
      session.process.kill();
      ptySessions.delete(sessionId);
    }
  });

  ipcMain.handle("minicode:pty:list", () => {
    const list = [];
    for (const [sessionId, session] of ptySessions.entries()) {
       list.push({
         session_id: sessionId,
         pid: session.process.pid,
         shell: session.shell || session.process.process || "shell",
         cwd: session.cwd
       });
    }
    return list;
  });

  // --- Local Environment Sniffer ---
  const { exec } = require("node:child_process");

  function checkCommand(command) {
    return new Promise((resolve) => {
      exec(`${command} --version`, (error) => {
        resolve(!error);
      });
    });
  }

  ipcMain.handle("minicode:env:detect", async () => {
    const [hasGit, hasPython, hasNode, hasDocker, hasOllama] = await Promise.all([
      checkCommand("git"),
      checkCommand("python"),
      checkCommand("node"),
      checkCommand("docker"),
      checkCommand("ollama")
    ]);
    return {
      git: hasGit,
      python: hasPython,
      node: hasNode,
      docker: hasDocker,
      ollama: hasOllama,
      home: require("node:os").homedir()
    };
  });
}

app.on("second-instance", (_event, argv) => {
  const deepLink = argv.find(
    (arg) => typeof arg === "string" && arg.startsWith("minicode://")
  );
  if (deepLink) {
    dispatchDeepLink(deepLink);
  }
  focusMainWindow();
});

app.on("open-url", (event, url) => {
  event.preventDefault();
  dispatchDeepLink(url);
});

app.on("before-quit", () => {
  stopBackendSidecar();
  if (windowStateSaveTimer) {
    clearTimeout(windowStateSaveTimer);
    windowStateSaveTimer = null;
  }
  persistWindowState();
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

app.whenReady().then(async () => {
  app.setAppUserModelId("MiniCode.Desktop");
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient("minicode", process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient("minicode");
  }
  buildApplicationMenu();
  registerIpcHandlers();
  appendDesktopLog("[desktop] app ready");
  await attemptAppStartup("when-ready");
});

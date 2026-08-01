"use strict";

const { WebContentsView, app, dialog } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_PARTITION = "persist:minicode-browser";
const ID_PATTERN = /^[a-zA-Z0-9_-]{1,96}$/;

let getMainWindow = () => null;
let appendDesktopLog = () => {};
let views = new Map();
let activeViewId = null;
const configuredSessions = new WeakSet();
const entriesByWebContentsId = new Map();
const DOWNLOAD_POLICIES = new Set(["block", "ask", "allow"]);
const SITE_PERMISSIONS = new Set(["clipboard-read", "geolocation", "media", "notifications"]);
let browserSettingsPath = "";
let browserSettingsLoaded = false;
let browserSettings = { downloadPolicy: "block", sitePermissions: {} };

function init(deps = {}) {
  if (typeof deps.getMainWindow === "function") getMainWindow = deps.getMainWindow;
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
  if (typeof deps.browserSettingsPath === "string" && deps.browserSettingsPath !== browserSettingsPath) {
    browserSettingsPath = deps.browserSettingsPath;
    browserSettingsLoaded = false;
    browserSettings = { downloadPolicy: "block", sitePermissions: {} };
  }
}

function settingsPath() {
  if (browserSettingsPath) return browserSettingsPath;
  try { return path.join(app.getPath("userData"), "browser-settings.json"); } catch { return ""; }
}

function normalizeOrigin(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.origin : "";
  } catch { return ""; }
}

function normalizeBrowserSettings(raw) {
  const downloadPolicy = DOWNLOAD_POLICIES.has(raw?.downloadPolicy) ? raw.downloadPolicy : "block";
  const sitePermissions = {};
  if (raw?.sitePermissions && typeof raw.sitePermissions === "object") {
    for (const [rawOrigin, rawPermissions] of Object.entries(raw.sitePermissions)) {
      const origin = normalizeOrigin(rawOrigin);
      if (!origin || !Array.isArray(rawPermissions)) continue;
      const permissions = Array.from(new Set(rawPermissions.filter((item) => SITE_PERMISSIONS.has(item)))).sort();
      if (permissions.length) sitePermissions[origin] = permissions;
    }
  }
  return { downloadPolicy, sitePermissions };
}

function loadBrowserSettings() {
  if (browserSettingsLoaded) return;
  browserSettingsLoaded = true;
  const file = settingsPath();
  if (!file || !fs.existsSync(file)) return;
  try {
    browserSettings = normalizeBrowserSettings(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch (error) {
    appendDesktopLog(`[desktop] failed to load browser settings: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function saveBrowserSettings() {
  const file = settingsPath();
  if (!file) return;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, `${JSON.stringify(browserSettings, null, 2)}\n`, "utf8");
  } catch (error) {
    appendDesktopLog(`[desktop] failed to save browser settings: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function getBrowserSettings(url = "") {
  loadBrowserSettings();
  const origin = normalizeOrigin(url);
  const allowed = origin && Array.isArray(browserSettings.sitePermissions[origin])
    ? browserSettings.sitePermissions[origin].filter((item) => SITE_PERMISSIONS.has(item))
    : [];
  return { downloadPolicy: browserSettings.downloadPolicy, origin, permissions: allowed };
}

function setBrowserSettings(payload = {}) {
  loadBrowserSettings();
  if (payload.downloadPolicy != null) {
    const policy = String(payload.downloadPolicy);
    if (!DOWNLOAD_POLICIES.has(policy)) throw new Error("Invalid browser download policy.");
    browserSettings.downloadPolicy = policy;
  }
  const origin = normalizeOrigin(payload.origin);
  const permission = String(payload.permission || "");
  if (payload.origin != null && String(payload.origin).trim() && !origin) {
    throw new Error("Browser site permissions require a valid HTTP or HTTPS origin.");
  }
  if (permission && !origin) throw new Error("Browser site permissions require a valid origin.");
  if (origin && permission) {
    if (!SITE_PERMISSIONS.has(permission)) throw new Error("Unsupported browser site permission.");
    const current = new Set(Array.isArray(browserSettings.sitePermissions[origin]) ? browserSettings.sitePermissions[origin] : []);
    if (payload.allowed) current.add(permission); else current.delete(permission);
    if (current.size) browserSettings.sitePermissions[origin] = Array.from(current).sort();
    else delete browserSettings.sitePermissions[origin];
  }
  saveBrowserSettings();
  return getBrowserSettings(origin);
}

function sitePermissionAllowed(webContents, permission, requestingOrigin = "", details = {}) {
  if (!SITE_PERMISSIONS.has(permission)) return false;
  const url = requestingOrigin || details.requestingUrl || webContents?.getURL?.() || "";
  return getBrowserSettings(url).permissions.includes(permission);
}

function availableDownloadPathIn(downloads, filename) {
  const safeName = path.basename(String(filename || "download"));
  const extension = path.extname(safeName);
  const stem = path.basename(safeName, extension);
  let candidate = path.join(downloads, safeName);
  for (let index = 1; fs.existsSync(candidate); index += 1) {
    candidate = path.join(downloads, `${stem} (${index})${extension}`);
  }
  return candidate;
}

function availableDownloadPath(filename) {
  return availableDownloadPathIn(app.getPath("downloads"), filename);
}

function assertViewId(value) {
  const id = typeof value === "string" ? value.trim() : "";
  if (!ID_PATTERN.test(id)) throw new Error("Invalid embedded browser tab id.");
  return id;
}

function isAllowedNavigationUrl(value) {
  if (value === "about:blank") return true;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function assertNavigationUrl(value) {
  const url = typeof value === "string" ? value.trim() : "";
  if (!isAllowedNavigationUrl(url)) {
    throw new Error("Only HTTP and HTTPS pages can be opened in the embedded browser.");
  }
  return url;
}

function normalizeViewBounds({ x, y, width, height }, content, zoomFactor = 1) {
  const scale = Math.max(0.25, Number(zoomFactor) || 1);
  const left = Math.max(0, Math.floor((Number(x) || 0) * scale));
  const top = Math.max(0, Math.floor((Number(y) || 0) * scale));
  const requestedWidth = Math.floor((Number(width) || 0) * scale);
  const requestedHeight = Math.floor((Number(height) || 0) * scale);
  return {
    x: left,
    y: top,
    width: Math.max(0, Math.min(requestedWidth, Number(content?.width || 0) - left)),
    height: Math.max(0, Math.min(requestedHeight, Number(content?.height || 0) - top)),
  };
}

function makeNetworkLogEntry(details = {}, error = "") {
  return {
    url: String(details.url || ""),
    method: String(details.method || "GET"),
    statusCode: Number(details.statusCode) || 0,
    resourceType: String(details.resourceType || "other"),
    fromCache: Boolean(details.fromCache),
    error: String(error || details.error || ""),
    timestamp: Date.now(),
  };
}

function recordNetworkEvent(details, error = "") {
  const entry = entriesByWebContentsId.get(Number(details?.webContentsId));
  if (!entry) return;
  entry.networkLogs.push(makeNetworkLogEntry(details, error));
  if (entry.networkLogs.length > 200) entry.networkLogs.splice(0, entry.networkLogs.length - 200);
}

function configureGuestSession(session) {
  if (!session || configuredSessions.has(session)) return;
  configuredSessions.add(session);
  loadBrowserSettings();
  session.setPermissionCheckHandler((webContents, permission, requestingOrigin, details) => (
    sitePermissionAllowed(webContents, permission, requestingOrigin, details)
  ));
  session.setPermissionRequestHandler((webContents, permission, callback, details) => {
    callback(sitePermissionAllowed(webContents, permission, details?.requestingUrl, details));
  });
  session.on("will-download", (event, item) => {
    const policy = browserSettings.downloadPolicy;
    if (policy === "block") {
      event.preventDefault();
      return;
    }
    if (policy === "ask") {
      const options = {
        title: "保存下载文件",
        defaultPath: path.join(app.getPath("downloads"), path.basename(item.getFilename() || "download")),
      };
      const owner = getMainWindow();
      const savePath = owner && !owner.isDestroyed() ? dialog.showSaveDialogSync(owner, options) : dialog.showSaveDialogSync(options);
      if (!savePath) {
        event.preventDefault();
        return;
      }
      item.setSavePath(savePath);
      return;
    }
    item.setSavePath(availableDownloadPath(item.getFilename()));
  });
  // Keep a compact, per-tab request history for page diagnostics.  The log is
  // intentionally metadata-only: request/response headers and bodies can
  // contain cookies, authorization values, or user data.
  const webRequest = session.webRequest;
  if (webRequest?.onCompleted) webRequest.onCompleted({ urls: ["*://*/*"] }, (details) => recordNetworkEvent(details));
  if (webRequest?.onErrorOccurred) webRequest.onErrorOccurred({ urls: ["*://*/*"] }, (details) => recordNetworkEvent(details, details.error));
}

function navigationState(entry, type, extra = {}) {
  const webContents = entry.view.webContents;
  const history = webContents.navigationHistory;
  return {
    id: entry.id,
    type,
    url: webContents.getURL() || entry.url || "",
    title: webContents.getTitle() || entry.title || "新标签页",
    faviconUrl: entry.faviconUrl || "",
    loading: webContents.isLoading(),
    canGoBack: Boolean(history?.canGoBack?.()),
    canGoForward: Boolean(history?.canGoForward?.()),
    ...extra,
  };
}

function targetState(entry) {
  const state = navigationState(entry, "updated");
  return {
    id: entry.id,
    type: "page",
    title: state.title,
    url: state.url,
    faviconUrl: state.faviconUrl,
    active: entry.id === activeViewId,
    loading: state.loading,
    canGoBack: state.canGoBack,
    canGoForward: state.canGoForward,
  };
}

function listTargets() {
  return Array.from(views.values(), targetState);
}

function selectEntry(id) {
  const requestedId = typeof id === "string" ? id.trim() : "";
  const entry = requestedId
    ? views.get(assertViewId(requestedId))
    : (activeViewId ? views.get(activeViewId) : null) || views.values().next().value;
  if (!entry) throw new Error("No embedded browser tab is available. Open the browser panel first.");
  if (entry.view.webContents.isDestroyed()) throw new Error("Embedded browser tab is no longer available.");
  return entry;
}

const scriptValue = (value) => JSON.stringify(value ?? null);

async function evaluateInEntry(entry, expression) {
  return entry.view.webContents.executeJavaScript(expression, true);
}

async function waitForSelector(entry, selector, timeoutMs) {
  const deadline = Date.now() + Math.max(0, Math.min(Number(timeoutMs) || 5000, 30000));
  while (Date.now() <= deadline) {
    if (await evaluateInEntry(entry, `Boolean(document.querySelector(${scriptValue(selector)}))`)) return true;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  return false;
}

const ELEMENT_PICKER_SCRIPT = `new Promise((resolve) => {
  if (window.__minicodeElementPickerCancel) window.__minicodeElementPickerCancel();
  const overlay = document.createElement('div');
  overlay.setAttribute('data-minicode-picker', 'true');
  Object.assign(overlay.style, {
    position: 'fixed', zIndex: '2147483647', pointerEvents: 'none',
    border: '2px solid #4f8cff', background: 'rgba(79,140,255,.12)',
    borderRadius: '4px', display: 'none', boxSizing: 'border-box'
  });
  document.documentElement.appendChild(overlay);
  let target = null;
  let timer = 0;
  const selectorFor = (element) => {
    if (element.id) return '#' + CSS.escape(element.id);
    const parts = [];
    let current = element;
    while (current && current.nodeType === 1 && parts.length < 7) {
      let part = current.tagName.toLowerCase();
      const classes = Array.from(current.classList || []).filter(Boolean).slice(0, 2);
      if (classes.length) part += '.' + classes.map((value) => CSS.escape(value)).join('.');
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((item) => item.tagName === current.tagName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')';
      }
      parts.unshift(part);
      current = parent;
    }
    return parts.join(' > ');
  };
  const cleanup = (value) => {
    clearTimeout(timer);
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('click', choose, true);
    document.removeEventListener('keydown', keydown, true);
    overlay.remove();
    window.__minicodeElementPickerCancel = null;
    resolve(value);
  };
  const move = (event) => {
    const element = document.elementFromPoint(event.clientX, event.clientY);
    if (!element || element === overlay || element.hasAttribute('data-minicode-picker')) return;
    target = element;
    const rect = element.getBoundingClientRect();
    Object.assign(overlay.style, {
      display: 'block', left: rect.left + 'px', top: rect.top + 'px',
      width: rect.width + 'px', height: rect.height + 'px'
    });
  };
  const choose = (event) => {
    if (!target) return;
    event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
    const rect = target.getBoundingClientRect();
    cleanup({
      selector: selectorFor(target),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
      text: String(target.innerText || target.textContent || '').trim().slice(0, 240)
    });
  };
  const keydown = (event) => { if (event.key === 'Escape') cleanup(null); };
  window.__minicodeElementPickerCancel = () => cleanup(null);
  document.addEventListener('mousemove', move, true);
  document.addEventListener('click', choose, true);
  document.addEventListener('keydown', keydown, true);
  timer = setTimeout(() => cleanup(null), 30000);
})`;

const REGION_PICKER_SCRIPT = `new Promise((resolve) => {
  if (window.__minicodeElementPickerCancel) window.__minicodeElementPickerCancel();
  const overlay = document.createElement('div');
  overlay.setAttribute('data-minicode-picker', 'true');
  Object.assign(overlay.style, {
    position: 'fixed', zIndex: '2147483647', pointerEvents: 'none',
    border: '2px solid #4f8cff', background: 'rgba(79,140,255,.12)',
    borderRadius: '4px', display: 'none', boxSizing: 'border-box'
  });
  document.documentElement.appendChild(overlay);
  const previousCursor = document.documentElement.style.cursor;
  document.documentElement.style.cursor = 'crosshair';
  let start = null;
  let timer = 0;
  const cleanup = (value) => {
    clearTimeout(timer);
    document.removeEventListener('mousedown', down, true);
    document.removeEventListener('mousemove', move, true);
    document.removeEventListener('mouseup', up, true);
    document.removeEventListener('keydown', keydown, true);
    document.documentElement.style.cursor = previousCursor;
    overlay.remove();
    window.__minicodeElementPickerCancel = null;
    resolve(value);
  };
  const stop = (event) => {
    event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
  };
  const down = (event) => {
    if (event.button !== 0) return;
    stop(event);
    start = { x: event.clientX, y: event.clientY };
    Object.assign(overlay.style, { display: 'block', left: start.x + 'px', top: start.y + 'px', width: '0px', height: '0px' });
  };
  const move = (event) => {
    if (!start) return;
    stop(event);
    const left = Math.min(start.x, event.clientX);
    const top = Math.min(start.y, event.clientY);
    const width = Math.abs(event.clientX - start.x);
    const height = Math.abs(event.clientY - start.y);
    Object.assign(overlay.style, { left: left + 'px', top: top + 'px', width: width + 'px', height: height + 'px' });
  };
  const up = (event) => {
    if (!start || event.button !== 0) return;
    stop(event);
    const rect = {
      x: Math.min(start.x, event.clientX), y: Math.min(start.y, event.clientY),
      width: Math.abs(event.clientX - start.x), height: Math.abs(event.clientY - start.y)
    };
    cleanup(rect.width >= 4 && rect.height >= 4
      ? { selector: '', rect, viewport: { width: innerWidth, height: innerHeight, devicePixelRatio }, text: '' }
      : null);
  };
  const keydown = (event) => { if (event.key === 'Escape') cleanup(null); };
  window.__minicodeElementPickerCancel = () => cleanup(null);
  document.addEventListener('mousedown', down, true);
  document.addEventListener('mousemove', move, true);
  document.addEventListener('mouseup', up, true);
  document.addEventListener('keydown', keydown, true);
  timer = setTimeout(() => cleanup(null), 30000);
})`;

function emit(entry, type, extra = {}) {
  const mainWindow = getMainWindow();
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send("minicode:embeddedBrowser:event", navigationState(entry, type, extra));
}

function activate(id) {
  const requestedId = assertViewId(id);
  activeViewId = requestedId;
  for (const [entryId, entry] of views) {
    entry.view.setVisible(entryId === requestedId);
  }
  return Boolean(views.get(requestedId));
}

function attachViewEvents(entry) {
  const webContents = entry.view.webContents;

  webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedNavigationUrl(url)) {
      emit(entry, "new-tab-request", { requestedUrl: url });
    }
    return { action: "deny" };
  });

  webContents.on("will-navigate", (event, url) => {
    if (isAllowedNavigationUrl(url)) return;
    event.preventDefault();
    emit(entry, "error", { error: "已阻止不安全的页面跳转。" });
  });
  webContents.on("did-start-loading", () => emit(entry, "loading"));
  webContents.on("did-stop-loading", () => emit(entry, "updated"));
  webContents.on("did-navigate", (_event, url) => {
    entry.url = url;
    entry.faviconUrl = "";
    emit(entry, "updated");
  });
  webContents.on("did-navigate-in-page", (_event, url) => {
    entry.url = url;
    emit(entry, "updated");
  });
  webContents.on("page-title-updated", (event, title) => {
    event.preventDefault();
    entry.title = title;
    emit(entry, "updated");
  });
  webContents.on("page-favicon-updated", (_event, favicons) => {
    entry.faviconUrl = Array.isArray(favicons)
      ? favicons.find((url) => typeof url === "string" && /^https?:\/\//i.test(url)) || ""
      : "";
    emit(entry, "updated");
  });
  webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) return;
    appendDesktopLog(`[desktop] embedded browser failed to load ${validatedURL || "unknown URL"}: ${errorDescription}`);
    emit(entry, "error", { error: errorDescription || "页面加载失败。" });
  });
  webContents.on("render-process-gone", (_event, details) => {
    appendDesktopLog(`[desktop] embedded browser renderer exited: ${details?.reason || "unknown"}`);
    emit(entry, "error", { error: "页面进程已退出，请刷新后重试。" });
  });
  webContents.on("console-message", (_event, level, message, line, sourceId) => {
    entry.consoleLogs.push({ level, message, line, sourceId, timestamp: Date.now() });
    if (entry.consoleLogs.length > 200) entry.consoleLogs.splice(0, entry.consoleLogs.length - 200);
  });
}

async function create({ id, url }) {
  const requestedId = assertViewId(id);
  const requestedUrl = assertNavigationUrl(url);
  let entry = views.get(requestedId);
  if (!entry) {
    const mainWindow = getMainWindow();
    if (!mainWindow || mainWindow.isDestroyed()) throw new Error("Main window is unavailable.");
    const view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        partition: DEFAULT_PARTITION,
        spellcheck: true,
      },
    });
    entry = { id: requestedId, view, url: requestedUrl, title: "新标签页", faviconUrl: "", consoleLogs: [], networkLogs: [] };
    views.set(requestedId, entry);
    entriesByWebContentsId.set(view.webContents.id, entry);
    configureGuestSession(view.webContents.session);
    attachViewEvents(entry);
    mainWindow.contentView.addChildView(view);
  }
  activate(requestedId);
  await entry.view.webContents.loadURL(requestedUrl);
  return navigationState(entry, "updated");
}

function setBounds({ id, x, y, width, height }) {
  const entry = views.get(assertViewId(id));
  if (!entry) return false;
  const mainWindow = getMainWindow();
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const content = mainWindow.getContentBounds();
  // getBoundingClientRect() is expressed in renderer CSS pixels. Electron View
  // bounds use unzoomed content coordinates, so compensate for Ctrl +/- zoom
  // or the guest view drifts left and overlaps the chat/composer.
  const zoomFactor = Number(mainWindow.webContents.getZoomFactor?.()) || 1;
  const bounds = normalizeViewBounds({ x, y, width, height }, content, zoomFactor);
  entry.view.setBounds(bounds);
  entry.view.setVisible(activeViewId === entry.id && bounds.width > 0 && bounds.height > 0);
  return true;
}

async function navigate({ id, url }) {
  const requestedId = assertViewId(id);
  const requestedUrl = assertNavigationUrl(url);
  const entry = views.get(requestedId);
  if (!entry) return create({ id: requestedId, url: requestedUrl });
  activate(requestedId);
  await entry.view.webContents.loadURL(requestedUrl);
  return navigationState(entry, "updated");
}

async function clearSiteData(id) {
  const entry = selectEntry(id);
  const origin = normalizeOrigin(entry.view.webContents.getURL() || entry.url);
  if (!origin) return false;
  await entry.view.webContents.session.clearStorageData({
    origin,
    storages: ["cookies", "filesystem", "indexdb", "localstorage", "serviceworkers", "cachestorage"],
  });
  loadBrowserSettings();
  delete browserSettings.sitePermissions[origin];
  saveBrowserSettings();
  return true;
}

async function executeControlCommand(payload = {}) {
  const action = String(payload.action || "").trim().toLowerCase();
  if (action === "discover" || action === "list_targets") {
    return { ok: true, action, browser: "MiniCode Embedded Browser", targets: listTargets() };
  }

  if (action === "navigate") {
    const url = assertNavigationUrl(payload.url);
    const requestedTargetId = typeof payload.target_id === "string" ? payload.target_id.trim() : "";
    let entry;
    let alreadyNavigated = false;
    try {
      entry = selectEntry(requestedTargetId);
    } catch (error) {
      if (requestedTargetId) throw error;
      await navigate({ id: "agent_browser", url });
      entry = selectEntry("agent_browser");
      alreadyNavigated = true;
    }
    if (!alreadyNavigated) await navigate({ id: entry.id, url });
    const waitMs = Math.max(0, Math.min(Number(payload.wait_ms) || 0, 5000));
    if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    return { ok: true, action, target: targetState(entry) };
  }
  const entry = selectEntry(payload.target_id);
  const webContents = entry.view.webContents;
  if (action === "screenshot") {
    const image = await webContents.capturePage();
    const size = image.getSize();
    return { ok: true, action, target: targetState(entry), mimeType: "image/png", data: image.toPNG().toString("base64"), width: size.width, height: size.height };
  }
  if (action === "get_url") return { ok: true, action, target: targetState(entry) };
  if (action === "get_text") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, "document.body ? document.body.innerText : ''") };
  if (action === "get_html") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, "document.documentElement ? document.documentElement.outerHTML : ''") };
  if (action === "get_dom") {
    const value = await evaluateInEntry(entry, `(() => Array.from(document.querySelectorAll('body *')).slice(0, 800).map((el) => ({
      tag: el.tagName.toLowerCase(), id: el.id || undefined, role: el.getAttribute('role') || undefined,
      ariaLabel: el.getAttribute('aria-label') || undefined,
      text: (el.childElementCount === 0 ? el.textContent : '').trim().slice(0, 240) || undefined
    })))()`);
    return { ok: true, action, target: targetState(entry), value };
  }
  if (action === "wait_for_element") {
    const selector = String(payload.selector || "");
    const found = await waitForSelector(entry, selector, payload.timeout_ms);
    return { ok: found, action, target: targetState(entry), value: found, error: found ? undefined : `Timed out waiting for element: ${selector}` };
  }
  if (action === "get_console_logs") return { ok: true, action, target: targetState(entry), value: entry.consoleLogs.slice(-100) };
  if (action === "get_network_logs") return { ok: true, action, target: targetState(entry), value: entry.networkLogs.slice(-100) };
  if (action === "pick_element") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, ELEMENT_PICKER_SCRIPT) };
  if (action === "pick_region") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, REGION_PICKER_SCRIPT) };
  if (action === "click") {
    const selector = String(payload.selector || "").trim();
    if (selector) {
      const result = await evaluateInEntry(entry, `(() => { const el = document.querySelector(${scriptValue(selector)}); if (!el) return { ok: false }; el.scrollIntoView({ block: 'center', inline: 'center' }); el.click(); return { ok: true }; })()`);
      if (!result?.ok) throw new Error(`Element not found: ${selector}`);
    } else {
      webContents.sendInputEvent({ type: "mouseDown", x: Number(payload.x), y: Number(payload.y), button: "left", clickCount: 1 });
      webContents.sendInputEvent({ type: "mouseUp", x: Number(payload.x), y: Number(payload.y), button: "left", clickCount: 1 });
    }
    return { ok: true, action, target: targetState(entry), value: selector || `${payload.x},${payload.y}` };
  }
  if (action === "type") {
    const selector = String(payload.selector || "").trim();
    if (selector) {
      const result = await evaluateInEntry(entry, `(() => { const el = document.querySelector(${scriptValue(selector)}); if (!el) return { ok: false }; el.focus(); if (${Boolean(payload.clear)}) { if ('value' in el) el.value = ''; else if (el.isContentEditable) el.textContent = ''; el.dispatchEvent(new Event('input', { bubbles: true })); } return { ok: true }; })()`);
      if (!result?.ok) throw new Error(`Element not found: ${selector}`);
    }
    await webContents.insertText(String(payload.text || ""));
    return { ok: true, action, target: targetState(entry), value: String(payload.text || "").length };
  }
  if (action === "press_key") {
    const key = String(payload.key || "");
    webContents.sendInputEvent({ type: "keyDown", keyCode: key });
    webContents.sendInputEvent({ type: "keyUp", keyCode: key });
    return { ok: true, action, target: targetState(entry), value: key };
  }
  if (action === "scroll") {
    const selector = String(payload.selector || "").trim();
    const deltaX = Number(payload.delta_x) || 0;
    const deltaY = payload.delta_y == null ? 600 : Number(payload.delta_y) || 0;
    const value = await evaluateInEntry(entry, selector
      ? `(() => { const el = document.querySelector(${scriptValue(selector)}); if (!el) return { ok: false }; el.scrollBy(${deltaX}, ${deltaY}); return { ok: true, left: el.scrollLeft, top: el.scrollTop }; })()`
      : `(() => { window.scrollBy(${deltaX}, ${deltaY}); return { ok: true, x: window.scrollX, y: window.scrollY }; })()`);
    if (!value?.ok) throw new Error(`Element not found: ${selector}`);
    return { ok: true, action, target: targetState(entry), value };
  }
  if (action === "evaluate") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, String(payload.expression || "")) };
  throw new Error(`Unsupported embedded browser action: ${action}`);
}

function runNavigationAction({ id, action }) {
  const entry = views.get(assertViewId(id));
  if (!entry) return false;
  const webContents = entry.view.webContents;
  const history = webContents.navigationHistory;
  if (action === "back" && history?.canGoBack?.()) history.goBack();
  else if (action === "forward" && history?.canGoForward?.()) history.goForward();
  else if (action === "reload") webContents.reload();
  else if (action === "stop") webContents.stop();
  else if (action === "focus") webContents.focus();
  else return false;
  return true;
}

function close(id) {
  const requestedId = assertViewId(id);
  const entry = views.get(requestedId);
  if (!entry) return false;
  const mainWindow = getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.contentView.removeChildView(entry.view); } catch { /* already detached */ }
  }
  views.delete(requestedId);
  entriesByWebContentsId.delete(entry.view.webContents.id);
  if (activeViewId === requestedId) activeViewId = null;
  if (!entry.view.webContents.isDestroyed()) entry.view.webContents.close();
  return true;
}

function disposeAll() {
  for (const id of Array.from(views.keys())) close(id);
  activeViewId = null;
}

module.exports = {
  init,
  create,
  activate,
  setBounds,
  navigate,
  runNavigationAction,
  close,
  clearSiteData,
  disposeAll,
  assertNavigationUrl,
  isAllowedNavigationUrl,
  normalizeViewBounds,
  listTargets,
  executeControlCommand,
  getBrowserSettings,
  setBrowserSettings,
  makeNetworkLogEntry,
  normalizeOrigin,
  normalizeBrowserSettings,
  availableDownloadPathIn,
};

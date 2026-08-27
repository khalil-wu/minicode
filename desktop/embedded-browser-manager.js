"use strict";

const { WebContentsView, app, dialog } = require("electron");
const net = require("node:net");
const dns = require("node:dns").promises;
const fs = require("node:fs");
const path = require("node:path");

const DEFAULT_PARTITION = "persist:minicode-browser";
const ID_PATTERN = /^[a-zA-Z0-9_-]{1,96}$/;
const CONTROL_DEFAULT_MAX_CHARS = 20_000;
const CONTROL_MAX_CHARS = 200_000;
const API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024;

let getMainWindow = () => null;
let appendDesktopLog = () => {};
let assessBrowserNavigationPolicy = null;
let isOwnedPreviewUrl = () => false;
let lookupHostAddresses = (host) => dns.lookup(host, { all: true, verbatim: true });
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
  if (typeof deps.assessBrowserNavigationPolicy === "function") {
    assessBrowserNavigationPolicy = deps.assessBrowserNavigationPolicy;
  }
  if (typeof deps.isOwnedPreviewUrl === "function") isOwnedPreviewUrl = deps.isOwnedPreviewUrl;
  if (typeof deps.lookupHostAddresses === "function") lookupHostAddresses = deps.lookupHostAddresses;
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

function requireConversationId(value) {
  const conversationId = typeof value === "string" ? value.trim() : "";
  if (!conversationId) throw new Error("Embedded browser commands require a conversation owner.");
  return conversationId;
}

function conversationIdFrom(payload = {}) {
  return requireConversationId(payload.conversation_id || payload.conversationId);
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

function isLoopbackHost(host) {
  const normalized = String(host || "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (normalized === "localhost" || normalized === "localhost.localdomain" || normalized.endsWith(".localhost")) return true;
  if (normalized.startsWith("::ffff:")) return isLoopbackHost(normalized.slice(7));
  return net.isIP(normalized) > 0 && (normalized === "::1" || normalized === "::" || normalized.startsWith("127.") || normalized === "0.0.0.0");
}

function isPrivateOrLinkLocalHost(host) {
  const normalized = String(host || "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (!normalized || isLoopbackHost(normalized)) return false;
  if (net.isIP(normalized) === 6) {
    // WHATWG URL normalization renders IPv4-mapped literals in compressed
    // hexadecimal form (for example ::ffff:7f00:1).  Treat the entire mapped
    // family as non-public rather than attempting a lossy text conversion.
    if (normalized.startsWith("::ffff:")) return true;
    return normalized.startsWith("fc")
      || normalized.startsWith("fd")
      || /^fe[89ab]/.test(normalized)
      || normalized.startsWith("ff")
      || normalized.startsWith("2001:db8:");
  }
  if (net.isIP(normalized) !== 4) return false;
  const parts = normalized.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [a, b] = parts;
  return a === 0
    || a === 10
    || a === 127
    || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 0 || b === 168))
    || (a === 198 && (b === 18 || b === 19 || b === 51))
    || (a === 203 && b === 0)
    || a >= 224;
}

function approvedPrivateOrigin(entry, value) {
  const origin = normalizeOrigin(value);
  return Boolean(origin && entry?.approvedPrivateOrigins instanceof Set && entry.approvedPrivateOrigins.has(origin));
}

function ownedPreviewTarget(value, conversationId) {
  try { return Boolean(isOwnedPreviewUrl(value, conversationId)); } catch { return false; }
}

function assessNavigationTarget(value, conversationId = "", entry = null) {
  if (value === "about:blank") return { allowed: true, risk: "blank", reason: "" };
  if (!isAllowedNavigationUrl(value)) {
    return { allowed: false, risk: "invalid", reason: "Only HTTP and HTTPS pages can be opened in the embedded browser." };
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return { allowed: false, risk: "invalid", reason: "Navigation URL is invalid." };
  }
  if (parsed.username || parsed.password) {
    return { allowed: false, risk: "credentials", reason: "Embedded browser URLs must not contain credentials." };
  }
  let assessment = null;
  if (typeof assessBrowserNavigationPolicy === "function") {
    try { assessment = assessBrowserNavigationPolicy(value); } catch (error) {
      return { allowed: false, risk: "invalid", reason: error instanceof Error ? error.message : String(error) };
    }
  }
  const localTarget = isLoopbackHost(parsed.hostname);
  const privateTarget = Boolean(assessment?.requiresPrivateNetworkApproval)
    || Boolean(assessment?.requiresPrivateNetwork)
    || isPrivateOrLinkLocalHost(parsed.hostname);
  if (localTarget || privateTarget) {
    if (approvedPrivateOrigin(entry, value) || ownedPreviewTarget(value, conversationId)) {
      return { allowed: true, risk: localTarget ? "local" : "private", reason: "" };
    }
    return {
      allowed: false,
      risk: localTarget ? "local" : "private",
      requiresPrivateNetworkApproval: true,
      reason: "Local and private network navigation requires explicit approval.",
    };
  }
  return { allowed: true, risk: "public", reason: "" };
}

async function hostResolvesToPrivateNetwork(host) {
  const normalized = String(host || "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (!normalized || net.isIP(normalized) > 0 || normalized.endsWith(".localhost")) return false;
  let timer;
  try {
    const addresses = await Promise.race([
      lookupHostAddresses(normalized),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("DNS lookup timed out.")), 3000);
      }),
    ]);
    return Array.isArray(addresses) && addresses.some((item) => {
      const address = String(item?.address || "");
      return isLoopbackHost(address) || isPrivateOrLinkLocalHost(address);
    });
  } catch {
    // DNS failure is not evidence that a hostname is public.  Return an
    // explicit unknown state so callers require a user decision instead of
    // silently weakening the private-network boundary (for example when
    // Chromium and Node use different proxy/DNS paths).
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function assessNavigationTargetForRequest(value, conversationId = "", entry = null) {
  const decision = assessNavigationTarget(value, conversationId, entry);
  if (!decision.allowed || decision.risk !== "public") return decision;
  let parsed;
  try { parsed = new URL(value); } catch { return decision; }
  const resolvesPrivate = await hostResolvesToPrivateNetwork(parsed.hostname);
  if (resolvesPrivate === false) return decision;
  if (approvedPrivateOrigin(entry, value) || ownedPreviewTarget(value, conversationId)) {
    return { allowed: true, risk: "private", reason: "" };
  }
  if (resolvesPrivate === null) {
    return {
      allowed: false,
      risk: "unverified",
      requiresPrivateNetworkApproval: true,
      reason: "The hostname could not be verified as a public network target.",
    };
  }
  return {
    allowed: false,
    risk: "private",
    requiresPrivateNetworkApproval: true,
    reason: "This hostname resolves to a local or private network address.",
  };
}

function requestApprovedNavigation(entry, url) {
  const origin = normalizeOrigin(url) || url;
  if (entry.pendingNavigationApprovals.has(origin)) return;
  entry.pendingNavigationApprovals.add(origin);
  void confirmNavigationUrl(url, entry.conversationId, entry)
    .then(() => entry.view.webContents.loadURL(url))
    .catch((error) => {
      emit(entry, "error", {
        error: error instanceof Error ? error.message : "Local or private navigation was cancelled.",
      });
    })
    .finally(() => entry.pendingNavigationApprovals.delete(origin));
}

function actualPeerNavigationError(entry, url, peerIp, proxyResolution = "DIRECT") {
  const peer = String(peerIp || "").trim().replace(/^\[|\]$/g, "");
  if (!peer || (!isLoopbackHost(peer) && !isPrivateOrLinkLocalHost(peer))) return "";
  // Chromium reports the proxy server as the connected peer.  A loopback or
  // fake-IP peer is therefore expected when Electron resolved this request
  // through a configured proxy; the DNS preflight above still validates the
  // destination hostname itself.  Keep the connected-peer check fail-closed
  // for DIRECT traffic and whenever proxy resolution cannot be obtained.
  const proxy = String(proxyResolution || "DIRECT").trim().toUpperCase();
  if (proxy && !/^DIRECT(?:\s*;|\s*$)/.test(proxy)) return "";
  if (approvedPrivateOrigin(entry, url) || ownedPreviewTarget(url, entry?.conversationId || "")) return "";
  return `Navigation to ${new URL(url).hostname} connected to a local or private peer (${peer}).`;
}

async function confirmNavigationUrl(value, conversationId = "", entry = null) {
  const url = typeof value === "string" ? value.trim() : "";
  const decision = await assessNavigationTargetForRequest(url, conversationId, entry);
  if (decision.allowed) {
    return {
      url,
      privateNetworkApproved: decision.risk === "local" || decision.risk === "private",
    };
  }
  if (!decision.requiresPrivateNetworkApproval) throw new Error(decision.reason);
  const owner = getMainWindow();
  const result = await dialog.showMessageBox(owner && !owner.isDestroyed() ? owner : undefined, {
    type: "warning",
    buttons: ["Cancel", "Open"],
    defaultId: 0,
    cancelId: 0,
    title: decision.risk === "local" ? "Open local browser target?" : "Open private browser target?",
    message: "Embedded browser navigation can access services on this computer or private network.",
    detail: `${new URL(url).hostname} may expose sensitive local services. Continue only if you trust this target.`,
    noLink: true,
  });
  if (result.response !== 1) throw new Error("Local or private browser navigation was cancelled.");
  const approvedOrigin = normalizeOrigin(url);
  if (approvedOrigin && entry?.approvedPrivateOrigins instanceof Set) entry.approvedPrivateOrigins.add(approvedOrigin);
  return { url, privateNetworkApproved: true };
}

function assertNavigationUrl(value, conversationId = "") {
  const url = typeof value === "string" ? value.trim() : "";
  const assessment = assessNavigationTarget(url, conversationId);
  if (!assessment.allowed) throw new Error(assessment.reason);
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
    url: String(details.url || "").slice(0, CONTROL_DEFAULT_MAX_CHARS),
    method: String(details.method || "GET").slice(0, CONTROL_DEFAULT_MAX_CHARS),
    statusCode: Number(details.statusCode) || 0,
    resourceType: String(details.resourceType || "other").slice(0, CONTROL_DEFAULT_MAX_CHARS),
    fromCache: Boolean(details.fromCache),
    error: String(error || details.error || "").slice(0, CONTROL_DEFAULT_MAX_CHARS),
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
  const navigationFilter = { urls: ["http://*/*", "https://*/*"] };
  if (webRequest?.onBeforeRequest) {
    webRequest.onBeforeRequest(navigationFilter, (details, callback) => {
      if (details.resourceType !== "mainFrame") {
        callback({ cancel: false });
        return;
      }
      const entry = entriesByWebContentsId.get(Number(details.webContentsId));
      if (!entry) {
        callback({ cancel: true });
        return;
      }
      void assessNavigationTargetForRequest(details.url, entry.conversationId, entry)
        .then((decision) => {
          callback({ cancel: !decision.allowed });
          if (decision.allowed) return;
          if (decision.requiresPrivateNetworkApproval) {
            requestApprovedNavigation(entry, details.url);
            return;
          }
          emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
        })
        .catch((error) => {
          callback({ cancel: true });
          emit(entry, "error", {
            error: error instanceof Error ? error.message : "Navigation policy check failed.",
          });
        });
    });
  }
  if (webRequest?.onResponseStarted) {
    webRequest.onResponseStarted(navigationFilter, (details) => {
      if (details.resourceType !== "mainFrame") return;
      const entry = entriesByWebContentsId.get(Number(details.webContentsId));
      if (!entry) return;
      void Promise.resolve(session.resolveProxy(details.url))
        .catch(() => "DIRECT")
        .then((proxyResolution) => {
          const error = actualPeerNavigationError(entry, details.url, details.ip, proxyResolution);
          if (!error) return;
          try { entry.view.webContents.stop(); } catch { /* renderer may already be gone */ }
          emit(entry, "error", { error });
        });
    });
  }
  if (webRequest?.onCompleted) webRequest.onCompleted({ urls: ["*://*/*"] }, (details) => recordNetworkEvent(details));
  if (webRequest?.onErrorOccurred) webRequest.onErrorOccurred({ urls: ["*://*/*"] }, (details) => recordNetworkEvent(details, details.error));
}

function navigationState(entry, type, extra = {}) {
  const webContents = entry.view.webContents;
  const history = webContents.navigationHistory;
  return {
    id: entry.id,
    conversationId: entry.conversationId,
    conversation_id: entry.conversationId,
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

function listTargets(conversationId) {
  const owner = requireConversationId(conversationId);
  return Array.from(views.values())
    .filter((entry) => entry.conversationId === owner)
    .map(targetState);
}

function selectEntry(id, conversationId) {
  const owner = requireConversationId(conversationId);
  const requestedId = typeof id === "string" ? id.trim() : "";
  const entry = requestedId
    ? views.get(assertViewId(requestedId))
    : (
        activeViewId && views.get(activeViewId)?.conversationId === owner
          ? views.get(activeViewId)
          : Array.from(views.values()).find((candidate) => candidate.conversationId === owner)
      );
  if (!entry) throw new Error("No embedded browser tab is available. Open the browser panel first.");
  if (entry.conversationId !== owner) throw new Error("Embedded browser tab belongs to another conversation.");
  if (entry.view.webContents.isDestroyed()) throw new Error("Embedded browser tab is no longer available.");
  return entry;
}

const scriptValue = (value) => JSON.stringify(value ?? null);

async function evaluateInEntry(entry, expression) {
  return entry.view.webContents.executeJavaScript(expression, true);
}

function controlMaxChars(value) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) return CONTROL_DEFAULT_MAX_CHARS;
  return Math.min(parsed, CONTROL_MAX_CHARS);
}

function pageStringExpression(expression, maxChars) {
  return `String(${expression}).slice(0, ${controlMaxChars(maxChars)})`;
}

function evaluatedValueExpression(expression, maxChars) {
  const limit = controlMaxChars(maxChars);
  return `(async () => {
    const value = await (${expression});
    if (typeof value === 'string') return value.slice(0, ${limit});
    try {
      const serialized = JSON.stringify(value);
      return typeof serialized === 'string' ? serialized.slice(0, ${limit}) : String(value).slice(0, ${limit});
    } catch {
      return String(value).slice(0, ${limit});
    }
  })()`;
}

function recentLogsWithin(entries, maxChars) {
  const limit = controlMaxChars(maxChars);
  const selected = [];
  let serializedChars = 2;
  for (let index = entries.length - 1; index >= 0 && selected.length < 100; index -= 1) {
    const entry = entries[index];
    const encoded = JSON.stringify(entry);
    const separatorChars = selected.length ? 1 : 0;
    if (serializedChars + separatorChars + encoded.length > limit) continue;
    selected.unshift(entry);
    serializedChars += separatorChars + encoded.length;
  }
  return selected;
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

function activate(id, conversationId) {
  const entry = selectEntry(id, conversationId);
  const requestedId = entry.id;
  activeViewId = requestedId;
  for (const [entryId, entry] of views) {
    entry.view.setVisible(entryId === requestedId);
  }
  return true;
}

function attachViewEvents(entry) {
  const webContents = entry.view.webContents;

  webContents.setWindowOpenHandler(({ url }) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed || decision.requiresPrivateNetworkApproval) {
      emit(entry, "new-tab-request", { requestedUrl: url });
    }
    return { action: "deny" };
  });

  webContents.on("will-navigate", (event, url) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed) return;
    event.preventDefault();
    if (decision.requiresPrivateNetworkApproval) {
      requestApprovedNavigation(entry, url);
      return;
    }
    emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
  });
  webContents.on("will-redirect", (event, url, _isInPlace, isMainFrame) => {
    if (!isMainFrame) return;
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed) return;
    event.preventDefault();
    if (decision.requiresPrivateNetworkApproval) {
      requestApprovedNavigation(entry, url);
      return;
    }
    emit(entry, "error", { error: decision.reason || "已阻止不安全的重定向。" });
  });
  webContents.on("did-start-loading", () => emit(entry, "loading"));
  webContents.on("did-stop-loading", () => emit(entry, "updated"));
  webContents.on("did-navigate", (_event, url) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (!decision.allowed) {
      try { webContents.stop(); } catch { /* renderer may already be gone */ }
      emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
      return;
    }
    entry.url = url;
    entry.faviconUrl = "";
    emit(entry, "updated");
  });
  webContents.on("did-navigate-in-page", (_event, url) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (!decision.allowed) {
      emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
      return;
    }
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
    entry.consoleLogs.push({
      level,
      message: String(message || "").slice(0, CONTROL_DEFAULT_MAX_CHARS),
      line,
      sourceId: String(sourceId || "").slice(0, CONTROL_DEFAULT_MAX_CHARS),
      timestamp: Date.now(),
    });
    if (entry.consoleLogs.length > 200) entry.consoleLogs.splice(0, entry.consoleLogs.length - 200);
  });
}

async function create(payload = {}) {
  const { id, url } = payload;
  const conversationId = conversationIdFrom(payload);
  const requestedId = assertViewId(id);
  let entry = views.get(requestedId);
  if (entry && entry.conversationId !== conversationId) {
    throw new Error("Embedded browser tab id belongs to another conversation.");
  }
  const navigation = await confirmNavigationUrl(url, conversationId, entry);
  const requestedUrl = navigation.url;
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
    const approvedPrivateOrigins = new Set();
    if (navigation.privateNetworkApproved) {
      const origin = normalizeOrigin(requestedUrl);
      if (origin) approvedPrivateOrigins.add(origin);
    }
    entry = {
      id: requestedId,
      conversationId,
      view,
      url: requestedUrl,
      title: "新标签页",
      faviconUrl: "",
      consoleLogs: [],
      networkLogs: [],
      approvedPrivateOrigins,
      pendingNavigationApprovals: new Set(),
    };
    views.set(requestedId, entry);
    entriesByWebContentsId.set(view.webContents.id, entry);
    configureGuestSession(view.webContents.session);
    attachViewEvents(entry);
    mainWindow.contentView.addChildView(view);
  }
  activate(requestedId, conversationId);
  await entry.view.webContents.loadURL(requestedUrl);
  return navigationState(entry, "updated");
}

function setBounds(payload = {}) {
  const { id, x, y, width, height } = payload;
  const entry = selectEntry(id, conversationIdFrom(payload));
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

async function navigate(payload = {}) {
  const { id, url } = payload;
  const conversationId = conversationIdFrom(payload);
  const requestedId = assertViewId(id);
  const entry = views.get(requestedId);
  if (!entry) return create({ id: requestedId, url, conversation_id: conversationId });
  if (entry.conversationId !== conversationId) throw new Error("Embedded browser tab belongs to another conversation.");
  const { url: requestedUrl } = await confirmNavigationUrl(url, conversationId, entry);
  activate(requestedId, conversationId);
  await entry.view.webContents.loadURL(requestedUrl);
  return navigationState(entry, "updated");
}

async function clearSiteData(payload = {}) {
  const entry = selectEntry(payload.id, conversationIdFrom(payload));
  const origin = normalizeOrigin(entry.view.webContents.getURL() || entry.url);
  if (!origin) return false;
  await entry.view.webContents.session.clearStorageData({
    origin,
    storages: ["cookies", "filesystem", "indexdb", "localstorage", "serviceworkers", "cachestorage"],
  });
  loadBrowserSettings();
  delete browserSettings.sitePermissions[origin];
  if (entry.approvedPrivateOrigins instanceof Set) entry.approvedPrivateOrigins.delete(origin);
  saveBrowserSettings();
  return true;
}

async function executeControlCommand(payload = {}) {
  const conversationId = conversationIdFrom(payload);
  const action = String(payload.action || "").trim().toLowerCase();
  if (action === "discover" || action === "list_targets") {
    return { ok: true, action, browser: "MiniCode Embedded Browser", targets: listTargets(conversationId) };
  }

  if (action === "navigate") {
    const url = typeof payload.url === "string" ? payload.url.trim() : "";
    const requestedTargetId = typeof payload.target_id === "string" ? payload.target_id.trim() : "";
    let entry;
    let alreadyNavigated = false;
    try {
      entry = selectEntry(requestedTargetId, conversationId);
    } catch (error) {
      if (requestedTargetId) throw error;
      const agentTabId = `agent_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
      await navigate({ id: agentTabId, url, conversation_id: conversationId });
      entry = selectEntry(agentTabId, conversationId);
      alreadyNavigated = true;
    }
    if (!alreadyNavigated) await navigate({ id: entry.id, url, conversation_id: conversationId });
    const waitMs = Math.max(0, Math.min(Number(payload.wait_ms) || 0, 5000));
    if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    return { ok: true, action, target: targetState(entry) };
  }
  const entry = selectEntry(payload.target_id, conversationId);
  const webContents = entry.view.webContents;
  if (action === "screenshot") {
    const image = await webContents.capturePage();
    const size = image.getSize();
    const png = image.toPNG();
    const base64Size = 4 * Math.ceil(png.length / 3);
    if (base64Size > API_IMAGE_MAX_BASE64_SIZE) {
      throw new Error("Embedded browser screenshot exceeds the 5 MiB API image limit.");
    }
    return { ok: true, action, target: targetState(entry), mimeType: "image/png", data: png.toString("base64"), width: size.width, height: size.height };
  }
  if (action === "get_url") return { ok: true, action, target: targetState(entry) };
  if (action === "get_text") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, pageStringExpression("document.body ? document.body.innerText : ''", payload.max_chars)) };
  if (action === "get_html") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, pageStringExpression("document.documentElement ? document.documentElement.outerHTML : ''", payload.max_chars)) };
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
  if (action === "get_console_logs") return { ok: true, action, target: targetState(entry), value: recentLogsWithin(entry.consoleLogs, payload.max_chars) };
  if (action === "get_network_logs") return { ok: true, action, target: targetState(entry), value: recentLogsWithin(entry.networkLogs, payload.max_chars) };
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
  if (action === "evaluate") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, evaluatedValueExpression(String(payload.expression || ""), payload.max_chars)) };
  throw new Error(`Unsupported embedded browser action: ${action}`);
}

function runNavigationAction(payload = {}) {
  const { id, action } = payload;
  const entry = selectEntry(id, conversationIdFrom(payload));
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

function closeEntry(entry) {
  const requestedId = entry.id;
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

function close(payload = {}) {
  const entry = selectEntry(payload.id, conversationIdFrom(payload));
  return closeEntry(entry);
}

function closeConversation(conversationId) {
  const owner = requireConversationId(conversationId);
  let closed = 0;
  for (const entry of Array.from(views.values())) {
    if (entry.conversationId !== owner) continue;
    if (closeEntry(entry)) closed += 1;
  }
  return closed;
}

function disposeAll() {
  for (const entry of Array.from(views.values())) closeEntry(entry);
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
  closeConversation,
  clearSiteData,
  disposeAll,
  assertNavigationUrl,
  isAllowedNavigationUrl,
  assessNavigationTarget,
  assessNavigationTargetForRequest,
  actualPeerNavigationError,
  normalizeViewBounds,
  listTargets,
  executeControlCommand,
  getBrowserSettings,
  setBrowserSettings,
  makeNetworkLogEntry,
  normalizeOrigin,
  normalizeBrowserSettings,
  availableDownloadPathIn,
  requireConversationId,
};

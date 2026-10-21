"use strict";

const { WebContentsView, app, dialog, session, shell, Menu } = require("electron");
const net = require("node:net");
const dns = require("node:dns").promises;
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { platform } = require("node:process");
const { atomicWriteTextSync } = require("./utils");
const { attachGuestAutofill } = require("./browser-autofill");

const DEFAULT_PARTITION = "persist:minicode-browser";
const GUEST_WEB_PREFERENCES = {
  contextIsolation: true, preload: path.join(__dirname, "browser-password-preload.js"),
  nodeIntegration: false, sandbox: true, partition: DEFAULT_PARTITION, spellcheck: true,
};
const ID_PATTERN = /^[a-zA-Z0-9_-]{1,96}$/;
const CONTROL_DEFAULT_MAX_CHARS = 20_000;
const CONTROL_MAX_CHARS = 200_000;
const API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024;

let getWindowByInstanceId = () => null;
let getMainWindows = () => [];
let getLocale = () => app.getLocale();
let getShortcutPreferences = require("./preferences").getPreferences;
let appendDesktopLog = () => {};
let onMenuStateChanged = () => {};
let assessBrowserNavigationPolicy = null;
let isOwnedPreviewUrl = () => false;
let lookupHostAddresses = (host) => dns.lookup(host, { all: true, verbatim: true });
let views = new Map();
const activeViewIds = new Map();
const pendingNavigations = new Map();
const configuredSessions = new WeakSet();
const entriesByWebContentsId = new Map();
const DOWNLOAD_POLICIES = new Set(["block", "ask", "allow"]);
const SITE_PERMISSIONS = new Set(["clipboard-read", "geolocation", "media", "notifications"]);
const SITE_CATEGORIES = [
  { id: "camera", defaultSetting: "ask", allowedSettings: ["ask", "allow", "block"] },
  { id: "microphone", defaultSetting: "ask", allowedSettings: ["ask", "allow", "block"] },
  { id: "geolocation", defaultSetting: "ask", allowedSettings: ["ask", "allow", "block"] },
  { id: "notifications", defaultSetting: "ask", allowedSettings: ["ask", "allow", "block"] },
  { id: "clipboard-read", defaultSetting: "ask", allowedSettings: ["ask", "allow", "block"] },
  { id: "mediaKeySystem", defaultSetting: "allow", allowedSettings: ["allow", "block"] },
  { id: "sound", defaultSetting: "allow", allowedSettings: ["allow", "block"] },
  { id: "images", defaultSetting: "allow", allowedSettings: ["allow", "block"] },
  { id: "javascript", defaultSetting: "allow", allowedSettings: ["allow", "block"] },
  { id: "usb", defaultSetting: "ask", allowedSettings: ["ask", "block"] },
];
const LEGACY_SITE_CATEGORIES = { media: ["camera", "microphone"], geolocation: ["geolocation"], notifications: ["notifications"], "clipboard-read": ["clipboard-read"] };
const sessionDeviceGrants = new Map();
let browserSettingsPath = "";
let browserSettingsLoaded = false;
const DEFAULT_BROWSER_SETTINGS = { downloadPolicy: "allow", downloadDirectory: null, browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, sitePermissions: {} };
let browserSettings = { ...DEFAULT_BROWSER_SETTINGS };
let browserHistory = null;
let downloadRecords = null;
const downloadItems = new Map();
let browserPersonalData = null;

const browserText = (english, chinese) => /^zh(?:[-_](?:cn|sg|hans.*))?$/i.test(getLocale()) ? chinese : english;
const PERMISSION_LABELS = { camera: ["camera", "摄像头"], microphone: ["microphone", "麦克风"], geolocation: ["location", "位置"],
  notifications: ["notifications", "通知"], "clipboard-read": ["clipboard", "剪贴板"], mediaKeySystem: ["protected content IDs", "受保护内容 ID"], usb: ["USB devices", "USB 设备"] };
const getActiveDownloadIds = () => [...downloadItems.keys()];

function downloadsPath() { return path.join(path.dirname(settingsPath()), "browser-downloads.json"); }
function listDownloads() {
  if (downloadRecords === null) {
    downloadRecords = fs.existsSync(downloadsPath()) ? JSON.parse(fs.readFileSync(downloadsPath(), "utf8")) : [];
    // Persisted entries have no DownloadItem after the owning app exited.
    downloadRecords = downloadRecords.map(record => ["progressing", "paused"].includes(record.state)
      ? { ...record, state: "interrupted" } : record);
  }
  return downloadRecords.map(record => {
    const item = downloadItems.get(record.id);
    const paused = item ? item.isPaused() : false;
    return { ...record, canPause: Boolean(item && record.state === "progressing" && !paused),
      canResume: Boolean(item && (paused || record.state === "interrupted") && item.canResume()), canCancel: Boolean(item),
      fileExists: fs.existsSync(record.path) };
  });
}
function saveDownloads() { atomicWriteTextSync(downloadsPath(), JSON.stringify(downloadRecords)); }
function publishDownloads() {
  for (const window of getMainWindows()) window.webContents.send("minicode:embeddedBrowser:downloadsChanged", listDownloads());
}
function claimAgentControl(entry, control) {
  if (control.agentControl) entry.agentControls.set(control.agentControl.id, control.agentControl);
}

function consumeAllowedDownload(entry, url) {
  const grant = entry.allowedDownload;
  if (grant && grant.grantedAtMs < Date.now() - 10000) entry.allowedDownload = null;
  if (!entry.allowedDownload || grant.url !== url || entry.agentControls.get(grant.control.id)?.turnId !== grant.control.turnId) return null;
  entry.allowedDownload = null;
  entry.blobDownload = null;
  return grant;
}

function closeDownloadDebugger(entry) {
  const owner = entry.downloadDebugger;
  if (!owner) return;
  entry.downloadDebugger = null;
  entry.blobDownload = null;
  owner.debugger.removeListener("message", owner.onMessage);
  owner.debugger.removeListener("detach", owner.onDetach);
  if (!entry.view.webContents.isDestroyed() && owner.debugger.isAttached()) owner.debugger.detach();
}

function downloadDebugger(entry) {
  if (entry.downloadDebugger) return entry.downloadDebugger.debugger;
  const debuggerApi = entry.view.webContents.debugger;
  if (entry.view.webContents.isDevToolsOpened() || debuggerApi.isAttached()) throw new Error("The original browser page debugger is owned by another consumer.");
  debuggerApi.attach("1.3");
  const owner = { debugger: debuggerApi, onMessage: (_event, method, params, sessionId) => {
    if (method !== "Page.downloadWillBegin" || typeof sessionId === "string" && sessionId.length > 0) return;
    const pending = entry.blobDownload;
    const parsed = typeof params?.url === "string" ? URL.parse(params.url) : null;
    if (!pending || parsed?.protocol !== "blob:" || parsed.origin !== pending.origin || params.frameId !== pending.frameId
      || pending.url !== null && params.url !== pending.url) return;
    entry.blobDownload = null;
    entry.allowedDownload = { url: params.url, grantedAtMs: Date.now(), control: pending.control };
  }, onDetach: () => {
    if (entry.downloadDebugger !== owner) return;
    entry.downloadDebugger = null;
    entry.blobDownload = null;
    debuggerApi.removeListener("message", owner.onMessage);
    debuggerApi.removeListener("detach", owner.onDetach);
  } };
  entry.downloadDebugger = owner;
  debuggerApi.on("message", owner.onMessage);
  debuggerApi.on("detach", owner.onDetach);
  return debuggerApi;
}

function releaseAgentControl({ conversation_id, window_instance_id, control }) {
  const conversationId = conversationIdFrom({ conversation_id });
  let released = 0;
  for (const entry of views.values()) {
    if (entry.windowInstanceId !== window_instance_id || entry.conversationId !== conversationId
      || entry.agentControls.get(control.id)?.turnId !== control.turnId) continue;
    entry.agentControls.delete(control.id);
    if (entry.allowedDownload?.control.id === control.id) entry.allowedDownload = null;
    if (entry.blobDownload?.control.id === control.id) entry.blobDownload = null;
    if (entry.agentControls.size === 0) closeDownloadDebugger(entry);
    released++;
  }
  return released;
}

async function runDownloadAction({ action, id }) {
  if (action === "open-folder") return shell.openPath(getBrowserSettings().downloadDirectory ?? app.getPath("downloads"));
  listDownloads();
  const record = downloadRecords.find(item => item.id === id);
  if (!record) throw new Error("Download record no longer exists");
  if (["pause", "resume", "cancel"].includes(action)) {
    const item = downloadItems.get(id);
    if (!item) throw new Error("Download is no longer active");
    item[action]();
    if (action !== "cancel") {
      record.state = item.isPaused() ? "paused" : item.getState();
      record.path = item.getSavePath();
      record.filename = path.basename(record.path);
      record.receivedBytes = item.getReceivedBytes();
      saveDownloads();
      publishDownloads();
    }
    return "";
  }
  if (action === "open") return shell.openPath(record.path);
  if (action === "reveal") { shell.showItemInFolder(record.path); return ""; }
  if (action === "remove") {
    if (downloadItems.has(id)) throw new Error("Cancel the active download before removing its history");
    downloadRecords = downloadRecords.filter(item => item.id !== id);
    saveDownloads();
    publishDownloads();
    return "";
  }
  throw new Error(`Unsupported download action: ${action}`);
}

function init(deps = {}) {
  if (deps.browserPersonalData) browserPersonalData = deps.browserPersonalData;
  if (deps.browserHistory) browserHistory = deps.browserHistory;
  if (typeof deps.getWindowByInstanceId === "function") getWindowByInstanceId = deps.getWindowByInstanceId;
  if (typeof deps.getMainWindows === "function") getMainWindows = deps.getMainWindows;
  if (typeof deps.getLocale === "function") getLocale = deps.getLocale;
  if (typeof deps.getShortcutPreferences === "function") getShortcutPreferences = deps.getShortcutPreferences;
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
  if (typeof deps.onMenuStateChanged === "function") onMenuStateChanged = deps.onMenuStateChanged;
  if (typeof deps.assessBrowserNavigationPolicy === "function") {
    assessBrowserNavigationPolicy = deps.assessBrowserNavigationPolicy;
  }
  if (typeof deps.isOwnedPreviewUrl === "function") isOwnedPreviewUrl = deps.isOwnedPreviewUrl;
  if (typeof deps.lookupHostAddresses === "function") lookupHostAddresses = deps.lookupHostAddresses;
  if (typeof deps.browserSettingsPath === "string" && deps.browserSettingsPath !== browserSettingsPath) {
    browserSettingsPath = deps.browserSettingsPath;
    browserSettingsLoaded = false;
    browserSettings = { ...DEFAULT_BROWSER_SETTINGS };
    downloadRecords = null;
    sessionDeviceGrants.clear();
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
  const downloadPolicy = DOWNLOAD_POLICIES.has(raw?.downloadPolicy) ? raw.downloadPolicy : DEFAULT_BROWSER_SETTINGS.downloadPolicy;
  const sitePermissions = {};
  if (raw?.sitePermissions && typeof raw.sitePermissions === "object") {
    for (const [rawOrigin, rawPermissions] of Object.entries(raw.sitePermissions)) {
      const origin = normalizeOrigin(rawOrigin);
      if (!origin || !Array.isArray(rawPermissions)) continue;
      const permissions = Array.from(new Set(rawPermissions.filter((item) => SITE_PERMISSIONS.has(item)))).sort();
      if (permissions.length) sitePermissions[origin] = permissions;
    }
  }
  return { downloadPolicy, downloadDirectory: raw.downloadDirectory ?? null, browserEnabled: raw.browserEnabled ?? true,
    deviceToolbarAlwaysOnNewTabs: raw.deviceToolbarAlwaysOnNewTabs ?? false, sitePermissions, ...(raw.sitePolicy ? { sitePolicy: raw.sitePolicy } : {}) };
}

function readSitePolicy() {
  loadBrowserSettings();
  const defaults = Object.fromEntries(SITE_CATEGORIES.map(category => [category.id, category.defaultSetting]));
  const origins = {};
  for (const [origin, permissions] of Object.entries(browserSettings.sitePermissions)) {
    origins[origin] = {};
    for (const permission of permissions) for (const category of LEGACY_SITE_CATEGORIES[permission]) origins[origin][category] = "allow";
  }
  const raw = browserSettings.sitePolicy;
  if (raw) {
    for (const [category, setting] of Object.entries(raw.defaults)) { validateSiteDecision(category, setting, true); defaults[category] = setting; }
    for (const [origin, settings] of Object.entries(raw.origins)) {
      if (normalizeOrigin(origin) !== origin) throw new Error("Saved browser site policy has an invalid origin.");
      origins[origin] ??= {};
      for (const [category, setting] of Object.entries(settings)) { validateSiteDecision(category, setting); origins[origin][category] = setting; }
    }
  }
  const devices = raw?.devices ?? [];
  if (!Array.isArray(devices) || devices.some(device => !device || typeof device.id !== "string" || normalizeOrigin(device.origin) !== device.origin
    || typeof device.name !== "string" || typeof device.deviceKey !== "string")) throw new Error("Saved browser device permissions are invalid.");
  return { defaults, origins, devices };
}

function validateSiteDecision(category, setting, defaultScope = false) {
  const definition = SITE_CATEGORIES.find(item => item.id === category);
  if (!definition || !definition.allowedSettings.includes(setting)) throw new Error("Unsupported browser site permission setting.");
  if (defaultScope && setting === "allow" && definition.allowedSettings.includes("ask")) throw new Error("This browser permission can only be allowed for an individual website.");
}

function effectiveSiteSetting(origin, category, policy = readSitePolicy()) {
  return policy.origins[origin]?.[category] ?? policy.defaults[category];
}

function getSitePolicy() {
  const policy = readSitePolicy();
  return { categories: SITE_CATEGORIES.map(({ id, allowedSettings }) => ({ id, allowedSettings, defaultSettings: allowedSettings.includes("ask") ? ["ask", "block"] : allowedSettings })), defaults: policy.defaults,
    sites: Object.entries(policy.origins).filter(([, settings]) => Object.keys(settings).length).map(([origin, settings]) => ({ origin, settings })).sort((left, right) => left.origin.localeCompare(right.origin)),
    devices: [...policy.devices, ...sessionDeviceGrants.values()].map(({ id, origin, name }) => ({ id, origin, name })) };
}

function setSitePolicy({ origin: rawOrigin, category, setting }) {
  const definition = SITE_CATEGORIES.find(item => item.id === category);
  if (!definition) throw new Error("Unsupported browser site permission category.");
  const origin = rawOrigin === undefined ? undefined : normalizeOrigin(rawOrigin);
  if (rawOrigin !== undefined && !origin) throw new Error("Browser site permissions require a valid HTTP or HTTPS origin.");
  if (setting !== "default") validateSiteDecision(category, setting, origin === undefined);
  const policy = readSitePolicy();
  if (origin === undefined) policy.defaults[category] = setting === "default" ? definition.defaultSetting : setting;
  else {
    policy.origins[origin] ??= {};
    if (setting === "default") delete policy.origins[origin][category]; else policy.origins[origin][category] = setting;
    if (!Object.keys(policy.origins[origin]).length) delete policy.origins[origin];
  }
  const legacy = { ...browserSettings.sitePermissions };
  if (origin) {
    // Once an origin is represented by explicit policies, retaining its legacy
    // media grant would re-enable a permission after Reset to default.
    delete legacy[origin];
  }
  saveBrowserSettings({ ...browserSettings, sitePermissions: legacy, sitePolicy: policy });
  for (const entry of views.values()) applyAudioPolicy(entry);
  return getSitePolicy();
}

function removeSiteDevice({ id }) {
  const policy = readSitePolicy();
  policy.devices = policy.devices.filter(item => item.id !== id);
  saveBrowserSettings({ ...browserSettings, sitePolicy: policy });
  sessionDeviceGrants.delete(id);
  return getSitePolicy();
}

function applyAudioPolicy(entry) {
  const origin = normalizeOrigin(entry.view.webContents.getURL());
  if (!origin) return;
  const muted = effectiveSiteSetting(origin, "sound") === "block";
  if (muted || entry.audioPolicyMuted) entry.view.webContents.setAudioMuted(muted);
  entry.audioPolicyMuted = muted;
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

function saveBrowserSettings(settings) {
  atomicWriteTextSync(settingsPath(), `${JSON.stringify(settings, null, 2)}\n`);
  browserSettings = settings;
  for (const window of getMainWindows()) {
    window.webContents.send("minicode:embeddedBrowser:settingsChanged", browserSettings);
  }
}

function getBrowserSettings(url = "") {
  loadBrowserSettings();
  const origin = normalizeOrigin(url);
  const policy = readSitePolicy();
  const allowed = origin ? Object.entries(LEGACY_SITE_CATEGORIES).filter(([, categories]) => categories.every(category => effectiveSiteSetting(origin, category, policy) === "allow")).map(([permission]) => permission).sort() : [];
  return { downloadPolicy: browserSettings.downloadPolicy, downloadDirectory: browserSettings.downloadDirectory, browserEnabled: browserSettings.browserEnabled,
    deviceToolbarAlwaysOnNewTabs: browserSettings.deviceToolbarAlwaysOnNewTabs, origin, permissions: allowed };
}

function setBrowserSettings(payload = {}) {
  loadBrowserSettings();
  let policy;
  if (payload.downloadPolicy != null) {
    policy = String(payload.downloadPolicy);
    if (!DOWNLOAD_POLICIES.has(policy)) throw new Error("Invalid browser download policy.");
  }
  const origin = normalizeOrigin(payload.origin);
  const permission = String(payload.permission || "");
  if (payload.origin != null && String(payload.origin).trim() && !origin) {
    throw new Error("Browser site permissions require a valid HTTP or HTTPS origin.");
  }
  if (permission && !origin) throw new Error("Browser site permissions require a valid origin.");
  if (origin && permission) {
    if (!SITE_PERMISSIONS.has(permission)) throw new Error("Unsupported browser site permission.");
  }
  const next = { ...browserSettings, sitePermissions: { ...browserSettings.sitePermissions } };
  for (const key of ["browserEnabled", "deviceToolbarAlwaysOnNewTabs"]) {
    if (payload[key] !== undefined) {
      if (typeof payload[key] !== "boolean") throw new Error(`${key} must be a boolean.`);
      next[key] = payload[key];
    }
  }
  if (policy != null) next.downloadPolicy = policy;
  if (payload.downloadDirectory !== undefined) next.downloadDirectory = payload.downloadDirectory;
  if (origin && permission) {
    const current = new Set(next.sitePermissions[origin] || []);
    if (payload.allowed) current.add(permission); else current.delete(permission);
    if (current.size) next.sitePermissions[origin] = Array.from(current).sort();
    else delete next.sitePermissions[origin];
    const policy = readSitePolicy();
    policy.origins[origin] ??= {};
    for (const category of LEGACY_SITE_CATEGORIES[permission]) policy.origins[origin][category] = payload.allowed ? "allow" : "block";
    next.sitePolicy = policy;
  }
  saveBrowserSettings(next);
  return getBrowserSettings(origin);
}

function sitePermissionAllowed(webContents, permission, requestingOrigin = "", details = {}) {
  const url = requestingOrigin || details.requestingUrl || webContents?.getURL?.() || "";
  const origin = normalizeOrigin(url);
  if (!origin) return false;
  const categories = permissionCategories(permission, details);
  const entry = webContents && entriesByWebContentsId.get(webContents.id);
  return categories.length > 0 && categories.every(category => effectiveSiteSetting(origin, category) !== "block" && (effectiveSiteSetting(origin, category) === "allow"
    || entry?.documentGrants.get(origin)?.has(category) || category === "usb"));
}

function permissionCategories(permission, details) {
  if (permission === "media") {
    const types = details.mediaTypes ?? (details.mediaType && details.mediaType !== "unknown" ? [details.mediaType] : ["video", "audio"]);
    return types.map(type => type === "video" ? "camera" : "microphone");
  }
  return SITE_CATEGORIES.some(item => item.id === permission && !["sound", "images", "javascript"].includes(permission)) ? [permission] : [];
}

function requestGuestPermission(contents, permission, callback, details) {
  const entry = entriesByWebContentsId.get(contents.id);
  const origin = normalizeOrigin(details.requestingUrl || details.securityOrigin || contents.getURL());
  const categories = permissionCategories(permission, details);
  if (!entry || !origin || !categories.length) { callback(false); return; }
  if (sitePermissionAllowed(contents, permission, origin, details)) { callback(true); return; }
  if (categories.some(category => effectiveSiteSetting(origin, category) === "block")) { callback(false); return; }
  entry.permissionRequests.add({ origin, categories, callback, controller: null });
  void presentGuestPermission(entry);
}

function dismissGuestPermissions(entry) {
  for (const request of entry.permissionRequests) {
    entry.permissionRequests.delete(request);
    request.controller?.abort();
    request.callback(false);
  }
}

async function presentGuestPermission(entry) {
  if (!entry.presented || entry.permissionPrompt) return;
  const request = entry.permissionRequests.values().next().value;
  if (!request) return;
  const { origin, categories } = request;
  const description = categories.map(category => browserText(...PERMISSION_LABELS[category])).join(browserText(", ", "、"));
  // The callback remains owned by this page while its tab is hidden. Native
  // presentation can be cancelled independently of the permission decision.
  const controller = new AbortController();
  request.controller = controller;
  entry.permissionPrompt = request;
  try {
    const { response } = await dialog.showMessageBox(entry.window, { type: "question", message: browserText(`Allow ${new URL(origin).hostname} to access ${description}?`, `允许 ${new URL(origin).hostname} 访问${description}吗？`),
      buttons: [browserText("Block", "阻止"), browserText("Allow this time", "本次允许"), browserText("Allow", "允许")], defaultId: 0, cancelId: 0, noLink: true,
      signal: controller.signal });
    if (controller.signal.aborted || !entry.permissionRequests.has(request)) return;
    const setting = categories.map(category => effectiveSiteSetting(origin, category));
    if (setting.includes("block")) { entry.permissionRequests.delete(request); request.callback(false); return; }
    if (response === 1) {
      const granted = entry.documentGrants.get(origin) ?? new Set();
      for (const category of categories) granted.add(category);
      entry.documentGrants.set(origin, granted);
    } else {
      const policy = readSitePolicy();
      policy.origins[origin] ??= {};
      for (const category of categories) policy.origins[origin][category] = response === 2 ? "allow" : "block";
      const legacy = { ...browserSettings.sitePermissions }; delete legacy[origin];
      saveBrowserSettings({ ...browserSettings, sitePermissions: legacy, sitePolicy: policy });
    }
    entry.permissionRequests.delete(request);
    request.callback(response !== 0);
  } catch (error) {
    if (entry.permissionRequests.delete(request)) {
      request.callback(false);
      emit(entry, "error", { error: error instanceof Error ? error.message : String(error) });
    }
  } finally {
    entry.permissionPrompt = null;
    void presentGuestPermission(entry);
  }
}

function usbIdentity(device) {
  return JSON.stringify([device.vendorId, device.productId, device.serialNumber || device.deviceId]);
}

function usbPermissionAllowed({ deviceType, origin, device }) {
  if (deviceType !== "usb" || effectiveSiteSetting(origin, "usb") === "block") return false;
  return [...readSitePolicy().devices, ...sessionDeviceGrants.values()].some(item => item.origin === origin && item.deviceKey === usbIdentity(device));
}

async function selectUsbDevice(event, details, callback) {
  event.preventDefault();
  const entry = [...views.values()].find(item => item.view.webContents.mainFrame === details.frame?.top);
  const origin = normalizeOrigin(details.frame?.url);
  if (!entry || !entry.presented || !origin || effectiveSiteSetting(origin, "usb") === "block") { callback(""); return; }
  const navigation = entry.navigationVersion;
  try {
    const { response } = await dialog.showMessageBox(entry.window, { type: "question", message: `${new URL(origin).hostname} wants to connect to a USB device`,
      buttons: [...details.deviceList.map(device => device.productName || device.manufacturerName || "USB device"), "Cancel"], defaultId: details.deviceList.length, cancelId: details.deviceList.length, noLink: true });
    const device = details.deviceList[response];
    if (!device || entry.window.isDestroyed() || entry.view.webContents.isDestroyed() || navigation !== entry.navigationVersion) { callback(""); return; }
    const grant = { id: randomUUID(), origin, name: device.productName || device.manufacturerName || "USB device", deviceKey: usbIdentity(device) };
    if (device.serialNumber) {
      const policy = readSitePolicy();
      policy.devices = [...policy.devices.filter(item => item.origin !== origin || item.deviceKey !== grant.deviceKey), grant];
      saveBrowserSettings({ ...browserSettings, sitePolicy: policy });
    } else sessionDeviceGrants.set(grant.id, grant);
    callback(device.deviceId);
  } catch (error) { callback(""); emit(entry, "error", { error: error instanceof Error ? error.message : String(error) }); }
}

async function handlePasswordSubmission(event, payload) {
  const entry = entriesByWebContentsId.get(event.sender.id);
  const frame = event.senderFrame;
  if (!entry || !browserPersonalData || !entry.presented || frame !== event.sender.mainFrame || !frame) return false;
  const origin = normalizeOrigin(frame.url);
  if (!origin || payload?.origin !== origin || payload.documentUrl !== frame.url
    || typeof payload.username !== "string" || typeof payload.password !== "string" || !payload.password
    || payload.oldPassword !== undefined && typeof payload.oldPassword !== "string" || entry.passwordSavePending) return false;
  entry.passwordSavePending = true;
  try {
    const status = browserPersonalData.listPasswords();
    if (!status.available) return false;
    let username = payload.username.trim();
    let previous = status.items.find(item => item.origin === origin && item.username === username);
    if (!previous && !username && payload.oldPassword) {
      const matches = status.items.filter(item => item.origin === origin && browserPersonalData.getPassword({ id: item.id }).password === payload.oldPassword);
      if (matches.length === 1) { previous = matches[0]; username = previous.username; }
    }
    if (previous && browserPersonalData.getPassword({ id: previous.id }).password === payload.password) return false;
    const { response } = await dialog.showMessageBox(entry.window, { type: "question", message: previous ? browserText("Update password?", "更新密码？") : browserText("Save password?", "保存密码？"),
      detail: [new URL(origin).hostname, username].filter(Boolean).join("\n"), buttons: [browserText("Not now", "暂不"), previous ? browserText("Update", "更新") : browserText("Save", "保存")], defaultId: 0, cancelId: 0, noLink: true });
    // A form normally navigates while this confirmation is open. Keep the
    // captured origin, and require the same still-presented conversation view.
    if (response !== 1 || entry.view.webContents.isDestroyed() || entry.window.isDestroyed() || !entry.presented || views.get(entry.key) !== entry) return false;
    browserPersonalData.savePassword({ ...(previous ? { id: previous.id } : {}), origin, username, password: payload.password });
    return true;
  } catch (error) {
    let message = (error instanceof Error ? error.message : String(error)).replaceAll(payload.password, "[redacted]");
    if (payload.oldPassword) message = message.replaceAll(payload.oldPassword, "[redacted]");
    emit(entry, "error", { error: message });
    return false;
  } finally { entry.passwordSavePending = false; }
}

function handleLinkClick(event, payload) {
  const entry = entriesByWebContentsId.get(event.sender.id);
  const frame = event.senderFrame;
  if (!entry || !entry.presented || frame !== event.sender.mainFrame || !frame
    || frame.detached || frame.isDestroyed()
    || views.get(entry.key) !== entry || payload.documentUrl !== frame.url
    || !normalizeOrigin(frame.url) || !normalizeOrigin(payload.url)) return false;
  const { ctrlKey, metaKey, altKey, shiftKey } = payload.modifiers;
  const primary = platform === "darwin" ? metaKey && !ctrlKey : ctrlKey && !metaKey;
  const shortcut = getShortcutPreferences().externalBrowserLinkShortcut;
  const matches = shortcut === "alt" ? altKey && !ctrlKey && !metaKey && !shiftKey
    : (shortcut === "primary" || shortcut === "primaryShift") && primary && !altKey && shiftKey === (shortcut === "primaryShift");
  if (!matches) return false;
  emit(entry, "external-link-request", { requestedUrl: payload.url });
  return true;
}

function listSitePermissions() {
  return getSitePolicy().sites.map(({ origin }) => ({ origin, permissions: getBrowserSettings(origin).permissions }));
}

function removeSitePermissions({ origin: rawOrigin }) {
  const origin = normalizeOrigin(rawOrigin);
  if (!origin) throw new Error("Browser site permissions require a valid HTTP or HTTPS origin.");
  loadBrowserSettings();
  const next = { ...browserSettings, sitePermissions: { ...browserSettings.sitePermissions } };
  delete next.sitePermissions[origin];
  const policy = readSitePolicy();
  delete policy.origins[origin];
  policy.devices = policy.devices.filter(item => item.origin !== origin);
  next.sitePolicy = policy;
  saveBrowserSettings(next);
  for (const [id, device] of sessionDeviceGrants) if (device.origin === origin) sessionDeviceGrants.delete(id);
  for (const entry of views.values()) applyAudioPolicy(entry);
}

function availableDownloadPathIn(downloads, filename, activePaths = []) {
  const safeName = path.basename(String(filename || "download"));
  const extension = path.extname(safeName);
  const stem = path.basename(safeName, extension);
  let candidate = path.join(downloads, safeName);
  const reserved = new Set(activePaths.map(value => path.normalize(value)));
  for (let index = 1; fs.existsSync(candidate) || reserved.has(candidate); index += 1) {
    candidate = path.join(downloads, `${stem} (${index})${extension}`);
  }
  return candidate;
}

function availableDownloadPath(filename) {
  return availableDownloadPathIn(browserSettings.downloadDirectory ?? app.getPath("downloads"), filename,
    Array.from(downloadItems.values(), item => item.getSavePath()));
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

function requireWindow(windowInstanceId) {
  const window = getWindowByInstanceId(windowInstanceId);
  if (!window || window.isDestroyed()) throw new Error("Embedded browser window owner is unavailable.");
  return window;
}

function viewKey(windowInstanceId, id) {
  return JSON.stringify([windowInstanceId, id]);
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

async function confirmNavigationUrl(value, conversationId = "", entry = null, control = {}) {
  const url = typeof value === "string" ? value.trim() : "";
  const decision = await assessNavigationTargetForRequest(url, conversationId, entry);
  if (decision.allowed) {
    return {
      url,
      privateNetworkApproved: decision.risk === "local" || decision.risk === "private",
    };
  }
  if (!decision.requiresPrivateNetworkApproval) throw new Error(decision.reason);
  const authorization = control.navigationAuthorization;
  if (authorization?.kind === "owned_preview" && authorization.url === url
    && authorization.conversation_id === conversationId) {
    // Backend tool admission already established the live preview's owner.
    // This exact origin grant also covers Chromium redirects/peer checks for
    // that preview; another local/private target still needs its own decision.
    return { url, privateNetworkApproved: true };
  }
  const owner = entry ? entry.window : control.window;
  if (!owner || owner.isDestroyed()) throw new Error("Embedded browser window owner is unavailable.");
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
  control.signal?.throwIfAborted();
  if (result.response !== 1) throw new Error("Local or private browser navigation was cancelled.");
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
  const started = entry.networkStarted?.get(details.id);
  entry.networkStarted?.delete(details.id);
  entry.networkLogs.push({ ...makeNetworkLogEntry(details, error), durationMs: started == null ? undefined : Math.max(0, Date.now() - started) });
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
    void requestGuestPermission(webContents, permission, callback, details);
  });
  session.setDevicePermissionHandler(usbPermissionAllowed);
  session.on("select-usb-device", (event, details, callback) => { void selectUsbDevice(event, details, callback); });
  session.on("will-download", (event, item, webContents) => {
    const policy = browserSettings.downloadPolicy;
    if (policy === "block") {
      event.preventDefault();
      return;
    }
    const entry = webContents && entriesByWebContentsId.get(webContents.id);
    const grant = entry ? consumeAllowedDownload(entry, item.getURL()) : null;
    if (!entry || (entry.agentControls.size > 0 && !grant)) {
      event.preventDefault(); item.cancel(); return;
    }
    if (policy === "ask" && !grant) {
      const options = {
        title: browserText("Save download file", "保存下载文件"),
        defaultPath: availableDownloadPath(item.getFilename()),
      };
      const owner = entry.window;
      if (!owner || owner.isDestroyed()) { event.preventDefault(); return; }
      const savePath = dialog.showSaveDialogSync(owner, options);
      if (!savePath) {
        event.preventDefault();
        return;
      }
      item.setSavePath(savePath);
    } else {
      item.setSavePath(availableDownloadPath(item.getFilename()));
    }
    listDownloads();
    const record = { id: randomUUID(), filename: path.basename(item.getSavePath()), url: item.getURL(), path: item.getSavePath(),
      conversationId: entry.conversationId, windowInstanceId: entry.windowInstanceId, tabId: entry.id, isUserInitiated: grant === null,
      state: "progressing", receivedBytes: 0, totalBytes: item.getTotalBytes(), startedAt: Date.now() };
    downloadRecords.unshift(record);
    downloadItems.set(record.id, item);
    saveDownloads();
    publishDownloads();
    item.on("updated", (_event, state) => {
      Object.assign(record, { state: state === "progressing" && item.isPaused() ? "paused" : state,
        path: item.getSavePath(), filename: path.basename(item.getSavePath()),
        receivedBytes: item.getReceivedBytes(), totalBytes: item.getTotalBytes() });
      publishDownloads();
    });
    item.once("done", (_event, state) => {
      Object.assign(record, { state, path: item.getSavePath(), filename: path.basename(item.getSavePath()), receivedBytes: item.getReceivedBytes() });
      downloadItems.delete(record.id);
      saveDownloads();
      publishDownloads();
    });
  });
  // Keep a compact, per-tab request history for page diagnostics.  The log is
  // intentionally metadata-only: request/response headers and bodies can
  // contain cookies, authorization values, or user data.
  const webRequest = session.webRequest;
  const navigationFilter = { urls: ["http://*/*", "https://*/*"] };
  webRequest.onHeadersReceived(navigationFilter, (details, callback) => {
    if (!["mainFrame", "subFrame"].includes(details.resourceType) || !entriesByWebContentsId.has(Number(details.webContentsId))) { callback({}); return; }
    const origin = normalizeOrigin(details.url);
    const directives = [];
    if (effectiveSiteSetting(origin, "javascript") === "block") directives.push("script-src 'none'");
    if (effectiveSiteSetting(origin, "images") === "block") directives.push("img-src 'none'");
    if (!directives.length) { callback({}); return; }
    const headers = details.responseHeaders ?? {};
    const key = Object.keys(headers).find(name => name.toLowerCase() === "content-security-policy") ?? "Content-Security-Policy";
    callback({ responseHeaders: { ...headers, [key]: [...(headers[key] ?? []), directives.join("; ")] } });
  });
  if (webRequest?.onBeforeRequest) {
    webRequest.onBeforeRequest(navigationFilter, (details, callback) => {
      const owner = entriesByWebContentsId.get(Number(details.webContentsId));
      if (owner) {
        owner.networkStarted ??= new Map();
        owner.networkStarted.set(details.id, Date.now());
      }
      if (details.resourceType !== "mainFrame") {
        const blocked = details.resourceType === "image" && owner && effectiveSiteSetting(normalizeOrigin(owner.view.webContents.getURL() || owner.url), "images") === "block";
        callback({ cancel: Boolean(blocked) });
        return;
      }
      const entry = entriesByWebContentsId.get(Number(details.webContentsId));
      if (!entry) {
        callback({ cancel: true });
        return;
      }
      const request = pendingNavigations.get(entry.key);
      const control = request?.control || {};
      void confirmNavigationUrl(details.url, entry.conversationId, entry, control)
        .then((navigation) => {
          control.signal?.throwIfAborted();
          if (views.get(entry.key) !== entry || entry.view.webContents.isDestroyed()
            || (request && pendingNavigations.get(entry.key) !== request)) {
            throw new Error("Browser navigation was cancelled.");
          }
          if (navigation.privateNetworkApproved) entry.approvedPrivateOrigins.add(normalizeOrigin(navigation.url));
          // Continue the original Chromium request after approval. Replacing it
          // with loadURL would drop POST bodies, referrers and redirect methods.
          callback({ cancel: false });
        })
        .catch((error) => {
          callback({ cancel: true });
          emit(entry, "error", {
            error: error instanceof Error ? error.message : String(error),
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
    active: entry.id === activeViewIds.get(entry.windowInstanceId),
    loading: state.loading,
    canGoBack: state.canGoBack,
    canGoForward: state.canGoForward,
  };
}

function listTargets(conversationId, windowInstanceId) {
  const owner = requireConversationId(conversationId);
  requireWindow(windowInstanceId);
  return Array.from(views.values())
    .filter((entry) => entry.conversationId === owner && entry.windowInstanceId === windowInstanceId)
    .map(targetState);
}

function selectEntry(id, conversationId, windowInstanceId) {
  const owner = requireConversationId(conversationId);
  requireWindow(windowInstanceId);
  const activeViewId = activeViewIds.get(windowInstanceId);
  const activeEntry = views.get(viewKey(windowInstanceId, activeViewId));
  const requestedId = typeof id === "string" ? id.trim() : "";
  const entry = requestedId
    ? views.get(viewKey(windowInstanceId, assertViewId(requestedId)))
    : (
        activeEntry?.conversationId === owner
          ? activeEntry
          : Array.from(views.values()).find((candidate) => candidate.conversationId === owner && candidate.windowInstanceId === windowInstanceId)
      );
  if (!entry) throw new Error("No embedded browser tab is available. Open the browser panel first.");
  if (entry.conversationId !== owner) throw new Error("Embedded browser tab belongs to another conversation.");
  if (entry.view.webContents.isDestroyed()) throw new Error("Embedded browser tab is no longer available.");
  return entry;
}

const scriptValue = (value) => JSON.stringify(value ?? null);

async function evaluateInEntry(entry, expression, control = {}) {
  control.signal?.throwIfAborted();
  control.onSubmitted?.();
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

async function waitForSelector(entry, selector, timeoutMs, control = {}) {
  const timeout = timeoutMs == null ? 5000 : Number(timeoutMs);
  if (!Number.isFinite(timeout)) throw new Error("timeout_ms must be a finite number");
  const deadline = Date.now() + Math.max(0, Math.min(timeout, 30000));
  while (true) {
    if (await evaluateInEntry(entry, `Boolean(document.querySelector(${scriptValue(selector)}))`, control)) return true;
    const remaining = deadline - Date.now();
    if (remaining <= 0) return false;
    await new Promise((resolve) => setTimeout(resolve, Math.min(150, remaining)));
  }
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
    const fiberKey = Object.keys(target).find((key) => key.startsWith('__reactFiber$'));
    let fiber = fiberKey ? target[fiberKey] : null;
    while (fiber && !fiber._debugSource) fiber = fiber.return;
    const source = fiber?._debugSource;
    cleanup({
      selector: selectorFor(target),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
      text: String(target.innerText || target.textContent || '').trim().slice(0, 240),
      source: source ? { path: source.fileName, line: source.lineNumber, column: source.columnNumber } : undefined
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
  // Live callbacks from a disposed tab must not republish it after closure.
  if (views.get(entry.key) !== entry) return;
  const mainWindow = entry.window;
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send("minicode:embeddedBrowser:event", navigationState(entry, type, extra));
}

function activate(id, conversationId, windowInstanceId) {
  const entry = selectEntry(id, conversationId, windowInstanceId);
  const requestedId = entry.id;
  activeViewIds.set(windowInstanceId, requestedId);
  for (const candidate of views.values()) {
    if (candidate.windowInstanceId === windowInstanceId) syncViewPresentation(candidate);
  }
  return true;
}

function syncFrameCapture(entry) {
  const contents = entry.view.webContents;
  if (!entry.presented || entry.captureWaiters.size === 0 || entry.captureInFlight) return;
  entry.captureInFlight = true;
  const backgroundThrottling = contents.getBackgroundThrottling();
  contents.setBackgroundThrottling(false);
  // Resize and visibility reach Chromium asynchronously. Two actual animation
  // frames let the presented viewport paint before requesting its compositor
  // image; this does not poll or substitute another page's rendering.
  void contents.executeJavaScript("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))")
    .then(async () => {
      if (!entry.presented || entry.captureWaiters.size === 0) return;
      const image = await contents.capturePage();
      const error = image.isEmpty() ? new Error("Browser compositor returned an empty frame.") : null;
      for (const complete of Array.from(entry.captureWaiters)) complete(image, error);
    })
    .catch((error) => {
      for (const complete of Array.from(entry.captureWaiters)) complete(null, error);
    })
    .finally(() => {
      if (!contents.isDestroyed()) contents.setBackgroundThrottling(backgroundThrottling);
      entry.captureInFlight = false;
      syncFrameCapture(entry);
    });
}

function syncViewPresentation(entry) {
  const bounds = entry.view.getBounds();
  const wasPresented = entry.presented;
  entry.presented = entry.layoutBound && activeViewIds.get(entry.windowInstanceId) === entry.id && bounds.width > 0 && bounds.height > 0;
  entry.view.setVisible(entry.presented);
  if (entry.presented) void presentGuestPermission(entry);
  else entry.permissionPrompt?.controller.abort();
  if (wasPresented !== entry.presented) onMenuStateChanged(entry.window);
  syncFrameCapture(entry);
}

function capturePresentedPage(entry, control) {
  control.signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const complete = (image, error) => {
      entry.captureWaiters.delete(complete);
      control.signal?.removeEventListener("abort", cancelled);
      syncFrameCapture(entry);
      if (error) reject(error); else resolve(image);
    };
    const cancelled = () => complete(null, control.signal.reason);
    entry.captureWaiters.add(complete);
    control.signal?.addEventListener("abort", cancelled, { once: true });
    syncFrameCapture(entry);
  });
}

function attachViewEvents(entry) {
  const webContents = entry.view.webContents;
  attachGuestAutofill(entry, () => browserPersonalData, { Menu, dialog, text: browserText, appendDesktopLog });

  webContents.setWindowOpenHandler(({ url }) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed || decision.requiresPrivateNetworkApproval) {
      return { action: "allow", overrideBrowserWindowOptions: { webPreferences: GUEST_WEB_PREFERENCES }, createWindow: options => {
        const child = createViewEntry({ id: randomUUID(), conversationId: entry.conversationId,
          windowInstanceId: entry.windowInstanceId, mainWindow: entry.window, url }, options);
        child.agentControls = new Map(entry.agentControls);
        if (approvedPrivateOrigin(entry, url)) child.approvedPrivateOrigins.add(normalizeOrigin(url));
        emit(child, "page");
        return child.view.webContents;
      } };
    }
    return { action: "deny" };
  });

  webContents.on("will-navigate", (event, url) => {
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed || decision.requiresPrivateNetworkApproval) return;
    event.preventDefault();
    emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
  });
  webContents.on("will-redirect", (event, url, _isInPlace, isMainFrame) => {
    if (!isMainFrame) return;
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (decision.allowed || decision.requiresPrivateNetworkApproval) return;
    event.preventDefault();
    emit(entry, "error", { error: decision.reason || "已阻止不安全的重定向。" });
  });
  webContents.on("did-start-loading", () => emit(entry, "loading"));
  webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || input.isComposing) return;
    const mac = platform === "darwin";
    const primary = mac ? input.meta && !input.control && !input.alt : input.control && !input.meta && !input.alt;
    const secondary = mac ? input.control && !input.meta && !input.alt : input.alt && !input.control && !input.meta;
    if (entry.presented && /^[1-9]$/.test(input.key) && !input.shift && (primary || secondary)) {
      const accelerator = `${primary ? "CommandOrControl" : mac ? "Control" : "Alt"}+${input.key}`;
      const aliases = primary ? [accelerator, `${mac ? "Command" : "Control"}+${input.key}`, `${mac ? "Cmd" : "Ctrl"}+${input.key}`]
        : mac ? [accelerator, `Ctrl+${input.key}`] : [accelerator];
      const hasBinding = (menu) => menu.items.some(item => item.enabled &&
        (aliases.includes(item.accelerator) || item.submenu && hasBinding(item.submenu)));
      const menu = Menu.getApplicationMenu();
      if (menu && hasBinding(menu)) return;
      const primaryTarget = getShortcutPreferences().primaryNumberShortcutTarget;
      const target = primary ? primaryTarget : primaryTarget === "tabs" ? "sidebar" : "tabs";
      event.preventDefault();
      emit(entry, "numbered-shortcut", { numberedShortcut: { target, index: Number(input.key) - 1 } });
      return;
    }
    if (!(input.control || input.meta)) return;
    const key = input.key.toLowerCase();
    if (key === "f") { event.preventDefault(); emit(entry, "find-request"); }
    if (key === "p") { event.preventDefault(); emit(entry, "print-request"); }
  });
  webContents.on("found-in-page", (_event, result) => emit(entry, "find-result", { findResult: result }));
  webContents.on("did-stop-loading", () => emit(entry, "updated"));
  webContents.on("did-navigate", (_event, url) => {
    dismissGuestPermissions(entry);
    if (entry.blobDownload && normalizeOrigin(url) !== entry.blobDownload.origin) entry.blobDownload = null;
    entry.navigationVersion++;
    entry.documentGrants.clear();
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (!decision.allowed) {
      try { webContents.stop(); } catch { /* renderer may already be gone */ }
      emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
      return;
    }
    entry.url = url;
    applyAudioPolicy(entry);
    entry.faviconUrl = "";
    if (browserHistory) entry.historyId = browserHistory.recordVisit(url, entry.title || "", entry.visitSource);
    emit(entry, "updated");
  });
  webContents.on("did-navigate-in-page", (_event, url) => {
    dismissGuestPermissions(entry);
    if (entry.blobDownload && normalizeOrigin(url) !== entry.blobDownload.origin) entry.blobDownload = null;
    entry.navigationVersion++;
    const decision = assessNavigationTarget(url, entry.conversationId, entry);
    if (!decision.allowed) {
      emit(entry, "error", { error: decision.reason || "已阻止不安全的页面跳转。" });
      return;
    }
    entry.url = url;
    applyAudioPolicy(entry);
    if (browserHistory) entry.historyId = browserHistory.recordVisit(url, entry.title || "", entry.visitSource);
    emit(entry, "updated");
  });
  webContents.on("page-title-updated", (event, title) => {
    event.preventDefault();
    entry.title = title;
    if (browserHistory) browserHistory.updateTitle(entry.historyId, title);
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
    dismissGuestPermissions(entry);
    entry.blobDownload = null;
    appendDesktopLog(`[desktop] embedded browser renderer exited: ${details?.reason || "unknown"}`);
    emit(entry, "error", { error: "页面进程已退出，请刷新后重试。" });
  });
  webContents.on("destroyed", () => { dismissGuestPermissions(entry); closeDownloadDebugger(entry); });
  webContents.on("console-message", ({ level, message, lineNumber: line, sourceId }) => {
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

function createViewEntry({ id, conversationId, windowInstanceId, mainWindow, url }, options = {}) {
  // Chromium's child already owns its navigation, POST body and opener. The
  // createWindow contract requires adopting and returning that exact contents.
  const view = new WebContentsView({ ...options,
    webPreferences: { ...options.webPreferences, ...GUEST_WEB_PREFERENCES },
  });
  const entry = {
    id, key: viewKey(windowInstanceId, id), windowInstanceId, window: mainWindow,
    conversationId, view, url, title: "新标签页", faviconUrl: "", visitSource: "other",
    consoleLogs: [], networkLogs: [], navigationVersion: 0, documentGrants: new Map(),
    permissionRequests: new Set(), permissionPrompt: null, agentControls: new Map(), allowedDownload: null,
    blobDownload: null, downloadDebugger: null,
    approvedPrivateOrigins: new Set(),
    layoutBound: false, presented: false, captureWaiters: new Set(), captureInFlight: false,
  };
  views.set(entry.key, entry);
  entriesByWebContentsId.set(view.webContents.id, entry);
  configureGuestSession(view.webContents.session);
  attachViewEvents(entry);
  mainWindow.contentView.addChildView(view);
  // Attaching initializes native bounds. Keep a layout viewport for an agent
  // or a native child while its panel has not yet provided presented bounds.
  const { width, height } = mainWindow.getContentBounds();
  view.setBounds({ x: 0, y: 0, width, height });
  view.setVisible(false);
  return entry;
}

async function create(payload = {}, control = {}) {
  control.signal?.throwIfAborted();
  const { id, url } = payload;
  const conversationId = conversationIdFrom(payload);
  const windowInstanceId = payload.window_instance_id;
  const mainWindow = requireWindow(windowInstanceId);
  const requestedId = assertViewId(id);
  const key = viewKey(windowInstanceId, requestedId);
  let entry = views.get(key);
  const claimedOwner = entry?.conversationId || pendingNavigations.get(key)?.conversationId;
  if (claimedOwner && claimedOwner !== conversationId) {
    throw new Error("Embedded browser tab id belongs to another conversation.");
  }
  control = { ...control, window: mainWindow };
  const request = { conversationId, windowInstanceId, control };
  pendingNavigations.set(key, request);
  const assertCurrent = () => {
    if (pendingNavigations.get(key) !== request || mainWindow.isDestroyed()) throw new Error("Browser navigation was cancelled.");
  };
  try {
    const navigation = await confirmNavigationUrl(url, conversationId, entry, control);
    control.signal?.throwIfAborted();
    assertCurrent();
    const requestedUrl = navigation.url;
    // Approval/DNS can yield; only this still-owned request may publish a view.
    entry = views.get(key);
    if (!entry) {
      entry = createViewEntry({ id: requestedId, conversationId, windowInstanceId, mainWindow, url: requestedUrl });
    }
    if (navigation.privateNetworkApproved) {
      entry.approvedPrivateOrigins.add(normalizeOrigin(requestedUrl));
    }
    claimAgentControl(entry, control);
    control.signal?.throwIfAborted();
    control.onSubmitted?.();
    entry.visitSource = control.onSubmitted ? "agent" : "other";
    await entry.view.webContents.loadURL(requestedUrl);
    assertCurrent();
    return navigationState(entry, "updated");
  } finally {
    if (pendingNavigations.get(key) === request) pendingNavigations.delete(key);
  }
}

function setBounds(payload = {}) {
  const { id, x, y, width, height } = payload;
  const owner = conversationIdFrom(payload);
  requireWindow(payload.window_instance_id);
  // Layout messages can already be queued when the native Close command retires a page.
  if (!views.has(viewKey(payload.window_instance_id, assertViewId(id)))) return false;
  const entry = selectEntry(id, owner, payload.window_instance_id);
  const mainWindow = entry.window;
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const content = mainWindow.getContentBounds();
  // getBoundingClientRect() is expressed in renderer CSS pixels. Electron View
  // bounds use unzoomed content coordinates, so compensate for Ctrl +/- zoom
  // or the guest view drifts left and overlaps the chat/composer.
  const zoomFactor = Number(mainWindow.webContents.getZoomFactor?.()) || 1;
  const bounds = normalizeViewBounds({ x, y, width, height }, content, zoomFactor);
  entry.view.setBounds(bounds);
  entry.layoutBound = bounds.width > 0 && bounds.height > 0;
  if ("viewport" in payload) {
    const viewport = payload.viewport;
    if (viewport === null) {
      if (entry.deviceViewport) entry.view.webContents.disableDeviceEmulation();
      entry.deviceViewport = null;
    } else {
      if (!Number.isInteger(viewport?.width) || !Number.isInteger(viewport?.height)
        || viewport.width < 240 || viewport.width > 4096 || viewport.height < 160 || viewport.height > 4096) {
        throw new Error("预览视口宽度必须在 240–4096 px、高度在 160–4096 px 之间。");
      }
      if (viewport.scale !== undefined && (!Number.isFinite(viewport.scale) || viewport.scale < 0 || viewport.scale > 1)) {
        throw new Error("设备视口显示比例必须在 0–1 之间。");
      }
      // The renderer supplies the centred visual frame, not the whole stage.
      // Host zoom scales that frame in DIP; it must scale emulation as well.
      // Keep the logical viewport current while its pixels yield to an HTML
      // menu/resize capture. A collapsed stage has no scale to present yet.
      const scale = viewport.scale === undefined
        ? Math.min(bounds.width / viewport.width, bounds.height / viewport.height)
        : viewport.scale * zoomFactor;
      const key = JSON.stringify([viewport.width, viewport.height, viewport.mobile, scale]);
      if (scale > 0 && entry.deviceViewport !== key) {
        entry.view.webContents.enableDeviceEmulation({
          screenPosition: viewport.mobile ? "mobile" : "desktop",
          screenSize: { width: viewport.width, height: viewport.height },
          viewPosition: { x: 0, y: 0 }, viewSize: { width: viewport.width, height: viewport.height },
          deviceScaleFactor: 1, scale,
        });
        entry.deviceViewport = key;
      }
    }
  }
  syncViewPresentation(entry);
  return true;
}

const navigate = create;

async function clearSiteData(payload = {}) {
  const entry = selectEntry(payload.id, conversationIdFrom(payload), payload.window_instance_id);
  const origin = normalizeOrigin(entry.view.webContents.getURL() || entry.url);
  if (!origin) return false;
  await entry.view.webContents.session.clearStorageData({
    origin,
    storages: ["cookies", "filesystem", "indexdb", "localstorage", "serviceworkers", "cachestorage"],
  });
  removeSitePermissions({ origin });
  if (entry.approvedPrivateOrigins instanceof Set) entry.approvedPrivateOrigins.delete(origin);
  return true;
}

async function clearBrowsingData(dataTypes = ["cookies", "siteData", "cache", "downloads", "history"]) {
  if (!Array.isArray(dataTypes) || dataTypes.some(type => !["cookies", "siteData", "cache", "downloads", "history"].includes(type))) {
    throw new Error("Unsupported browser data type.");
  }
  const browserSession = session.fromPartition(DEFAULT_PARTITION);
  const storages = [];
  if (dataTypes.includes("cookies")) storages.push("cookies");
  if (dataTypes.includes("siteData")) storages.push("filesystem", "indexdb", "localstorage", "serviceworkers", "cachestorage");
  if (storages.length) await browserSession.clearStorageData({ storages });
  if (dataTypes.includes("cache")) await browserSession.clearCache();
  if (dataTypes.includes("history")) browserHistory.clear();
  if (dataTypes.includes("downloads")) { listDownloads(); downloadRecords = downloadRecords.filter(record => downloadItems.has(record.id)); saveDownloads(); publishDownloads(); }
  return true;
}

async function capture(payload) {
  const entry = selectEntry(payload.id, conversationIdFrom(payload), payload.window_instance_id);
  const contents = entry.view.webContents;
  if (contents.getURL() !== payload.expectedUrl) throw new Error("浏览器页面已经变化，请重新选择批注位置。");
  let image = await capturePresentedPage(entry, {});
  if (contents.getURL() !== payload.expectedUrl) throw new Error("浏览器页面已经变化，请重新选择批注位置。");
  if (payload.rect) {
    const size = image.getSize();
    const viewport = payload.viewport;
    const x = Math.max(0, Math.floor(payload.rect.x * size.width / viewport.width));
    const y = Math.max(0, Math.floor(payload.rect.y * size.height / viewport.height));
    const right = Math.min(size.width, Math.ceil((payload.rect.x + payload.rect.width) * size.width / viewport.width));
    const bottom = Math.min(size.height, Math.ceil((payload.rect.y + payload.rect.height) * size.height / viewport.height));
    image = image.crop({ x, y, width: right - x, height: bottom - y });
  }
  const { width, height } = image.getSize();
  return { data: image.toPNG().toString("base64"), mimeType: "image/png", width, height, url: contents.getURL() };
}

async function executeControlCommand(payload = {}, control = {}) {
  loadBrowserSettings();
  if (!browserSettings.browserEnabled) throw new Error("Browser control is disabled in Browser settings.");
  control.signal?.throwIfAborted();
  const submit = () => { control.signal?.throwIfAborted(); control.onSubmitted?.(); };
  const conversationId = conversationIdFrom(payload);
  const action = String(payload.action || "").trim().toLowerCase();
  const windowInstanceId = payload.window_instance_id;
  requireWindow(windowInstanceId);
  if (action === "discover" || action === "list_targets") {
    return { ok: true, action, browser: "MiniCode Embedded Browser", targets: listTargets(conversationId, windowInstanceId) };
  }
  if (action === "list_downloads") {
    return { ok: true, action, value: listDownloads().filter(record => record.conversationId === conversationId
      && record.windowInstanceId === windowInstanceId && record.isUserInitiated === false) };
  }

  if (action === "navigate") {
    const url = typeof payload.url === "string" ? payload.url.trim() : "";
    const requestedTargetId = typeof payload.target_id === "string" ? payload.target_id.trim() : "";
    let entry;
    let alreadyNavigated = false;
    try {
      entry = selectEntry(requestedTargetId, conversationId, windowInstanceId);
    } catch (error) {
      if (requestedTargetId) throw error;
      const agentTabId = `agent_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
      await navigate({ id: agentTabId, url, conversation_id: conversationId, window_instance_id: windowInstanceId }, control);
      entry = selectEntry(agentTabId, conversationId, windowInstanceId);
      alreadyNavigated = true;
    }
    if (!alreadyNavigated) await navigate({ id: entry.id, url, conversation_id: conversationId, window_instance_id: windowInstanceId }, control);
    if (control.requestPresentation) emit(entry, "presentation-requested");
    const waitMs = Math.max(0, Math.min(Number(payload.wait_ms) || 0, 5000));
    if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    return { ok: true, action, target: targetState(entry) };
  }
  const entry = selectEntry(payload.target_id, conversationId, windowInstanceId);
  claimAgentControl(entry, control);
  const webContents = entry.view.webContents;
  if (action === "allow_download") {
    if (!control.agentControl) throw new Error("Download authorization requires the original authenticated agent control.");
    const url = payload.url;
    const parsed = typeof url === "string" ? URL.parse(url) : null;
    if (!parsed || !["http:", "https:", "blob:"].includes(parsed.protocol)) throw new Error("Download authorization requires an exact HTTP, HTTPS or blob URL.");
    const frame = webContents.mainFrame;
    const navigationVersion = entry.navigationVersion;
    if (parsed.protocol === "blob:") {
      if (parsed.origin !== normalizeOrigin(frame.url)) throw new Error("Blob download authorization must belong to the original page origin.");
    } else await confirmNavigationUrl(url, conversationId, entry, control);
    control.signal?.throwIfAborted();
    if (views.get(entry.key) !== entry || entry.navigationVersion !== navigationVersion || webContents.mainFrame !== frame) {
      throw new Error("The original download page changed before authorization completed.");
    }
    submit();
    entry.blobDownload = null;
    entry.allowedDownload = { url, grantedAtMs: Date.now(), control: control.agentControl };
    return { ok: true, action, target: targetState(entry), value: url };
  }
  if (action === "set_blob_download_origin") {
    if (!control.agentControl) throw new Error("Blob download authorization requires the original authenticated agent control.");
    const origin = payload.origin;
    if (origin == null) {
      submit();
      if (entry.blobDownload?.control.id === control.agentControl.id) entry.blobDownload = null;
      return { ok: true, action, target: targetState(entry), value: null };
    }
    const url = payload.url ?? null;
    const parsed = url === null ? null : URL.parse(url);
    if (normalizeOrigin(origin) !== origin || url !== null && (parsed?.protocol !== "blob:" || parsed.origin !== origin)) {
      throw new Error("Blob download origin and optional exact URL must describe the original HTTP or HTTPS page.");
    }
    const frame = webContents.mainFrame;
    const navigationVersion = entry.navigationVersion;
    const debuggerApi = downloadDebugger(entry);
    await debuggerApi.sendCommand("Page.enable", {});
    const { frameTree } = await debuggerApi.sendCommand("Page.getFrameTree", {});
    control.signal?.throwIfAborted();
    if (views.get(entry.key) !== entry || entry.navigationVersion !== navigationVersion || webContents.mainFrame !== frame
      || typeof frameTree?.frame?.id !== "string" || !frameTree.frame.id
      || normalizeOrigin(frameTree.frame.url) !== origin || normalizeOrigin(frame.url) !== origin) {
      throw new Error("The original browser main frame or blob origin changed before authorization completed.");
    }
    submit();
    entry.allowedDownload = null;
    entry.blobDownload = { origin, url, frameId: frameTree.frame.id, control: control.agentControl };
    return { ok: true, action, target: targetState(entry), value: origin };
  }
  if (action === "screenshot") {
    submit();
    if (control.requestPresentation) emit(entry, "presentation-requested");
    const image = await capturePresentedPage(entry, control);
    const size = image.getSize();
    const png = image.toPNG();
    const base64Size = 4 * Math.ceil(png.length / 3);
    if (base64Size > API_IMAGE_MAX_BASE64_SIZE) {
      throw new Error("Embedded browser screenshot exceeds the 5 MiB API image limit.");
    }
    return { ok: true, action, target: targetState(entry), mimeType: "image/png", data: png.toString("base64"), width: size.width, height: size.height };
  }
  if (action === "get_url") return { ok: true, action, target: targetState(entry) };
  if (action === "get_text") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, pageStringExpression("document.body ? document.body.innerText : ''", payload.max_chars), control) };
  if (action === "get_html") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, pageStringExpression("document.documentElement ? document.documentElement.outerHTML : ''", payload.max_chars), control) };
  if (action === "get_dom") {
    const value = await evaluateInEntry(entry, `(() => Array.from(document.querySelectorAll('body *')).slice(0, 800).map((el) => ({
      tag: el.tagName.toLowerCase(), id: el.id || undefined, role: el.getAttribute('role') || undefined,
      ariaLabel: el.getAttribute('aria-label') || undefined,
      text: (el.childElementCount === 0 ? el.textContent : '').trim().slice(0, 240) || undefined
    })))()`, control);
    return { ok: true, action, target: targetState(entry), value };
  }
  if (action === "wait_for_element") {
    const selector = String(payload.selector || "");
    const found = await waitForSelector(entry, selector, payload.timeout_ms, control);
    return { ok: found, action, target: targetState(entry), value: found, error: found ? undefined : `Timed out waiting for element: ${selector}` };
  }
  if (action === "get_console_logs") return { ok: true, action, target: targetState(entry), value: recentLogsWithin(entry.consoleLogs, payload.max_chars) };
  if (action === "get_network_logs") return { ok: true, action, target: targetState(entry), value: recentLogsWithin(entry.networkLogs, payload.max_chars) };
  if (action === "pick_element" || action === "pick_region") {
    webContents.focus();
    const value = await evaluateInEntry(entry, action === "pick_element" ? ELEMENT_PICKER_SCRIPT : REGION_PICKER_SCRIPT, control);
    return { ok: true, action, target: targetState(entry), value };
  }
  if (action === "click") {
    const selector = String(payload.selector || "").trim();
    if (selector) {
      const result = await evaluateInEntry(entry, `(() => { const el = document.querySelector(${scriptValue(selector)}); if (!el) return { ok: false }; el.scrollIntoView({ block: 'center', inline: 'center' }); el.click(); return { ok: true }; })()`, control);
      if (!result?.ok) throw new Error(`Element not found: ${selector}`);
    } else {
      submit();
      webContents.sendInputEvent({ type: "mouseDown", x: Number(payload.x), y: Number(payload.y), button: "left", clickCount: 1 });
      webContents.sendInputEvent({ type: "mouseUp", x: Number(payload.x), y: Number(payload.y), button: "left", clickCount: 1 });
    }
    return { ok: true, action, target: targetState(entry), value: selector || `${payload.x},${payload.y}` };
  }
  if (action === "type") {
    const selector = String(payload.selector || "").trim();
    if (selector) {
      const result = await evaluateInEntry(entry, `(() => { const el = document.querySelector(${scriptValue(selector)}); if (!el) return { ok: false }; el.focus(); if (${Boolean(payload.clear)}) { if ('value' in el) el.value = ''; else if (el.isContentEditable) el.textContent = ''; el.dispatchEvent(new Event('input', { bubbles: true })); } return { ok: true }; })()`, control);
      if (!result?.ok) throw new Error(`Element not found: ${selector}`);
    }
    submit();
    await webContents.insertText(String(payload.text || ""));
    return { ok: true, action, target: targetState(entry), value: String(payload.text || "").length };
  }
  if (action === "press_key") {
    submit();
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
      : `(() => { window.scrollBy(${deltaX}, ${deltaY}); return { ok: true, x: window.scrollX, y: window.scrollY }; })()`, control);
    if (!value?.ok) throw new Error(`Element not found: ${selector}`);
    return { ok: true, action, target: targetState(entry), value };
  }
  if (action === "evaluate") return { ok: true, action, target: targetState(entry), value: await evaluateInEntry(entry, evaluatedValueExpression(String(payload.expression || ""), payload.max_chars), control) };
  throw new Error(`Unsupported embedded browser action: ${action}`);
}

function runNavigationAction(payload = {}) {
  const { id, action } = payload;
  const entry = selectEntry(id, conversationIdFrom(payload), payload.window_instance_id);
  const webContents = entry.view.webContents;
  const history = webContents.navigationHistory;
  entry.visitSource = "other";
  if (action === "back" && history?.canGoBack?.()) history.goBack();
  else if (action === "forward" && history?.canGoForward?.()) history.goForward();
  else if (action === "reload") webContents.reload();
  else if (action === "stop") webContents.stop();
  else if (action === "focus") webContents.focus();
  else return false;
  return true;
}

function menuTarget(window, focusedContents, activePage = false) {
  const focused = focusedContents && entriesByWebContentsId.get(focusedContents.id);
  if (focused?.window === window && focused.presented) return focused;
  return activePage ? Array.from(views.values()).find(entry => entry.window === window && entry.presented) : undefined;
}

function canReloadVisiblePage(window, focusedContents) {
  return Boolean(menuTarget(window, focusedContents, true));
}

function runMenuCommand(window, focusedContents, action) {
  const entry = menuTarget(window, focusedContents, action === "reload" || action === "hard-reload");
  if (!entry) return false;
  const contents = entry.view.webContents;
  if (action === "reload" || action === "hard-reload") {
    entry.visitSource = "other";
    if (action === "hard-reload") contents.reloadIgnoringCache(); else contents.reload();
  } else if (action === "close") {
    closeEntry(entry);
  } else if (action === "undo" || action === "redo") {
    contents[action]();
  } else if (action === "open-find") {
    emit(entry, "find-request");
  } else if (action === "find-next" || action === "find-previous") {
    emit(entry, "find-request", { findDirection: action === "find-next" ? "next" : "previous" });
  } else if (action === "zoom-in" || action === "zoom-out" || action === "reset-zoom") {
    // Installed Source Gr/Za steps through these percentages, including
    // non-standard current values, rather than adding a fixed delta.
    const levels = [25, 33, 50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500];
    const current = Math.round(contents.getZoomFactor() * 100);
    const percent = action === "reset-zoom" ? 100
      : action === "zoom-in" ? levels.find(value => value > current) ?? 500
        : levels.findLast(value => value < current) ?? 25;
    contents.setZoomFactor(percent / 100);
    emit(entry, "updated", { zoomFactor: percent / 100 });
  } else if (action === "back" || action === "forward") {
    const history = contents.navigationHistory;
    if (action === "back" && history.canGoBack()) history.goBack();
    if (action === "forward" && history.canGoForward()) history.goForward();
  } else {
    throw new Error(`Unsupported browser menu command: ${action}`);
  }
  return true;
}

async function runPageCommand(payload = {}) {
  const entry = selectEntry(payload.id, conversationIdFrom(payload), payload.window_instance_id);
  const contents = entry.view.webContents;
  if (payload.action === "find") {
    if (!payload.text) { contents.stopFindInPage("clearSelection"); return { matches: 0 }; }
    return { requestId: contents.findInPage(payload.text, { forward: payload.forward !== false, findNext: Boolean(payload.findNext) }) };
  }
  if (payload.action === "zoom") {
    if (payload.zoomFactor !== undefined) {
      if (!Number.isFinite(payload.zoomFactor) || payload.zoomFactor < .25 || payload.zoomFactor > 5) throw new Error("zoomFactor must be between .25 and 5");
      contents.setZoomFactor(payload.zoomFactor);
      emit(entry, "updated", { zoomFactor: payload.zoomFactor });
    }
    return { zoomFactor: contents.getZoomFactor() };
  }
  if (payload.action === "site-info") {
    const url = new URL(contents.getURL());
    const cookies = await contents.session.cookies.get({ url: url.href });
    return { origin: url.origin, hostname: url.hostname, secure: url.protocol === "https:", cookieCount: cookies.length };
  }
  if (payload.action === "print") {
    return new Promise((resolve, reject) => contents.print({}, (success, reason) => success ? resolve({ success }) : reject(new Error(reason))));
  }
  if (payload.action === "screenshot") {
    const image = await contents.capturePage();
    const result = await dialog.showSaveDialog(entry.window, { defaultPath: path.join(app.getPath("downloads"), "screenshot.png"), filters: [{ name: "PNG", extensions: ["png"] }] });
    if (!result.canceled) await fs.promises.writeFile(result.filePath, image.toPNG());
    return { saved: !result.canceled };
  }
  throw new Error(`Unsupported browser page command: ${payload.action}`);
}

function closeEntry(entry) {
  const requestedId = entry.id;
  dismissGuestPermissions(entry);
  entry.agentControls.clear();
  entry.allowedDownload = null;
  closeDownloadDebugger(entry);
  for (const complete of Array.from(entry.captureWaiters)) {
    complete(null, new Error("Browser tab closed before screenshot presentation completed."));
  }
  pendingNavigations.delete(entry.key);
  const mainWindow = entry.window;
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.contentView.removeChildView(entry.view); } catch { /* already detached */ }
  }
  views.delete(entry.key);
  entriesByWebContentsId.delete(entry.view.webContents.id);
  if (activeViewIds.get(entry.windowInstanceId) === requestedId) activeViewIds.delete(entry.windowInstanceId);
  if (!entry.view.webContents.isDestroyed()) entry.view.webContents.close();
  if (entry.presented) onMenuStateChanged(mainWindow);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("minicode:embeddedBrowser:event", {
      id: entry.id,
      conversationId: entry.conversationId,
      conversation_id: entry.conversationId,
      type: "closed",
      url: entry.url || "",
      title: entry.title || "",
      loading: false,
      canGoBack: false,
      canGoForward: false,
    });
  }
  return true;
}

function close(payload = {}) {
  const owner = conversationIdFrom(payload);
  const windowInstanceId = payload.window_instance_id;
  requireWindow(windowInstanceId);
  const id = assertViewId(payload.id);
  const key = viewKey(windowInstanceId, id);
  const pending = pendingNavigations.get(key);
  if (pending && !views.has(key)) {
    if (pending.conversationId !== owner) throw new Error("Embedded browser tab belongs to another conversation.");
    pendingNavigations.delete(key);
    return true;
  }
  const entry = selectEntry(id, owner, windowInstanceId);
  return closeEntry(entry);
}

function closeConversation(conversationId, windowInstanceId) {
  const owner = requireConversationId(conversationId);
  requireWindow(windowInstanceId);
  for (const [id, request] of pendingNavigations) {
    if (request.conversationId === owner && request.windowInstanceId === windowInstanceId) pendingNavigations.delete(id);
  }
  let closed = 0;
  for (const entry of Array.from(views.values())) {
    if (entry.conversationId !== owner || entry.windowInstanceId !== windowInstanceId) continue;
    if (closeEntry(entry)) closed += 1;
  }
  return closed;
}

function disposeAll() {
  pendingNavigations.clear();
  for (const entry of Array.from(views.values())) closeEntry(entry);
  activeViewIds.clear();
}

function releaseWindow(windowInstanceId) {
  for (const [key, request] of pendingNavigations) {
    if (request.windowInstanceId === windowInstanceId) pendingNavigations.delete(key);
  }
  for (const entry of Array.from(views.values())) {
    if (entry.windowInstanceId === windowInstanceId) closeEntry(entry);
  }
  activeViewIds.delete(windowInstanceId);
}

module.exports = {
  init,
  create,
  activate,
  setBounds,
  navigate,
  runNavigationAction,
  canReloadVisiblePage,
  runMenuCommand,
  runPageCommand,
  listDownloads,
  getActiveDownloadIds,
  runDownloadAction,
  releaseAgentControl,
  close,
  closeConversation,
  clearSiteData,
  clearBrowsingData,
  capture,
  disposeAll,
  releaseWindow,
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
  getSitePolicy,
  setSitePolicy,
  removeSiteDevice,
  handlePasswordSubmission,
  handleLinkClick,
  listSitePermissions,
  removeSitePermissions,
  makeNetworkLogEntry,
  normalizeOrigin,
  normalizeBrowserSettings,
  availableDownloadPathIn,
  requireConversationId,
};

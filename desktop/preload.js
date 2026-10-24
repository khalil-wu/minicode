const { contextBridge, ipcRenderer } = require("electron");

const DEFAULT_BACKEND_HOST = process.env.MINICODE_BACKEND_HOST || "127.0.0.1";
const DEFAULT_BACKEND_PORT = process.env.MINICODE_BACKEND_PORT || "8000";

function readRuntimeArgument(prefix) {
  const argument = process.argv.find(
    (value) => typeof value === "string" && value.startsWith(prefix),
  );
  return argument ? argument.slice(prefix.length) : "";
}

function readRuntimeConfig() {
  const value = ipcRenderer.sendSync("minicode:runtime:get");
  if (!value || typeof value !== "object") return null;
  if (typeof value.apiBaseUrl !== "string" || typeof value.wsBaseUrl !== "string") return null;
  return value;
}

const mainRuntimeConfig = readRuntimeConfig();
const remoteHostId = readRuntimeArgument("--minicode-remote-host-id=");
if (remoteHostId && mainRuntimeConfig?.remoteHost?.id !== remoteHostId) {
  throw new Error("The selected remote host's runtime binding is unavailable");
}
const apiBaseUrl =
  mainRuntimeConfig?.apiBaseUrl ||
  readRuntimeArgument("--minicode-api-base-url=") ||
  process.env.MINICODE_API_BASE_URL ||
  `http://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`;
const wsBaseUrl =
  mainRuntimeConfig?.wsBaseUrl ||
  readRuntimeArgument("--minicode-ws-base-url=") ||
  process.env.MINICODE_WS_BASE_URL ||
  `ws://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`;
const runtimeToken =
  mainRuntimeConfig?.runtimeToken ||
  process.env.MINICODE_RUNTIME_TOKEN ||
  "";
const runtimeRevision = Number(mainRuntimeConfig?.revision) || 0;
const updateActivityRendererInstanceId = globalThis.crypto?.randomUUID?.()
  || `renderer-${Date.now()}-${Math.random().toString(36).slice(2)}`;
let updateActivityRevision = 0;
let lastDeliveredDeepLinkId = null;

async function connectionCommand(action, ...args) {
  const result = await ipcRenderer.invoke(`minicode:connections:${action}`, ...args);
  if (!result.ok) throw new Error(result.error);
  return result.value;
}

const runtimeConfig = {
  apiBaseUrl,
  wsBaseUrl,
  runtimeToken,
  remoteHost: mainRuntimeConfig?.remoteHost,
  windowOwnerId: mainRuntimeConfig?.windowOwnerId || readRuntimeArgument("--minicode-window-owner-id="),
  windowInstanceId: mainRuntimeConfig?.windowInstanceId || readRuntimeArgument("--minicode-window-instance-id="),
  windowBindingToken: mainRuntimeConfig?.windowBindingToken || "",
  initialNavigation: mainRuntimeConfig?.initialNavigation,
  desktop: {
    personalizations: {
      listHosts: () => ipcRenderer.invoke("minicode:personalizations:listHosts"),
      request: (hostId, action, payload) => ipcRenderer.invoke("minicode:personalizations:request", hostId, action, payload),
    },
    realtimeVoice: {
      claim: payload => ipcRenderer.invoke("minicode:realtimeVoice:claim", payload),
      update: payload => ipcRenderer.invoke("minicode:realtimeVoice:update", payload),
      release: payload => ipcRenderer.invoke("minicode:realtimeVoice:release", payload),
      ready: payload => ipcRenderer.invoke("minicode:realtimeVoice:ready", payload),
      reply: payload => ipcRenderer.invoke("minicode:realtimeVoice:reply", payload),
      onRequest: callback => {
        const listener = (_event, value) => callback(value);
        ipcRenderer.on("minicode:realtimeVoice:request", listener);
        return () => ipcRenderer.removeListener("minicode:realtimeVoice:request", listener);
      },
    },
    dictation: {
      get: () => ipcRenderer.invoke("minicode:dictation:get"),
      getLocalRuntime: () => ipcRenderer.invoke("minicode:dictation:getLocalRuntime"),
      openProviderSettings: () => ipcRenderer.invoke("minicode:dictation:openProviderSettings"),
      consumeProviderSettings: () => ipcRenderer.invoke("minicode:dictation:consumeProviderSettings"),
      migratePreferences: legacy => ipcRenderer.invoke("minicode:dictation:migratePreferences", legacy),
      configure: patch => ipcRenderer.invoke("minicode:dictation:configure", patch),
      suspend: value => ipcRenderer.invoke("minicode:dictation:suspend", value),
      insert: (token, text) => ipcRenderer.invoke("minicode:dictation:insert", token, text),
      complete: token => ipcRenderer.invoke("minicode:dictation:complete", token),
      onEvent: callback => {
        const listener = (_event, value) => callback(value);
        ipcRenderer.on("minicode:dictation:event", listener);
        return () => ipcRenderer.removeListener("minicode:dictation:event", listener);
      },
      onState: callback => {
        const listener = (_event, value) => callback(value);
        ipcRenderer.on("minicode:dictation:state", listener);
        return () => ipcRenderer.removeListener("minicode:dictation:state", listener);
      },
    },
    mcpAppSandbox: {
      prepare: frameUrl => ipcRenderer.invoke("minicode:mcpApp:prepare", frameUrl),
      release: id => ipcRenderer.invoke("minicode:mcpApp:release", id),
      clearData: () => ipcRenderer.invoke("minicode:mcpApp:clearData"),
    },
    browserPersonalData: {
      listPasswords: () => ipcRenderer.invoke("minicode:browserPersonalData:listPasswords"),
      getPassword: request => ipcRenderer.invoke("minicode:browserPersonalData:getPassword", request),
      savePassword: request => ipcRenderer.invoke("minicode:browserPersonalData:savePassword", request),
      removePassword: request => ipcRenderer.invoke("minicode:browserPersonalData:removePassword", request),
      listContacts: () => ipcRenderer.invoke("minicode:browserPersonalData:listContacts"),
      saveContact: request => ipcRenderer.invoke("minicode:browserPersonalData:saveContact", request),
      removeContact: request => ipcRenderer.invoke("minicode:browserPersonalData:removeContact", request),
      setAutofillSettings: patch => ipcRenderer.invoke("minicode:browserPersonalData:setAutofillSettings", patch),
    },
    connections: {
      list: () => connectionCommand("list"),
      discover: (kind) => connectionCommand("discover", kind),
      save: (connection) => connectionCommand("save", connection),
      saveMany: (connections) => connectionCommand("saveMany", connections),
      remove: (id) => connectionCommand("remove", id),
      connect: (id) => connectionCommand("connect", id),
      disconnect: (id) => connectionCommand("disconnect", id),
      open: (id) => connectionCommand("open", id),
      onChanged: (callback) => {
        const handler = (_event, connections) => callback(connections);
        ipcRenderer.on("minicode:connections:changed", handler);
        return () => ipcRenderer.removeListener("minicode:connections:changed", handler);
      },
    },
    preferences: {
      getStartupStatus: () => ipcRenderer.invoke("minicode:preferences:getStartupStatus"),
      openStartupSettings: () => ipcRenderer.invoke("minicode:preferences:openStartupSettings"),
      getOpenSourceLicenses: () => ipcRenderer.invoke("minicode:preferences:getOpenSourceLicenses"),
      pickNotificationSound: () => ipcRenderer.invoke("minicode:preferences:pickNotificationSound"),
      importNotificationSound: payload => ipcRenderer.invoke("minicode:preferences:importNotificationSound", payload),
      previewNotificationSound: sound => ipcRenderer.invoke("minicode:preferences:previewNotificationSound", sound),
      chooseTaskDirectory: () => ipcRenderer.invoke("minicode:preferences:chooseTaskDirectory"),
      get: () => ipcRenderer.invoke("minicode:preferences:get"),
      set: (patch) => ipcRenderer.invoke("minicode:preferences:set", patch),
      onChanged: (callback) => {
        const listener = (_event, value) => callback(value);
        ipcRenderer.on("minicode:preferences:changed", listener);
        return () => ipcRenderer.removeListener("minicode:preferences:changed", listener);
      },
    },
    openPreferredPath: (target, line, options) => ipcRenderer.invoke("minicode:openPreferredPath", target, line, options),
    saveFile: (filename, bytes) => ipcRenderer.invoke("minicode:saveFile", filename, bytes),
    windowControls: {
      listFontFamilies: () => ipcRenderer.invoke("minicode:window:fontFamilies"),
      listFonts: () => ipcRenderer.invoke("minicode:window:fonts"),
      consumeInitialNavigation: () => ipcRenderer.sendSync("minicode:window:initialNavigation:consume"),
      openConversation: (conversationId) => ipcRenderer.invoke("minicode:window:openConversation", conversationId),
      getState: () => ipcRenderer.invoke("minicode:window:state"),
      getAppearance: () => ipcRenderer.invoke("minicode:window:appearance:get"),
      setAppearance: (options) => ipcRenderer.invoke("minicode:window:appearance:set", options),
      onAppearanceChanged: (callback) => {
        const listener = (_event, state) => callback(state);
        ipcRenderer.on("minicode:window:appearance", listener);
        return () => ipcRenderer.removeListener("minicode:window:appearance", listener);
      },
      onStateChanged: (callback) => {
        const listener = (_event, state) => callback(state);
        ipcRenderer.on("minicode:window:state", listener);
        return () => ipcRenderer.removeListener("minicode:window:state", listener);
      },
      minimize: () => ipcRenderer.invoke("minicode:window:minimize"),
      maximize: () => ipcRenderer.invoke("minicode:window:maximize"),
      close: () => ipcRenderer.invoke("minicode:window:close"),
      captureUnloadIntent: () => ipcRenderer.sendSync("minicode:window:unload:capture"),
      resumeUnload: (id) => ipcRenderer.invoke("minicode:window:unload:resume", id),
    },
    notify: (payload) => ipcRenderer.invoke("minicode:notify", payload),
    hideNotifications: (selector) => ipcRenderer.invoke("minicode:notifications:hide", selector),
    onDeepLink: (callback) => {
      let subscribed = true;
      const deliver = (payload) => {
        if (!subscribed || !payload || payload.id === lastDeliveredDeepLinkId) return;
        lastDeliveredDeepLinkId = payload.id;
        callback(payload);
      };
      const handler = (_event, payload) => deliver(payload);
      ipcRenderer.on("minicode:deep-link", handler);
      void ipcRenderer.invoke("minicode:deepLink:pending").then(deliver);
      return () => {
        subscribed = false;
        ipcRenderer.removeListener("minicode:deep-link", handler);
      };
    },
    ackDeepLink: (id) => ipcRenderer.invoke("minicode:deepLink:ack", id),
    updates: {
      check: () => ipcRenderer.invoke("minicode:update:check"),
      download: () => ipcRenderer.invoke("minicode:update:download"),
      reportActivity: (payload, requestIds = []) => ipcRenderer.invoke("minicode:update:activity", {
        ...payload,
        revision: ++updateActivityRevision,
        rendererInstanceId: updateActivityRendererInstanceId,
        requestIds,
      }),
      onActivityRequest: (callback) => {
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on("minicode:update:activity:request", handler);
        return () => ipcRenderer.removeListener("minicode:update:activity:request", handler);
      },
      preflight: () => ipcRenderer.invoke("minicode:update:preflight"),
      install: (payload) => ipcRenderer.invoke("minicode:update:install", payload),
      getStatus: () => ipcRenderer.invoke("minicode:update:status:get"),
      onStatus: (callback) => {
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on("minicode:update:status", handler);
        return () => ipcRenderer.removeListener("minicode:update:status", handler);
      },
    },
    pickDirectory: () => ipcRenderer.invoke("minicode:pickDirectory"),
    pickWorkspaceDirectory: () => ipcRenderer.invoke("minicode:workspace:pickDirectory"),
    workspaceDirectoryEntries: (directoryPath, directoriesOnly) => ipcRenderer.invoke("minicode:workspace:directoryEntries", directoryPath, directoriesOnly),
    attachments: {
      importRemoteFile: request => ipcRenderer.invoke("minicode:attachments:importRemoteFile", request),
    },
    trustWorkspace: (path) => ipcRenderer.invoke("minicode:workspace:trust", path),
    openExternal: (target) => ipcRenderer.invoke("minicode:openExternal", target),
    openPath: (target) => ipcRenderer.invoke("minicode:openPath", target),
    revealPath: (target) => ipcRenderer.invoke("minicode:revealPath", target),
    settings: {
      openTarget: request => ipcRenderer.invoke("minicode:settings:openTarget", request),
    },
    skills: {
      reveal: request => ipcRenderer.invoke("minicode:skills:reveal", request),
    },
    openDeepLink: (target) => ipcRenderer.invoke("minicode:deepLink:open", target),
    diagnostics: {
      export: () => ipcRenderer.invoke("minicode:diagnostics:export"),
    },
    platformInfo: {
      isDesktop: true,
      platform: process.platform,
      arch: process.arch,
    },
    fs: {
      listTree: (path) => ipcRenderer.invoke("minicode:fs:listTree", path),
      searchFiles: (rootPath, query, limit) => ipcRenderer.invoke("minicode:fs:searchFiles", rootPath, query, limit),
      searchFilesByKind: (rootPath, query, limit, kind) => ipcRenderer.invoke("minicode:fs:searchFiles", rootPath, query, limit, kind),
      readFile: (path) => ipcRenderer.invoke("minicode:fs:readFile", path),
      deletePath: (path, recursive, confirm) => ipcRenderer.invoke("minicode:fs:deletePath", path, recursive, confirm),
    },
    pty: {
      spawn: (cwd, conversationId) => ipcRenderer.invoke("minicode:pty:spawn", cwd, conversationId),
      write: (sessionId, data, conversationId) => ipcRenderer.invoke("minicode:pty:write", sessionId, data, conversationId),
      resize: (sessionId, cols, rows, conversationId) => ipcRenderer.invoke("minicode:pty:resize", sessionId, cols, rows, conversationId),
      kill: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:kill", sessionId, conversationId),
      restart: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:restart", sessionId, conversationId),
      killConversation: (conversationId, preserveHistory = false) => ipcRenderer.invoke("minicode:pty:killConversation", conversationId, preserveHistory),
      list: (conversationId) => ipcRenderer.invoke("minicode:pty:list", conversationId),
      snapshot: (sessionId, maxChars, conversationId) => ipcRenderer.invoke("minicode:pty:snapshot", sessionId, maxChars, conversationId),
      clear: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:clear", sessionId, conversationId),
      ackExit: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:ackExit", sessionId, conversationId),
      onData: (callback) => {
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on("minicode:pty:data", handler);
        return () => ipcRenderer.removeListener("minicode:pty:data", handler);
      },
      onExit: (callback) => {
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on("minicode:pty:exit", handler);
        return () => ipcRenderer.removeListener("minicode:pty:exit", handler);
      }
    },
    pets: {
      getState: () => ipcRenderer.invoke("minicode:pets:getState"),
      refresh: () => ipcRenderer.invoke("minicode:pets:refresh"),
      update: (patch) => ipcRenderer.invoke("minicode:pets:update", patch),
      toggleMini: () => ipcRenderer.invoke("minicode:pets:toggleMini"),
      setNotificationMuted: (request) => ipcRenderer.invoke("minicode:pets:setNotificationMuted", request),
      openFolder: () => ipcRenderer.invoke("minicode:pets:openFolder"),
      getSprite: (id) => ipcRenderer.invoke("minicode:pets:getSprite", id),
      publishActivity: (activity) => ipcRenderer.invoke("minicode:pets:publishActivity", activity),
      onActionRequested: (callback) => {
        const listener = (_event, request) => callback(request);
        ipcRenderer.on("minicode:pets:actionRequested", listener);
        return () => ipcRenderer.removeListener("minicode:pets:actionRequested", listener);
      },
      completeAction: (result) => ipcRenderer.invoke("minicode:pets:completeAction", result),
      onStateChanged: (callback) => {
        const listener = (_event, state) => callback(state);
        ipcRenderer.on("minicode:pets:stateChanged", listener);
        return () => ipcRenderer.removeListener("minicode:pets:stateChanged", listener);
      },
    },
    env: {
      detect: () => ipcRenderer.invoke("minicode:env:detect"),
    },
    sandbox: {
      setup: () => ipcRenderer.invoke("minicode:sandbox:setup"),
    },
    menu: {
      popup: (key) => ipcRenderer.invoke("minicode:menu:popup", key),
      setState: (state) => ipcRenderer.invoke("minicode:menu:setState", state),
      execute: (action) => ipcRenderer.invoke("minicode:menu:execute", action),
    },
    browserProfileImport: {
      listImportableBrowserProfiles: () => ipcRenderer.invoke("minicode:browserProfileImport:list"),
      importBrowserProfile: (request) => ipcRenderer.invoke("minicode:browserProfileImport:import", request),
    },
    browser: {
      history: (query) => ipcRenderer.invoke("minicode:browser:history", query),
      removeHistory: (ids) => ipcRenderer.invoke("minicode:browser:removeHistory", ids),
      discover: (endpoint) => ipcRenderer.invoke("minicode:browser:discover", endpoint),
      captureScreenshot: (endpoint, targetId) => ipcRenderer.invoke("minicode:browser:captureScreenshot", endpoint, targetId),
      navigate: (endpoint, targetId, url) => ipcRenderer.invoke("minicode:browser:navigate", endpoint, targetId, url),
      click: (endpoint, targetId, selector) => ipcRenderer.invoke("minicode:browser:click", endpoint, targetId, selector),
      type: (endpoint, targetId, selector, text) => ipcRenderer.invoke("minicode:browser:type", endpoint, targetId, selector, text),
    },
    embeddedBrowser: {
      getSitePolicy: () => ipcRenderer.invoke("minicode:embeddedBrowser:getSitePolicy"),
      setSitePolicy: request => ipcRenderer.invoke("minicode:embeddedBrowser:setSitePolicy", request),
      removeSiteDevice: request => ipcRenderer.invoke("minicode:embeddedBrowser:removeSiteDevice", request),
      listSitePermissions: () => ipcRenderer.invoke("minicode:embeddedBrowser:listSitePermissions"),
      removeSitePermissions: request => ipcRenderer.invoke("minicode:embeddedBrowser:removeSitePermissions", request),
      pageCommand: payload => ipcRenderer.invoke("minicode:embeddedBrowser:pageCommand", payload),
      downloads: () => ipcRenderer.invoke("minicode:embeddedBrowser:downloads"),
      downloadAction: payload => ipcRenderer.invoke("minicode:embeddedBrowser:downloadAction", payload),
      onDownloadsChanged: callback => {
        const listener = (_event, records) => callback(records);
        ipcRenderer.on("minicode:embeddedBrowser:downloadsChanged", listener);
        return () => ipcRenderer.removeListener("minicode:embeddedBrowser:downloadsChanged", listener);
      },
      capture: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:capture", payload),
      clearBrowsingData: dataTypes => ipcRenderer.invoke("minicode:embeddedBrowser:clearBrowsingData", dataTypes),
      create: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:create", payload),
      list: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:list", payload),
      activate: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:activate", payload),
      setBounds: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:setBounds", payload),
      navigate: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:navigate", payload),
      runAction: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:runAction", payload),
      inspect: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:inspect", payload),
      getSettings: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:getSettings", payload),
      setSettings: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:setSettings", payload),
      onSettingsChanged: (callback) => {
        const listener = (_event, settings) => callback(settings);
        ipcRenderer.on("minicode:embeddedBrowser:settingsChanged", listener);
        return () => ipcRenderer.removeListener("minicode:embeddedBrowser:settingsChanged", listener);
      },
      clearSiteData: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:clearSiteData", payload),
      close: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:close", payload),
      closeConversation: (conversationId) => ipcRenderer.invoke("minicode:embeddedBrowser:closeConversation", conversationId),
      onEvent: (callback) => {
        const handler = (_event, payload) => callback(payload);
        ipcRenderer.on("minicode:embeddedBrowser:event", handler);
        return () => ipcRenderer.removeListener("minicode:embeddedBrowser:event", handler);
      },
    }
  },
};

ipcRenderer.on("minicode:runtime:changed", (_event, payload) => {
  const nextRevision = Number(payload?.revision) || 0;
  if (nextRevision <= runtimeRevision) return;
  window.location.reload();
});

if (process.contextIsolated) {
  ipcRenderer.on("minicode:menu:new-chat", () => window.dispatchEvent(new Event("new-conversation")));
  ipcRenderer.on("minicode:menu:quick-chat", () => window.dispatchEvent(new Event("quick-chat")));
  ipcRenderer.on("minicode:menu:open-folder", () => window.dispatchEvent(new Event("open-import-modal")));
  ipcRenderer.on("minicode:menu:extensions-marketplace", () => {
    window.dispatchEvent(new CustomEvent("open-extensions-marketplace", { detail: { tab: "all" } }));
  });
  ipcRenderer.on("minicode:menu:settings", (_event, detail) => window.dispatchEvent(new CustomEvent("open-settings", { detail })));
  ipcRenderer.on("minicode:menu:open-pets-settings", () => window.dispatchEvent(new Event("open-pets-settings")));
  ipcRenderer.on("minicode:menu:focus-composer", () => window.dispatchEvent(new Event("composer:focus")));
  ipcRenderer.on("minicode:menu:undo-app-action", () => window.dispatchEvent(new Event("undo-app-action")));
  ipcRenderer.on("minicode:menu:redo-app-action", () => window.dispatchEvent(new Event("redo-app-action")));
  ipcRenderer.on("minicode:pets:refreshed", () => window.dispatchEvent(new Event("pets:refreshed")));
  for (const event of ["new-project", "open-browser-tab", "focus-browser-address-bar", "toggle-file-tree", "toggle-bottom-panel", "toggle-pinned-summary", "toggle-side-panel", "find-in-thread", "find-next-in-thread", "find-previous-in-thread", "navigate-back", "navigate-forward", "previous-thread", "next-thread", "previous-tab", "next-tab", "close-tab", "open-shortcuts", "open-command-menu", "search-files", "search-chats", "rename-thread", "toggle-thread-pin", "archive-thread", "copy-deeplink", "copy-working-directory", "open-side-chat"]) {
    ipcRenderer.on(`minicode:menu:${event}`, () => window.dispatchEvent(new Event(event)));
  }
  ipcRenderer.on("minicode:menu:toggle-sidebar", () => window.dispatchEvent(new Event("toggle-sidebar")));
  ipcRenderer.on("minicode:menu:toggle-context", () => window.dispatchEvent(new Event("toggle-context")));
  ipcRenderer.on("minicode:shortcut:terminal", () => window.dispatchEvent(new Event("toggle-terminal")));
  ipcRenderer.on("minicode:shortcut:import", () => window.dispatchEvent(new Event("open-import-modal")));
  contextBridge.exposeInMainWorld("__MINICODE_RUNTIME__", runtimeConfig);
} else {
  window.__MINICODE_RUNTIME__ = runtimeConfig;
}

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

const runtimeConfig = {
  apiBaseUrl,
  wsBaseUrl,
  runtimeToken,
  desktop: {
    windowControls: {
      minimize: () => ipcRenderer.invoke("minicode:window:minimize"),
      maximize: () => ipcRenderer.invoke("minicode:window:maximize"),
      close: () => ipcRenderer.invoke("minicode:window:close"),
    },
    notify: (payload) => ipcRenderer.invoke("minicode:notify", payload),
    onDeepLink: (callback) => {
      const handler = (_event, payload) => callback(payload);
      ipcRenderer.on("minicode:deep-link", handler);
      return () => ipcRenderer.removeListener("minicode:deep-link", handler);
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
    trustWorkspace: (path) => ipcRenderer.invoke("minicode:workspace:trust", path),
    openExternal: (target) => ipcRenderer.invoke("minicode:openExternal", target),
    openPath: (target) => ipcRenderer.invoke("minicode:openPath", target),
    revealPath: (target) => ipcRenderer.invoke("minicode:revealPath", target),
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
      writeFile: (path, content) => ipcRenderer.invoke("minicode:fs:writeFile", path, content),
      compareWriteFile: (path, expectedHash, content) => ipcRenderer.invoke("minicode:fs:compareWriteFile", path, expectedHash, content),
      createDirectory: (path) => ipcRenderer.invoke("minicode:fs:createDirectory", path),
      renamePath: (oldPath, newPath) => ipcRenderer.invoke("minicode:fs:renamePath", oldPath, newPath),
      deletePath: (path, recursive, confirm) => ipcRenderer.invoke("minicode:fs:deletePath", path, recursive, confirm),
    },
    pty: {
      spawn: (cwd, conversationId) => ipcRenderer.invoke("minicode:pty:spawn", cwd, conversationId),
      write: (sessionId, data, conversationId) => ipcRenderer.invoke("minicode:pty:write", sessionId, data, conversationId),
      resize: (sessionId, cols, rows, conversationId) => ipcRenderer.invoke("minicode:pty:resize", sessionId, cols, rows, conversationId),
      kill: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:kill", sessionId, conversationId),
      restart: (sessionId, conversationId) => ipcRenderer.invoke("minicode:pty:restart", sessionId, conversationId),
      killConversation: (conversationId) => ipcRenderer.invoke("minicode:pty:killConversation", conversationId),
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
    env: {
      detect: () => ipcRenderer.invoke("minicode:env:detect"),
    },
    browser: {
      discover: (endpoint) => ipcRenderer.invoke("minicode:browser:discover", endpoint),
      captureScreenshot: (endpoint, targetId) => ipcRenderer.invoke("minicode:browser:captureScreenshot", endpoint, targetId),
      navigate: (endpoint, targetId, url) => ipcRenderer.invoke("minicode:browser:navigate", endpoint, targetId, url),
      click: (endpoint, targetId, selector) => ipcRenderer.invoke("minicode:browser:click", endpoint, targetId, selector),
      type: (endpoint, targetId, selector, text) => ipcRenderer.invoke("minicode:browser:type", endpoint, targetId, selector, text),
    },
    embeddedBrowser: {
      create: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:create", payload),
      list: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:list", payload),
      activate: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:activate", payload),
      setBounds: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:setBounds", payload),
      navigate: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:navigate", payload),
      runAction: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:runAction", payload),
      inspect: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:inspect", payload),
      getSettings: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:getSettings", payload),
      setSettings: (payload) => ipcRenderer.invoke("minicode:embeddedBrowser:setSettings", payload),
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
  ipcRenderer.on("minicode:menu:settings", () => window.dispatchEvent(new Event("open-settings")));
  ipcRenderer.on("minicode:menu:toggle-sidebar", () => window.dispatchEvent(new Event("toggle-sidebar")));
  ipcRenderer.on("minicode:menu:toggle-context", () => window.dispatchEvent(new Event("toggle-context")));
  ipcRenderer.on("minicode:shortcut:terminal", () => window.dispatchEvent(new Event("toggle-terminal")));
  ipcRenderer.on("minicode:shortcut:import", () => window.dispatchEvent(new Event("open-import-modal")));
  contextBridge.exposeInMainWorld("__MINICODE_RUNTIME__", runtimeConfig);
} else {
  window.__MINICODE_RUNTIME__ = runtimeConfig;
}

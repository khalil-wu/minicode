const { contextBridge, ipcRenderer } = require("electron");

const DEFAULT_BACKEND_HOST = process.env.MINICODE_BACKEND_HOST || "127.0.0.1";
const DEFAULT_BACKEND_PORT = process.env.MINICODE_BACKEND_PORT || "8000";

function readRuntimeArgument(prefix) {
  const argument = process.argv.find(
    (value) => typeof value === "string" && value.startsWith(prefix),
  );
  return argument ? argument.slice(prefix.length) : "";
}

const apiBaseUrl =
  readRuntimeArgument("--minicode-api-base-url=") ||
  process.env.MINICODE_API_BASE_URL ||
  `http://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`;
const wsBaseUrl =
  readRuntimeArgument("--minicode-ws-base-url=") ||
  process.env.MINICODE_WS_BASE_URL ||
  `ws://${DEFAULT_BACKEND_HOST}:${DEFAULT_BACKEND_PORT}`;
const runtimeToken =
  readRuntimeArgument("--minicode-runtime-token=") ||
  process.env.MINICODE_RUNTIME_TOKEN ||
  "";

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
    pickDirectory: () => ipcRenderer.invoke("minicode:pickDirectory"),
    trustWorkspace: (path) => ipcRenderer.invoke("minicode:workspace:trust", path),
    openExternal: (target) => ipcRenderer.invoke("minicode:openExternal", target),
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
      spawn: (cwd) => ipcRenderer.invoke("minicode:pty:spawn", cwd),
      write: (sessionId, data) => ipcRenderer.invoke("minicode:pty:write", sessionId, data),
      resize: (sessionId, cols, rows) => ipcRenderer.invoke("minicode:pty:resize", sessionId, cols, rows),
      kill: (sessionId) => ipcRenderer.invoke("minicode:pty:kill", sessionId),
      list: () => ipcRenderer.invoke("minicode:pty:list"),
      snapshot: (sessionId, maxChars) => ipcRenderer.invoke("minicode:pty:snapshot", sessionId, maxChars),
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
      navigate: (endpoint, targetId, url, options) => ipcRenderer.invoke("minicode:browser:navigate", endpoint, targetId, url, options),
      click: (endpoint, targetId, selector) => ipcRenderer.invoke("minicode:browser:click", endpoint, targetId, selector),
      type: (endpoint, targetId, selector, text) => ipcRenderer.invoke("minicode:browser:type", endpoint, targetId, selector, text),
    }
  },
};

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

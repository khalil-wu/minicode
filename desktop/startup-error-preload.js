const { contextBridge, ipcRenderer } = require("electron");

const startupBridge = {
  getState: () => ipcRenderer.invoke("minicode:startup:getState"),
  retry: () => ipcRenderer.invoke("minicode:startup:retry"),
  quit: () => ipcRenderer.invoke("minicode:startup:quit"),
  openLogs: () => ipcRenderer.invoke("minicode:startup:openLogs"),
  onState: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("minicode:startup:state", handler);
    return () => {
      ipcRenderer.removeListener("minicode:startup:state", handler);
    };
  },
};

if (process.contextIsolated) {
  contextBridge.exposeInMainWorld("minicodeStartup", startupBridge);
} else {
  window.minicodeStartup = startupBridge;
}

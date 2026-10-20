"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("miniPet", {
  getState: () => ipcRenderer.invoke("minicode:pets:mini-get-state"),
  getSprite: id => ipcRenderer.invoke("minicode:pets:mini-get-sprite", id),
  act: (action, point) => ipcRenderer.invoke("minicode:pets:mini-action", action, point),
  onState: listener => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on("minicode:pets:mini-state", handler);
    return () => ipcRenderer.removeListener("minicode:pets:mini-state", handler);
  },
  onError: listener => {
    const handler = (_event, message) => listener(message);
    ipcRenderer.on("minicode:pets:mini-error", handler);
    return () => ipcRenderer.removeListener("minicode:pets:mini-error", handler);
  },
});

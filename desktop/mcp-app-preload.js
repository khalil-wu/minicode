"use strict";

const { ipcRenderer } = require("electron");
const MESSAGE_CHANNEL = "minicode:mcpApp:message";

// The outer document belongs to MiniCode. Its child keeps the MCP resource's
// own origin, so App code never receives Electron APIs or the host document.
window.addEventListener("message", event => {
  const frame = document.querySelector("iframe");
  if (event.source === frame?.contentWindow && event.origin === new URL(frame.src).origin) {
    ipcRenderer.sendToHost(MESSAGE_CHANNEL, event.data);
  }
});
ipcRenderer.on(MESSAGE_CHANNEL, (_event, message) => {
  const frame = document.querySelector("iframe");
  frame.contentWindow.postMessage(message, new URL(frame.src).origin);
});

"use strict";

const path = require("node:path");
const { randomUUID } = require("node:crypto");

const MCP_APP_SCHEME = "minicode-mcp-host";
const MCP_APP_PARTITION = "persist:minicode-mcp-apps";

function registerMcpAppScheme(protocol) {
  protocol.registerSchemesAsPrivileged([{ scheme: MCP_APP_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
}

function createMcpAppSandbox({ session }) {
  const sandboxSession = session.fromPartition(MCP_APP_PARTITION);
  const surfaces = new Map();
  const owners = new Set();
  const guestOwners = new Map();
  const preload = path.join(__dirname, "mcp-app-preload.js");
  sandboxSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  sandboxSession.setPermissionCheckHandler(() => false);
  sandboxSession.webRequest.onBeforeRequest((request, callback) => {
    const url = new URL(request.url);
    const supported = ["http:", "https:", "ws:", "wss:", "data:", "blob:", "about:", `${MCP_APP_SCHEME}:`].includes(url.protocol);
    const admitted = request.resourceType !== "mainFrame" || [...surfaces.values()].some(surface =>
      surface.src === request.url && surface.ownerId === guestOwners.get(request.webContentsId));
    callback({ cancel: !supported || !admitted });
  });
  sandboxSession.protocol.handle(MCP_APP_SCHEME, request => {
    const url = new URL(request.url);
    const surface = surfaces.get(url.pathname.slice(1));
    if (url.hostname !== "view" || !surface) return new Response("MCP App has closed", { status: 410 });
    const html = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}iframe{width:100%;height:100%;border:0;display:block}</style></head><body><iframe sandbox="allow-scripts allow-same-origin" referrerpolicy="no-referrer" allow="camera 'none'; microphone 'none'; geolocation 'none'" src="${surface.frameUrl}"></iframe></body></html>`;
    return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy": `default-src 'none'; style-src 'unsafe-inline'; frame-src ${surface.frameOrigin}`,
      "Cache-Control": "no-store" } });
  });

  function prepare(ownerId, frameUrl, backendUrl) {
    const url = new URL(frameUrl);
    const backend = new URL(backendUrl);
    if (url.protocol !== "http:" || !/^mcp-app-[a-f0-9]{48}\.localhost$/.test(url.hostname)
      || url.port !== backend.port || !/^\/mcp-app\/frame\/[A-Za-z0-9_-]+$/.test(url.pathname)
      || url.username || url.password || url.search || url.hash) {
      throw new TypeError("MCP App frame must belong to the connected local backend.");
    }
    const id = randomUUID();
    const src = `${MCP_APP_SCHEME}://view/${id}`;
    surfaces.set(id, { ownerId, frameUrl: url.href, frameOrigin: url.origin, src });
    return { id, src, partition: MCP_APP_PARTITION };
  }

  function release(ownerId, id) {
    if (surfaces.get(id)?.ownerId === ownerId) surfaces.delete(id);
  }

  function attachOwner(owner) {
    if (owners.has(owner.id)) return;
    owners.add(owner.id);
    owner.on("will-attach-webview", (event, preferences, parameters) => {
      const surface = [...surfaces.values()].find(entry => entry.src === parameters.src && entry.ownerId === owner.id);
      if (parameters.partition !== MCP_APP_PARTITION || !surface) { event.preventDefault(); return; }
      preferences.preload = preload;
      preferences.partition = MCP_APP_PARTITION;
      preferences.contextIsolation = true;
      preferences.nodeIntegration = false;
      preferences.nodeIntegrationInSubFrames = false;
      preferences.sandbox = true;
      preferences.webviewTag = false;
      preferences.webSecurity = true;
      preferences.allowRunningInsecureContent = false;
    });
    owner.on("did-attach-webview", (_event, guest) => {
      guestOwners.set(guest.id, owner.id);
      guest.once("destroyed", () => guestOwners.delete(guest.id));
      guest.setWindowOpenHandler(() => ({ action: "deny" }));
      guest.on("will-navigate", (event, target) => {
        if (![...surfaces.values()].some(entry => entry.ownerId === owner.id && entry.src === target)) event.preventDefault();
      });
      guest.on("will-frame-navigate", event => {
        if (event.isMainFrame && ![...surfaces.values()].some(entry => entry.ownerId === owner.id && entry.src === event.url)) event.preventDefault();
      });
    });
    owner.once("destroyed", () => {
      owners.delete(owner.id);
      for (const [id, surface] of surfaces) if (surface.ownerId === owner.id) surfaces.delete(id);
    });
  }

  return { prepare, release, attachOwner, clearData: () => sandboxSession.clearData() };
}

module.exports = { registerMcpAppScheme, createMcpAppSandbox, MCP_APP_PARTITION, MCP_APP_SCHEME };

"use strict";

const { RemoteHosts } = require("./remote-host");
const { releaseEditorWindow } = require("./window-lifecycle");

const NATIVE_CHANNELS = new Set([
  "minicode:workspace:trust", "minicode:openPreferredPath", "minicode:openPath", "minicode:revealPath",
  "minicode:fs:listTree", "minicode:fs:readFile", "minicode:fs:searchFiles", "minicode:fs:deletePath",
  "minicode:pty:spawn", "minicode:pty:write", "minicode:pty:resize", "minicode:pty:kill", "minicode:pty:restart",
  "minicode:pty:killConversation", "minicode:pty:list", "minicode:pty:snapshot", "minicode:pty:clear", "minicode:pty:ackExit",
  "minicode:env:detect",
]);

class RemoteDesktop {
  constructor({ userData, bridgeEndpoint, windowManager, browserBridge, log, spawnTransport }) {
    this.windows = windowManager;
    this.bridge = browserBridge;
    this.log = log;
    this.hosts = new RemoteHosts(userData, { bridgeEndpoint, spawnTransport });
  }

  broadcast() {
    const connections = this.hosts.list();
    for (const window of this.windows.getMainWindows()) window.webContents.send("minicode:connections:changed", connections);
  }

  list() { return this.hosts.list(); }
  discover() { return this.hosts.discover(); }
  save(connection) { const result = this.hosts.save(connection); this.broadcast(); return result; }
  async remove(id) {
    if (this.windows.getMainWindows().some((window) => this.windows.getWindowRecord(window).remoteHostId === id)) {
      throw new Error("Close this connection's windows before removing it");
    }
    await this.hosts.remove(id);
    this.broadcast();
  }

  async connect(id) {
    try {
      const connecting = this.hosts.connect(id);
      this.broadcast();
      const host = await connecting;
      if (!host.desktopBound) {
        host.desktopBound = true;
        this.bridge.registerRemoteHost(id, host);
        host.on("nativeEvent", (event) => {
          const record = this.windows.getWindowRecordByInstanceId(event.windowInstanceId);
          if (record && record.remoteHostId === id && ["minicode:pty:data", "minicode:pty:exit"].includes(event.channel)) {
            record.webContents.send(event.channel, event.payload);
          }
        });
        host.on("status", (_status, error) => {
          if (host.closed) this.bridge.unregisterRemoteHost(id, host);
          if (error) this.log(`[desktop] remote connection ${id}: ${error.message}`);
          this.broadcast();
        });
        await this.flushReleasedWindows(id, host);
        for (const window of this.windows.getMainWindows()) {
          const record = this.windows.getWindowRecord(window);
          if (record.remoteHostId !== id) continue;
          record.runtime = { ...host.runtime(), revision: record.runtime.revision + 1 };
          this.windows.reloadWindow(window);
        }
      }
      return host;
    } finally { this.broadcast(); }
  }

  async open(id) {
    const host = await this.connect(id);
    const window = await this.windows.createNewWindow({ remoteHostId: id, runtime: host.runtime() });
    this.windows.focusMainWindow(window);
  }

  async disconnect(id) { await this.hosts.disconnect(id); this.broadcast(); }

  host(record) {
    const host = this.hosts.hosts.get(record.remoteHostId);
    if (!host || host.closed) throw new Error("This remote desktop connection is disconnected");
    return host;
  }

  invokes(channel) { return NATIVE_CHANNELS.has(channel); }
  invoke(record, channel, args) { return this.host(record).native(record, channel, args); }
  mapUrl(record, url) { return this.host(record).mapUrl(url); }

  async releaseWindow(record) {
    const pending = this.hosts.saved.releasedWindows || (this.hosts.saved.releasedWindows = []);
    if (!pending.some((item) => item.windowInstanceId === record.windowInstanceId)) {
      pending.push({ remoteHostId: record.remoteHostId, remoteGeneration: record.runtime.remoteHost.generation,
        windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId });
      this.hosts.persist();
    }
    const host = this.hosts.hosts.get(record.remoteHostId);
    if (host && !host.closed) await this.flushReleasedWindows(record.remoteHostId, host);
    return { pending: this.hosts.saved.releasedWindows.some((item) => item.windowInstanceId === record.windowInstanceId) };
  }

  async flushReleasedWindows(id, host) {
    for (const record of [...(this.hosts.saved.releasedWindows || [])].filter((item) => item.remoteHostId === id || item.remoteGeneration === host.ready.generation)) {
      await Promise.all([
        host.native(record, "minicode:window:release", [], { lifecycle: true }),
        releaseEditorWindow({ ...host.runtime(), ...record, desktopLifecycleToken: host.ready.bridge_token }),
      ]);
      this.hosts.saved.releasedWindows = this.hosts.saved.releasedWindows.filter((item) => item.windowInstanceId !== record.windowInstanceId);
      this.hosts.persist();
    }
  }

  async close() { await this.hosts.close(); }
}

module.exports = { RemoteDesktop };

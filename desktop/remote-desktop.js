"use strict";

const { RemoteHosts } = require("./remote-host");
const { releaseEditorWindow } = require("./window-lifecycle");

const NATIVE_CHANNELS = new Set([
  "minicode:workspace:trust", "minicode:workspace:directoryEntries", "minicode:openPreferredPath", "minicode:openPath", "minicode:revealPath",
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
    this.hosts = new RemoteHosts(userData, { bridgeEndpoint, spawnTransport, log });
    this.windowReleases = new WeakMap();
  }

  broadcast() {
    const connections = this.hosts.list();
    for (const window of this.windows.getMainWindows()) window.webContents.send("minicode:connections:changed", connections);
  }

  async refresh() {
    await this.hosts.refresh();
    for (const [id, host] of [...this.hosts.hosts]) {
      if (host.closed) continue;
      const connection = this.hosts.catalog.find(item => item.id === id);
      if (connection && ["kind", "alias", "sshHost", "sshPort", "identity", "distribution"]
        .every(field => host.connection[field] === connection[field])) {
        host.connection = connection;
        continue;
      }
      const reconnect = host.status === "connecting" || host.status === "connected";
      await this.closeHost(host);
      await this.hosts.refreshing;
      if (reconnect && this.hosts.catalog.some(item => item.id === id)) await this.connectHost(id);
    }
    return this.hosts.list();
  }
  async list() { const connections = await this.refresh(); this.broadcast(); return connections; }
  discover(kind) { return this.hosts.discover(kind); }
  async save(connection) { return (await this.saveMany([connection]))[0] ?? null; }
  async saveMany(connections) {
    const entries = this.hosts.saveMany(connections);
    const ids = new Set(entries.map(entry => entry.id));
    const available = await this.refresh();
    this.broadcast();
    return available.filter(connection => ids.has(connection.id));
  }
  async remove(id) {
    if (this.windows.getMainWindows().some((window) => this.windows.getWindowRecord(window).remoteHostId === id)) {
      throw new Error("Close this connection's windows before removing it");
    }
    await this.hosts.remove(id);
    this.broadcast();
  }

  async connect(id) {
    await this.refresh();
    return this.connectHost(id);
  }

  async connectHost(id) {
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

  async closeHost(host) {
    let release;
    while ((release = this.windowReleases.get(host))) await release.task;
    await host.close();
  }

  async disconnect(id) {
    const host = this.hosts.hosts.get(id);
    if (host) await this.closeHost(host);
    this.broadcast();
  }

  host(record) {
    const host = this.hosts.hosts.get(record.remoteHostId);
    if (!host || host.closed) throw new Error("This remote desktop connection is disconnected");
    return host;
  }

  invokes(channel) { return NATIVE_CHANNELS.has(channel); }
  invoke(record, channel, args) { return this.host(record).native(record, channel, args); }
  mapUrl(record, url) { return this.host(record).mapUrl(url); }

  async releaseWindow(record, retry = false) {
    const pending = this.hosts.saved.releasedWindows || (this.hosts.saved.releasedWindows = []);
    if (!pending.some((item) => item.windowInstanceId === record.windowInstanceId)) {
      pending.push({ remoteHostId: record.remoteHostId, remoteGeneration: record.runtime.remoteHost.generation,
        windowOwnerId: record.windowOwnerId, windowInstanceId: record.windowInstanceId });
      this.hosts.persist();
    }
    const host = this.hosts.hosts.get(record.remoteHostId);
    if (host) {
      const active = this.windowReleases.get(host);
      if (retry && active?.failed) this.windowReleases.delete(host);
      if (!host.closed) await this.flushReleasedWindows(record.remoteHostId, host);
    }
    return { pending: this.hosts.saved.releasedWindows.some((item) => item.windowInstanceId === record.windowInstanceId) };
  }

  flushReleasedWindows(id, host) {
    const active = this.windowReleases.get(host);
    if (active) return active.task;
    const release = { task: null, failed: false };
    release.task = Promise.resolve().then(async () => {
      let record;
      while ((record = (this.hosts.saved.releasedWindows || []).find(item => item.remoteGeneration === host.ready.generation))) {
        await host.waitForNativeRequests(record.windowInstanceId);
        const results = await Promise.allSettled([
          host.native(record, "minicode:window:release", [], { lifecycle: true }),
          releaseEditorWindow({ ...host.runtime(), ...record, desktopLifecycleToken: host.ready.bridge_token }),
        ]);
        const errors = results.filter(result => result.status === "rejected").map(result => result.reason);
        if (errors.length === 1) throw errors[0];
        if (errors.length > 1) throw new AggregateError(errors, errors.map(error => error.message).join("; "));
        this.hosts.saved.releasedWindows = this.hosts.saved.releasedWindows.filter(item => item.windowInstanceId !== record.windowInstanceId);
        this.hosts.persist();
      }
    }).then(() => {
      this.windowReleases.delete(host);
    }, error => {
      // Only main's existing explicit cleanup retry can retire this failure.
      release.failed = true;
      throw error;
    });
    this.windowReleases.set(host, release);
    return release.task;
  }

  async close() { await Promise.all([...this.hosts.hosts.values()].map(host => this.closeHost(host))); }
}

module.exports = { RemoteDesktop };

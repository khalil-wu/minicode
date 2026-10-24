"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, execFile } = require("node:child_process");
const { EventEmitter, once } = require("node:events");
const { promisify } = require("node:util");
const { atomicWriteTextSync } = require("./utils");

const MAX_FRAME_BYTES = 1024 * 1024;
const CHUNK_BYTES = 64 * 1024;
const exec = promisify(execFile);
const quote = (value) => "'" + String(value).replace(/'/g, "'\\''") + "'";

function sshConfigValue(raw, multiple = false) {
  const values = [];
  let value = "", quoted = false, escaped = false;
  for (const character of raw) {
    if (escaped) { value += character === '"' ? character : "\\" + character; escaped = false; }
    else if (character === '"' && (multiple || !value || quoted)) quoted = !quoted;
    else if (character === "\\") escaped = true;
    else if (!quoted && character === "#" && (!multiple || values.length)) break;
    else if (multiple && !quoted && /[ \t=]/.test(character)) { if (value) { values.push(value); value = ""; } }
    else value += character;
  }
  if (quoted || escaped) throw new Error(`Unexpected line break in SSH config: ${raw}`);
  if (!multiple) return value.trim();
  if (value) values.push(value);
  if (!values.length) throw new Error("Missing value in SSH config");
  return values.length > 1 ? values : values[0].split(/\s+/).filter(Boolean);
}

function discoverSshAliases(entrypoint, homeDirectory) {
  if (!fs.existsSync(entrypoint)) return [];
  const aliases = new Set(), visited = new Set();
  const rootDirectory = path.dirname(path.resolve(entrypoint));
  const visit = configPath => {
    if (visited.has(configPath)) return;
    visited.add(configPath);
    let inBlock = false;
    for (const line of fs.readFileSync(configPath, "utf8").split(/\r?\n/)) {
      const directive = line.match(/^\s*([^#\s=]+)[ \t]*(?:=[ \t]*)?(.*)$/);
      if (!directive) continue;
      const keyword = directive[1].toLowerCase();
      if (keyword === "host") {
        inBlock = true;
        const alias = sshConfigValue(directive[2], true).find(value => !/[!*?[\]]/.test(value));
        if (alias) aliases.add(alias);
      } else if (keyword === "match") inBlock = true;
      // Source scans root directives; Host/Match own the following Include lines.
      else if (keyword === "include" && !inBlock) {
        for (const value of sshConfigValue(directive[2]).split(/\s+/).filter(Boolean)) {
          const expanded = value === "~" ? homeDirectory
            : value.startsWith("~/") || value.startsWith("~\\") ? path.join(homeDirectory, value.slice(2)) : value;
          const includePath = path.isAbsolute(expanded) ? expanded : path.resolve(rootDirectory, expanded);
          const matches = fs.globSync(includePath);
          if (!matches.length && !/[*?[\]{}()]/.test(includePath)) {
            if (fs.existsSync(includePath)) visit(includePath);
          } else for (const match of matches) if (fs.statSync(match).isFile()) visit(match);
        }
      }
    }
  };
  visit(path.resolve(entrypoint));
  return [...aliases].filter(alias => alias.trim().toLowerCase() !== "colima").sort((left, right) => left.localeCompare(right));
}

function sshMetadata(stdout) {
  let hostname = null, port = null, identity = null;
  for (const line of stdout.split(/\r?\n/)) {
    const directive = line.match(/^\s*([^#\s=]+)[ \t]*(?:=[ \t]*)?(.*)$/);
    if (!directive) continue;
    const keyword = directive[1].toLowerCase();
    if (!["hostname", "port", "identityfile"].includes(keyword)) continue;
    const value = sshConfigValue(directive[2]);
    if (!value) continue;
    if (keyword === "hostname" && hostname === null) hostname = value;
    else if (keyword === "port" && port === null) {
      const parsed = Number(value);
      if (Number.isSafeInteger(parsed)) port = parsed;
    } else if (keyword === "identityfile" && identity === null && value !== "none") identity = value;
  }
  return { hostname, port, identity };
}

function remoteLoginShellCommand(command) {
  const payload = 'PATH="${MINICODE_INSTALL_DIR:-$HOME/.local/bin}:$PATH"; export PATH; ' + command;
  const execute = 'exec /bin/sh -c "$MINICODE_REMOTE_PAYLOAD"';
  const csh = ['set loginsh=1', 'if ( -r /etc/csh.login ) source /etc/csh.login',
    'if ( -r ~/.login ) source ~/.login', execute].join('; ');
  const script = [
    'if [ -z "$SHELL" ] || [ ! -x "$SHELL" ]; then echo "MiniCode remote SSH requires SHELL to point to an executable login shell" >&2; exit 127; fi;',
    'MINICODE_REMOTE_PAYLOAD="$1"; export MINICODE_REMOTE_PAYLOAD;',
    'case "${SHELL##*/}" in',
    `csh|tcsh) exec "$SHELL" -i -c ${quote(csh)} ;;`,
    `nu) exec "$SHELL" -l -i -c ${quote('exec /bin/sh -c $env.MINICODE_REMOTE_PAYLOAD')} ;;`,
    `*) exec "$SHELL" -l -i -c ${quote(execute)} ;;`,
    'esac',
  ].join(' ');
  return `sh -c ${quote(script)} sh ${quote(payload)}`;
}

function sshCommand(connection, command) {
  const args = ["-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=12", "-o", "ExitOnForwardFailure=yes"];
  const alias = connection.alias?.trim();
  if (!alias) {
    if (connection.sshPort != null) args.push("-p", String(connection.sshPort));
    if (connection.identity) args.push("-i", connection.identity);
  }
  args.push("--", alias || connection.sshHost, remoteLoginShellCommand(command));
  return ["ssh", args];
}

function transportCommand(connection, desktopId, marker) {
  const remote = ["minicode-host", "proxy", "--desktop-id", desktopId, "--marker", marker];
  if (connection.kind === "wsl") {
    return ["wsl.exe", ["--distribution", connection.distribution, "--exec", ...remote]];
  }
  if (connection.kind !== "ssh") throw new Error("Unknown desktop connection kind");
  return sshCommand(connection, `exec ${remote.map(quote).join(" ")}`);
}

class RemoteHost extends EventEmitter {
  constructor(connection, desktopId, { spawnTransport = spawn, bridgeEndpoint } = {}) {
    super();
    this.connection = connection;
    this.desktopId = desktopId;
    this.spawnTransport = spawnTransport;
    this.bridgeEndpoint = bridgeEndpoint;
    this.streams = new Map();
    this.servers = new Map();
    this.nativeRequests = new Set();
    this.nextId = 1;
    this.closed = false;
    this.status = "disconnected";
    this.queue = Promise.resolve();
    this.inputQueue = Promise.resolve();
    this.buffer = Buffer.alloc(0);
  }

  async connect() {
    this.status = "connecting";
    if (this.connection.kind === "ssh") {
      try {
        const lookup = await this.runSshCommand("if command -v minicode-host >/dev/null 2>&1; then exit 0; fi; exit 86", "MiniCode host lookup");
        if (lookup.code === 86) throw new Error("No `minicode-host` found in the remote login shell PATH. Install MiniCode on the remote machine before connecting.");
        if (lookup.code !== 0) throw new Error(`SSH MiniCode host lookup exited (${lookup.signal || lookup.code}): ${lookup.stderr || lookup.stdout}`);
        const version = await this.runSshCommand("minicode-host --version", "MiniCode host version check");
        if (version.code !== 0) throw new Error(`SSH MiniCode host version check exited (${version.signal || version.code}): ${version.stderr || version.stdout}`);
        this.installedVersion = version.stdout;
      } catch (error) {
        await this.close(error);
        throw error;
      }
    }
    const marker = crypto.randomBytes(32);
    const [program, args] = transportCommand(this.connection, this.desktopId, marker.toString("hex"));
    this.process = this.spawnTransport(program, args, { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    this.stderr = "";
    this.process.stderr.on("data", (chunk) => { this.stderr = (this.stderr + chunk.toString("utf8")).slice(-16_384); });
    let marked = false;
    let resolveReady, rejectReady;
    const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
    const failed = (error) => {
      rejectReady(error);
      if (!this.closed) void this.close(error);
    };
    const timeout = setTimeout(() => failed(new Error("Remote MiniCode host did not become ready within 60 seconds")), 60_000);
    this.process.once("error", failed);
    this.process.stdin.on("error", failed);
    this.process.stdout.on("error", failed);
    this.process.once("exit", (code, signal) => failed(new Error(`Remote MiniCode transport exited (${signal || code}): ${this.stderr.trim()}`)));
    this.process.stdout.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      if (!marked) {
        const index = this.buffer.indexOf(marker);
        if (index === -1) { this.buffer = this.buffer.subarray(Math.max(0, this.buffer.length - marker.length + 1)); return; }
        this.buffer = this.buffer.subarray(index + marker.length);
        marked = true;
      }
      try {
        while (this.buffer.length >= 4) {
          const size = this.buffer.readUInt32BE(0);
          if (size > MAX_FRAME_BYTES) throw new Error("Remote desktop frame exceeds 1 MiB");
          if (this.buffer.length < size + 4) break;
          const frame = JSON.parse(this.buffer.subarray(4, size + 4).toString("utf8"));
          this.buffer = this.buffer.subarray(size + 4);
          this.inputQueue = this.inputQueue.then(async () => {
            if (frame.type === "ready") {
              if (this.ready || frame.protocol !== 1 || frame.stream_window_bytes !== 262144
                || frame.stream_chunk_bytes !== CHUNK_BYTES) throw new Error("Remote MiniCode desktop protocol is incompatible");
              this.ready = frame;
              resolveReady(frame);
            } else await this.receive(frame);
          }).catch(failed);
        }
      } catch (error) { failed(error); }
    });
    try {
      await ready;
      this.backendPort = await this.listen("backend", { type: "backend" });
      this.status = "connected";
      this.emit("status", this.status);
      return this;
    } finally { clearTimeout(timeout); }
  }

  runSshCommand(command, operation) {
    const [program, args] = sshCommand(this.connection, command);
    const child = this.spawnTransport(program, args, { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    this.process = child;
    return new Promise((resolve, reject) => {
      let stdout = "", stderr = "";
      const timeout = setTimeout(() => {
        child.kill();
        reject(new Error(`SSH ${operation} timed out after 60 seconds: ${stderr.trim()}`));
      }, 60_000);
      child.stdout.on("data", chunk => { stdout = (stdout + chunk.toString("utf8")).slice(-16_384); });
      child.stderr.on("data", chunk => { stderr = (stderr + chunk.toString("utf8")).slice(-16_384); });
      child.once("error", error => { clearTimeout(timeout); reject(error); });
      child.once("close", (code, signal) => {
        clearTimeout(timeout);
        if (this.closed) reject(new Error(`SSH ${operation} was disconnected`));
        else resolve({ code, signal, stdout: stdout.trim(), stderr: stderr.trim() });
      });
      child.stdin.end();
    });
  }

  send(frame) {
    const operation = this.queue.then(async () => {
      if (this.closed) throw new Error("Remote desktop attachment is disconnected");
      const payload = Buffer.from(JSON.stringify(frame));
      if (payload.length > MAX_FRAME_BYTES) throw new Error("Remote desktop frame exceeds 1 MiB");
      const header = Buffer.allocUnsafe(4);
      header.writeUInt32BE(payload.length);
      if (!this.process.stdin.write(Buffer.concat([header, payload]))) await once(this.process.stdin, "drain");
    });
    this.queue = operation.catch((error) => { void this.close(error); });
    return operation;
  }

  track(id, socket) {
    const stream = { socket, sendCredit: this.ready.stream_window_bytes, receiveCredit: this.ready.stream_window_bytes,
      waiters: [], received: Promise.resolve(), sent: Promise.resolve(), peerEnded: false };
    this.streams.set(id, stream);
    socket.on("data", (data) => {
      socket.pause();
      const write = async () => {
        for (let index = 0; index < data.length; index += this.ready.stream_chunk_bytes) {
          const chunk = data.subarray(index, index + this.ready.stream_chunk_bytes);
          while (stream.sendCredit < chunk.length) await new Promise((resolve, reject) => stream.waiters.push({ resolve, reject }));
          stream.sendCredit -= chunk.length;
          await this.send({ type: "data", id, data: chunk.toString("base64") });
        }
        if (!socket.destroyed) socket.resume();
      };
      stream.sent = stream.sent.then(write).catch((error) => socket.destroy(error));
    });
    socket.on("end", () => {
      stream.sent = stream.sent.then(() => this.send({ type: "end", id })).catch((error) => socket.destroy(error));
    });
    socket.on("error", (error) => { this.emit("streamError", { id, error }); });
    socket.on("close", () => {
      this.streams.delete(id);
      for (const waiter of stream.waiters.splice(0)) waiter.reject(new Error("Remote desktop stream closed"));
      if (!this.closed) void this.send({ type: "close", id }).catch(() => {});
    });
  }

  async receive(frame) {
    if (frame.type === "event") { this.emit("nativeEvent", frame); return; }
    if (frame.type === "pong") return;
    if (frame.type === "open") {
      if (frame.target !== "bridge" || frame.id % 2 !== 0 || this.streams.has(frame.id)) throw new Error("Invalid remote bridge stream");
      const bridge = new URL(this.bridgeEndpoint);
      const socket = net.createConnection({ host: bridge.hostname, port: Number(bridge.port), allowHalfOpen: true });
      this.track(frame.id, socket);
      return;
    }
    const stream = this.streams.get(frame.id);
    const socket = stream?.socket;
    if (frame.type === "close" || frame.type === "error") {
      if (socket) {
        if (frame.type === "close" && stream.peerEnded) stream.received = stream.received.then(() => socket.destroy());
        else socket.destroy(frame.type === "error" ? new Error(frame.error) : undefined);
      }
      return;
    }
    if (!socket) return; // The original local consumer may have closed this stream.
    if (frame.type === "credit") {
      if (!Number.isInteger(frame.bytes) || frame.bytes <= 0
        || stream.sendCredit + frame.bytes > this.ready.stream_window_bytes) throw new Error("Invalid remote desktop stream credit");
      stream.sendCredit += frame.bytes;
      for (const waiter of stream.waiters.splice(0)) waiter.resolve();
      return;
    }
    if (frame.type === "end") {
      stream.peerEnded = true;
      stream.received = stream.received.then(() => new Promise((resolve, reject) => {
        socket.end(error => error ? reject(error) : resolve());
      })).catch(error => socket.destroy(error));
      return;
    }
    if (frame.type !== "data") throw new Error(`Unknown remote desktop frame: ${frame.type}`);
    const data = Buffer.from(frame.data, "base64");
    if (data.length > this.ready.stream_chunk_bytes || data.length > stream.receiveCredit) throw new Error("Remote desktop stream exceeded its receive credit");
    stream.receiveCredit -= data.length;
    stream.received = stream.received.then(async () => {
      await new Promise((resolve, reject) => socket.write(data, (error) => error ? reject(error) : resolve()));
      stream.receiveCredit += data.length;
      await this.send({ type: "credit", id: frame.id, bytes: data.length });
    }).catch((error) => socket.destroy(error));
  }

  async listen(key, target) {
    const existing = this.servers.get(key);
    if (existing) return existing.listeningPort;
    const server = net.createServer({ allowHalfOpen: true }, (socket) => {
      socket.pause();
      const id = this.nextId;
      this.nextId += 2;
      this.track(id, socket);
      void this.send({ type: "open", id, target: target.type, ...(target.port ? { port: target.port, host: target.host } : {}) })
        .then(() => socket.resume(), (error) => socket.destroy(error));
    });
    server.on("error", (error) => { void this.close(error); });
    this.servers.set(key, server);
    server.listeningPort = new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => resolve(server.address().port));
    });
    return server.listeningPort;
  }

  async mapUrl(raw) {
    const url = new URL(raw);
    if (!["http:", "https:", "ws:", "wss:"].includes(url.protocol)
      || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) return raw;
    const remotePort = Number(url.port || (["https:", "wss:"].includes(url.protocol) ? 443 : 80));
    const originalHost = url.hostname === "[::1]" ? "::1" : url.hostname;
    const localPort = await this.listen(`preview:${originalHost}:${remotePort}`, { type: "preview", host: originalHost, port: remotePort });
    url.hostname = "127.0.0.1";
    url.port = String(localPort);
    return url.toString();
  }

  runtime() {
    return { revision: 1, backendPort: this.backendPort, apiBaseUrl: `http://127.0.0.1:${this.backendPort}`,
      wsBaseUrl: `ws://127.0.0.1:${this.backendPort}`, runtimeToken: this.ready.runtime_token,
      remoteHost: { id: this.connection.id, displayName: this.connection.displayName, kind: this.connection.kind,
        platform: this.ready.platform === "windows" ? "win32" : this.ready.platform,
        home: this.ready.home, generation: this.ready.generation } };
  }

  native(record, channel, args, { lifecycle = false } = {}) {
    if (this.closing) return Promise.reject(new Error("Remote desktop attachment is disconnecting"));
    const operation = { windowInstanceId: record.windowInstanceId, task: null };
    operation.task = (async () => {
      const response = await fetch(`${this.runtime().apiBaseUrl}/api/desktop-host/native`, {
        method: "POST", headers: { "content-type": "application/json", "x-minicode-token": this.ready.runtime_token,
          ...(lifecycle ? { "x-minicode-desktop-lifecycle-token": this.ready.bridge_token } : {}) },
        body: JSON.stringify({ window_instance_id: record.windowInstanceId, window_owner_id: record.windowOwnerId,
          window_binding_token: record.windowBindingToken || "", channel, args }), signal: AbortSignal.timeout(30_000),
      });
      if (!response.ok) throw new Error(`Remote desktop operation ${channel}: HTTP ${response.status} ${await response.text()}`);
      return response.json();
    })().finally(() => this.nativeRequests.delete(operation));
    this.nativeRequests.add(operation);
    return operation.task;
  }

  async waitForNativeRequests(windowInstanceId) {
    let pending;
    while ((pending = [...this.nativeRequests].filter(operation =>
      windowInstanceId === undefined || operation.windowInstanceId === windowInstanceId)).length) {
      // Each original caller keeps its result/error; this join owns completion.
      await Promise.allSettled(pending.map(operation => operation.task));
    }
  }

  async close(error) {
    if (error) {
      this.closeFailure ||= error;
      if (this.closing && !this.closed) for (const stream of this.streams.values()) stream.socket.destroy(error);
    }
    if (!this.closing) this.closing = this.closeTransport(error);
    return this.closing;
  }

  async closeTransport(error) {
    if (this.status === "connected" && !error) await this.waitForNativeRequests();
    error = this.closeFailure || error;
    const graceful = this.status === "connected" && !error;
    this.closed = true;
    this.status = "disconnected";
    for (const stream of this.streams.values()) stream.socket.destroy();
    this.streams.clear();
    const closing = [...this.servers.values()].map((server) => new Promise((resolve) => server.close(resolve)));
    this.servers.clear();
    let transportClosed;
    if (this.process) {
      if (this.process.pid && this.process.exitCode === null && this.process.signalCode === null) {
        transportClosed = once(this.process, "close");
      }
      this.process.stdin.end(); // EOF detaches; it does not stop the persistent MiniCode host.
      if (transportClosed && !graceful) this.process.kill();
    }
    await Promise.all(closing);
    if (transportClosed) {
      const [code, signal] = await transportClosed;
      if (graceful && code !== 0) {
        const failure = new Error(`Remote MiniCode transport exited (${signal || code}): ${this.stderr.trim()}`);
        this.emit("status", this.status, failure);
        throw failure;
      }
    }
    this.emit("status", this.status, error);
  }
}

class RemoteHosts {
  constructor(userData, dependencies = {}) {
    this.file = path.join(userData, "remote-hosts.json");
    this.saved = fs.existsSync(this.file) ? JSON.parse(fs.readFileSync(this.file, "utf8"))
      : { desktopId: crypto.randomBytes(16).toString("hex"), connections: [] };
    this.dependencies = dependencies;
    this.log = dependencies.log ?? console.warn;
    this.hosts = new Map();
    this.catalog = this.saved.connections.filter(connection => connection.source !== "discovered");
    this.refreshGeneration = 0;
  }

  persist(saved = this.saved) {
    atomicWriteTextSync(this.file, JSON.stringify(saved, null, 2));
    this.saved = saved;
  }
  list() {
    return this.catalog.map(connection => {
      const host = this.hosts.get(connection.id), status = host?.status || "disconnected";
      return { ...connection, status, ...(status === "connected" ? { appServerVersion: host.ready.version } : {}) };
    });
  }
  async resolveAlias(connection) {
    const homeDirectory = this.dependencies.homeDirectory ?? os.homedir();
    const entrypointPath = path.join(homeDirectory, ".ssh", "config");
    let result;
    try {
      result = await (this.dependencies.execFile ?? exec)("ssh", ["-G", "-F", entrypointPath, connection.alias],
        { windowsHide: true, encoding: "utf8" });
    } catch (error) {
      this.log(`[desktop] ssh_alias_resolution_failed ${JSON.stringify({ entrypointPath, alias: connection.alias,
        code: error.code, stderr: error.stderr, error: error.message })}`);
      if (Number.isInteger(error.code)) return null;
      throw error;
    }
    const metadata = sshMetadata(result.stdout);
    if (metadata.hostname === null) {
      this.log(`[desktop] ssh_alias_missing_hostname ${JSON.stringify({ entrypointPath, alias: connection.alias })}`);
      return null;
    }
    if (metadata.hostname.trim().toLowerCase() === "github.com") {
      this.log(`[desktop] ssh_alias_excluded ${JSON.stringify({ entrypointPath, alias: connection.alias, hostname: metadata.hostname })}`);
      return null;
    }
    return { ...connection, sshHost: metadata.hostname, sshPort: metadata.port, identity: metadata.identity };
  }
  refresh() {
    const generation = ++this.refreshGeneration;
    this.refreshing = Promise.all(this.saved.connections.map(connection => connection.source === "discovered"
      ? this.resolveAlias(connection) : connection)).then(connections => {
      if (generation !== this.refreshGeneration) return this.refreshing;
      this.catalog = connections.filter(connection => connection !== null);
      return this.list();
    });
    return this.refreshing;
  }
  save(connection) { return this.saveMany([connection])[0]; }
  saveMany(connections) {
    const entries = connections.map(connection => {
      if (!["ssh", "wsl"].includes(connection.kind)) throw new Error("Desktop connections use SSH or WSL");
      if (connection.color != null && !["black", "red", "orange", "yellow", "green", "blue", "purple", "pink"].includes(connection.color)) throw new Error("Invalid desktop connection color");
      const alias = connection.kind === "ssh" && connection.source === "discovered" ? connection.alias.trim() : null;
      const entry = { id: connection.id || crypto.randomUUID(), kind: connection.kind, displayName: connection.displayName.trim(),
        ...(connection.color != null ? { color: connection.color } : {}),
        ...(connection.kind === "ssh" ? alias !== null
          ? { source: "discovered", alias, sshHost: alias, sshPort: null, identity: null }
          : { sshHost: connection.sshHost.trim(), sshPort: connection.sshPort ?? null, identity: connection.identity || null }
          : { distribution: connection.distribution.trim() }) };
      if (!/^[A-Za-z0-9_-]{1,96}$/.test(entry.id)) throw new Error("Invalid desktop connection identity");
      if (!entry.displayName || !(entry.kind === "ssh" ? entry.sshHost : entry.distribution)) throw new Error("A connection name and target are required");
      if (entry.kind === "ssh" && entry.sshHost.startsWith("-")) throw new Error("SSH hostname cannot be an option");
      if (entry.sshPort != null && (!Number.isInteger(entry.sshPort) || entry.sshPort < 1 || entry.sshPort > 65535)) throw new Error("SSH port must be between 1 and 65535");
      return entry;
    });
    const ids = new Set(entries.map(entry => entry.id));
    if (ids.size !== entries.length) throw new Error("Desktop connection identities must be unique");
    const next = [...this.saved.connections.filter(connection => !ids.has(connection.id)), ...entries];
    const names = new Set();
    for (const connection of next) {
      if (names.has(connection.displayName)) throw new Error("A connection with this display name already exists");
      names.add(connection.displayName);
    }
    this.persist({ ...this.saved, connections: next });
    return entries;
  }
  async remove(id) {
    const host = this.hosts.get(id);
    if (host && host.status !== "disconnected") throw new Error("Only disconnected connections can be removed");
    this.persist({ ...this.saved, connections: this.saved.connections.filter(item => item.id !== id) });
    this.catalog = this.catalog.filter(item => item.id !== id);
  }
  async connect(id) {
    const connection = this.catalog.find(item => item.id === id);
    if (!connection) throw new Error("Desktop connection is unavailable");
    const existing = this.hosts.get(id);
    if (existing && !existing.closed) return existing.connecting;
    const host = new RemoteHost(connection, this.saved.desktopId, this.dependencies);
    this.hosts.set(id, host);
    host.connecting = host.connect();
    await host.connecting;
    return host;
  }
  async disconnect(id) { const host = this.hosts.get(id); if (host) await host.close(); }
  async close() { await Promise.all([...this.hosts.values()].map((host) => host.close())); }
  async discover(kind) {
    const homeDirectory = this.dependencies.homeDirectory ?? os.homedir();
    const aliases = kind === "ssh" ? discoverSshAliases(path.join(homeDirectory, ".ssh", "config"), homeDirectory) : [];
    let distributions = [];
    if (kind === "wsl" && (this.dependencies.platform ?? process.platform) === "win32") {
      const { stdout } = await (this.dependencies.execFile ?? exec)("wsl.exe", ["--list", "--quiet"], { windowsHide: true, encoding: "buffer" });
      distributions = stdout.toString("utf16le").replace(/\0/g, "").split(/\r?\n/).map((name) => name.trim()).filter(Boolean);
    }
    return { sshAliases: aliases, wslDistributions: distributions };
  }
}

module.exports = { RemoteHost, RemoteHosts, transportCommand };

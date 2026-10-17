// Host contract: Codex Desktop 26.1013.40934 main lre/fre/mre/_re.
// The installed Source package owns native helper launch, stdio and shutdown.
import net from 'node:net';
import os from 'node:os';
import process from 'node:process';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';

const MAX_FRAME = 8 * 1024 * 1024;
const APPROVAL_TIMEOUT_MS = 300000;
const APPROVAL_PREFIX = 'computer-use-approval:';

export async function createComputerUseHost(options) {
  const { WindowsHelperTransport } = await import(pathToFileURL(options.transportModulePath).href);
  const sockets = new Set(), activeTurns = new Map(), approvals = new Map(), closingTransports = new Set();
  let transport, busy = false;
  const log = error => process.stderr.write((error.stack || String(error)) + '\n');
  const send = (socket, message) => {
    const bytes = Buffer.from(JSON.stringify(message), 'utf8'), frame = Buffer.alloc(4 + bytes.length);
    if (os.endianness() === 'LE') frame.writeUInt32LE(bytes.length, 0);
    else frame.writeUInt32BE(bytes.length, 0);
    bytes.copy(frame, 4);
    socket.write(frame);
  };
  const closeTransport = async () => {
    const original = transport;
    transport = undefined;
    if (original) {
      const closing = original.close();
      closingTransports.add(closing);
      try { await closing; } finally { closingTransports.delete(closing); }
    }
    // The desktop host normally remains alive after the last socket. This
    // dedicated process must also await that original close before it exits.
    await Promise.all(closingTransports);
  };
  const getTransport = () => transport ||= new WindowsHelperTransport({
    helperArgs: ['--parent-pid', String(process.pid)],
    helperCommand: options.helperPath,
    helperEnv: { CODEX_HOME: options.codexHome, CODEX_CLI_PATH: options.codexCliPath },
  });
  const rejectApprovals = error => {
    for (const pending of approvals.values()) { clearTimeout(pending.timeout); pending.reject(error); }
    approvals.clear();
  };
  const approve = (socket, params) => new Promise((resolve, reject) => {
    const id = APPROVAL_PREFIX + randomUUID();
    const timeout = setTimeout(() => { approvals.delete(id); reject(new Error('Computer Use app approval timed out')); }, APPROVAL_TIMEOUT_MS);
    timeout.unref();
    approvals.set(id, { socket, resolve, reject, timeout });
    send(socket, { id, jsonrpc: '2.0', method: 'requestComputerUseApproval', params });
  });
  const turnKey = ({ sessionId, turnId }) => sessionId + '\0' + turnId;
  const handle = async (socket, message) => {
    if (typeof message.id === 'string' && message.id.startsWith(APPROVAL_PREFIX)) {
      const pending = approvals.get(message.id);
      if (!pending || pending.socket !== socket) return;
      approvals.delete(message.id); clearTimeout(pending.timeout);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
      return;
    }
    if (message.id == null || typeof message.method !== 'string') return;
    const closing = message.method === 'close' || message.method === 'request' && message.params?.method === 'end_turn';
    if (busy && !closing) {
      send(socket, { id: message.id, jsonrpc: '2.0', error: { code: -32000, message: 'Computer Use helper already has an active request' } });
      return;
    }
    if (!closing) busy = true;
    try {
      let result;
      if (message.method === 'ping') result = 'pong';
      else if (message.method === 'close') { activeTurns.delete(socket); await closeTransport(); result = null; }
      else if (message.method === 'request') {
        const { method, params, codexTurnMetadata } = message.params;
        if (typeof method !== 'string' || !method.trim()) throw new Error('Invalid Computer Use native pipe method');
        if (typeof codexTurnMetadata?.session_id === 'string' && typeof codexTurnMetadata?.turn_id === 'string') {
          activeTurns.set(socket, turnKey({ sessionId: codexTurnMetadata.session_id, turnId: codexTurnMetadata.turn_id }));
        }
        result = await getTransport().request(method, params, {
          codexTurnMetadata, createElicitation: request => approve(socket, request),
        });
        if (method === 'end_turn') activeTurns.delete(socket);
      } else throw new Error('Unsupported Computer Use native pipe method');
      if (!socket.destroyed) send(socket, { id: message.id, jsonrpc: '2.0', result });
    } catch (error) {
      if (!socket.destroyed) send(socket, { id: message.id, jsonrpc: '2.0', error: { code: -32000, message: error.message || String(error) } });
    } finally { if (!closing) busy = false; }
  };
  const server = net.createServer(socket => {
    sockets.add(socket);
    let buffer = Buffer.alloc(0);
    socket.on('data', chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4) {
        const length = os.endianness() === 'LE' ? buffer.readUInt32LE(0) : buffer.readUInt32BE(0);
        if (length > MAX_FRAME) { socket.destroy(new Error('Computer Use native pipe frame exceeds 8 MiB')); return; }
        if (buffer.length < length + 4) return;
        const bytes = buffer.subarray(4, length + 4); buffer = buffer.subarray(length + 4);
        try { void handle(socket, JSON.parse(bytes.toString('utf8'))).catch(log); }
        catch (error) { socket.destroy(error); return; }
      }
    });
    socket.on('error', log);
    socket.on('close', () => {
      sockets.delete(socket); activeTurns.delete(socket);
      if (sockets.size === 0) {
        rejectApprovals(new Error('Computer Use native pipe client disconnected'));
        void closeTransport().catch(log);
      }
    });
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(options.pipePath, () => { server.off('error', reject); resolve(); }); });
  return {
    pipePath: options.pipePath,
    async closeActiveTurn(params) {
      const key = turnKey(params);
      if (![...activeTurns.values()].includes(key)) return false;
      for (const [socket, current] of activeTurns) if (current === key) activeTurns.delete(socket);
      await closeTransport();
      return true;
    },
    async dispose() {
      rejectApprovals(new Error('Computer Use native pipe is shutting down'));
      try { await closeTransport(); }
      finally {
        for (const socket of sockets) socket.destroy();
        sockets.clear(); activeTurns.clear();
        await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
      }
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const host = await createComputerUseHost(JSON.parse(process.argv[2]));
  const send = value => process.stdout.write(JSON.stringify(value) + '\n');
  send({ type: 'ready', pipePath: host.pipePath });
  let disposed = false;
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', line => {
    const request = JSON.parse(line);
    void (async () => {
      try {
        let result;
        if (request.method === 'closeActiveTurn') result = await host.closeActiveTurn(request.params);
        else if (request.method === 'shutdown') { disposed = true; await host.dispose(); result = null; lines.close(); process.stdin.destroy(); }
        else throw new Error('Unsupported Computer Use host control method');
        send({ id: request.id, result });
      } catch (error) { send({ id: request.id, error: error.stack || String(error) }); }
    })();
  });
  lines.on('close', () => { if (!disposed) { disposed = true; void host.dispose().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; }); } });
}

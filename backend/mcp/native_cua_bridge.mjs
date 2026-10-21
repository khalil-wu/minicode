import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';

export function createNativeCuaExtension({ send, python, pythonSource, apiModule, appAccess }) {
  if (appAccess.windows?.exes?.length) throw new Error('MiniCode native CUA does not implement Windows publisher/signer rules');
  const approvals = new Map(), kernelTurns = new Map(), grants = new Set(), cleanupPins = new Map();
  let helper, failure, closed = false, sequence = 0;
  const keyFor = owner => JSON.stringify([owner.session_id, owner.turn_id]);
  const ownerFor = metadata => {
    const owner = metadata['x-codex-turn-metadata'];
    if (!owner?.session_id || !owner?.turn_id) throw new Error('Native CUA requires the real receiving thread and turn');
    return owner;
  };
  const startHelper = () => {
    const cleanups = [...cleanupPins.values()];
    let readyResolve, readyReject;
    const entry = { child: null, pins: new Map(), pending: new Map(), stopReason: null,
      ready: new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; }) };
    const child = entry.child = spawn(python, ['-u', '-X', 'utf8', '-c', pythonSource, JSON.stringify(cleanups)],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    const errors = [];
    child.stderr.on('data', chunk => errors.push(chunk));
    child.on('error', error => readyReject(error));
    entry.exited = new Promise((resolve, reject) => child.once('close', (code, signal) => {
      const receipt = { pid: child.pid, code, signal, reason: entry.stopReason || 'natural exit' };
      const error = new Error('Native CUA receiving helper closed: ' + JSON.stringify(receipt) + ': ' + Buffer.concat(errors).toString());
      send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: code === 0 || entry.stopReason ? 'info' : 'error', logger: 'native_cua', data: receipt } });
      readyReject(error);
      if (code !== 0) for (const pin of entry.pins.values()) cleanupPins.set(pin.target, pin);
      for (const waiter of entry.pending.values()) waiter.reject(error);
      entry.pending.clear();
      if (helper === entry) helper = undefined;
      if (code === 0 || entry.stopReason) resolve(receipt); else { failure = error; reject(error); }
    }));
    // Startup and shutdown both observe this same original process close.
    entry.exited.catch(() => {});
    createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', line => {
      const reply = JSON.parse(line);
      if (reply.ready) {
        if (reply.implementation !== 'minicode-win32' || reply.platform !== 'win32' || reply.cleanedTargetCount !== cleanups.length) {
          readyReject(new Error('Receiving helper did not confirm the selected Windows implementation and original target cleanup'));
          return;
        }
        for (const pin of cleanups) cleanupPins.delete(pin.target);
        readyResolve(entry);
        return;
      }
      const waiter = entry.pending.get(reply.id);
      entry.pending.delete(reply.id);
      if (reply.error) waiter.reject(new Error(reply.error)); else waiter.resolve(reply.result);
    });
    helper = entry;
    return entry;
  };
  const receivingHelper = async () => {
    if (closed) throw new Error('The original native CUA receiving thread is closed');
    if (failure) throw failure;
    return await (helper || startHelper()).ready;
  };
  const native = async (method, args, owner) => {
    const entry = await receivingHelper();
    if (method === 'select_window') {
      args = { ...args, target: randomUUID() };
      // Allocate the nonce before selection so cancellation owns its cleanup,
      // even when native selection has not returned yet.
      entry.pins.set(args.target, { window_id: args.window_id, target: args.target, owner });
    }
    const result = await new Promise((resolve, reject) => {
      const id = ++sequence;
      entry.pending.set(id, { resolve, reject, owner });
      entry.child.stdin.write(JSON.stringify({ id, method, args, owner }) + '\n');
    });
    if (method === 'end_turn') for (const [token, pin] of entry.pins) if (keyFor(pin.owner) === keyFor(owner)) entry.pins.delete(token);
    return result;
  };
  const endTurn = owner => native('end_turn', {}, owner);
  const stopHelper = async reason => {
    if (!helper && !cleanupPins.size) return;
    const entry = await (helper || startHelper()).ready;
    if (entry.pending.size) {
      entry.stopReason = reason;
      if (!entry.child.kill()) throw new Error('Original native CUA helper could not be terminated');
    } else entry.child.stdin.end();
    const receipt = await entry.exited;
    // A killed blocking call cannot run Python finally. A fresh receiving
    // helper removes only this process's original window nonces, then exits.
    if (cleanupPins.size) await stopHelper('original window target cleanup');
    return receipt;
  };
  const approve = (selected, message, signal) => new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const id = 'minicode-cua-app-' + (++sequence);
    const cancel = () => {
      approvals.delete(id);
      signal?.removeEventListener('abort', cancel);
      send({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: id, reason: 'Original CUA execution cancelled' } });
      reject(new Error('Original CUA execution cancelled'));
    };
    approvals.set(id, { resolve, reject, signal, cancel, kernelId: message.kernelId });
    signal?.addEventListener('abort', cancel, { once: true });
    send({ jsonrpc: '2.0', id, method: 'elicitation/create', params: {
      mode: 'form', message: 'Allow MiniCode to use ' + selected.name + '?', requestedSchema: {},
      _meta: { codex_approval_kind: 'mcp_tool_call', connector_id: 'computer-use', connector_name: 'Computer Use',
        persist: ['session'], riskLevel: 'high', tool_params: { app: selected.app_id },
        tool_params_display: [{ name: 'app', display_name: 'App', value: selected.name }] },
    } });
  });
  return {
    name: 'MiniCode native Windows computer companion',
    instructions: 'Independent Windows computer control through cua.getState() and cua.getApp({window_id}). Start with cua.getState() for the actual API documentation. No browser surface is supplied.',
    workerData: { apiModule },
    async initialize() { await receivingHelper(); },
    acceptResponse(message) {
      const waiter = approvals.get(message.id);
      if (!waiter || message.method) return false;
      approvals.delete(message.id);
      waiter.signal?.removeEventListener('abort', waiter.cancel);
      if (message.error) waiter.reject(new Error(message.error.message)); else waiter.resolve(message.result);
      return true;
    },
    async request(message, signal) {
      const owner = ownerFor(message.requestMeta);
      signal?.throwIfAborted();
      const turns = kernelTurns.get(message.kernelId) || new Map();
      turns.set(keyFor(owner), owner);
      kernelTurns.set(message.kernelId, turns);
      const result = await native(message.method, message.args, owner);
      if (message.method === 'select_window') {
        const key = JSON.stringify([owner.session_id, result.app_id]);
        const policy = appAccess.windows?.aumids?.[result.aumid] ?? appAccess.default_app_access;
        if (policy === 'deny') throw new Error('Computer Use access to this app is denied by the captured configuration');
        if (policy !== 'allow' && !grants.has(key)) {
          const choice = await approve(result, message, signal);
          if (choice?.action !== 'accept' || choice.content?.persist !== 'session') throw new Error('Computer Use app access was not approved');
          signal?.throwIfAborted();
          grants.add(key);
        }
        await native('authorize', { target: result.target }, owner);
      }
      signal?.throwIfAborted();
      return result;
    },
    async callTool(name, args) {
      if (name === '__minicode_close_cua_thread') {
        await this.close();
        return { content: [], isError: false, structuredContent: { closed: true } };
      }
      if (name !== '__minicode_close_cua_turn') return undefined;
      if (helper?.pending.size) {
        const active = [...helper.pending.values()].some(call => call.owner.session_id === args.sessionId && call.owner.turn_id === args.turnId);
        if (!active) return { content: [], isError: false, structuredContent: { closed: false } };
        await stopHelper('original turn cancelled');
        return { content: [], isError: false, structuredContent: { closed: true } };
      }
      const closed = await endTurn({ session_id: args.sessionId, turn_id: args.turnId });
      return { content: [], isError: false, structuredContent: { closed } };
    },
    async retireKernel(kernelId) {
      for (const waiter of [...approvals.values()]) if (waiter.kernelId === kernelId) waiter.cancel();
      if (closed) { kernelTurns.delete(kernelId); return; }
      if (helper?.pending.size) await stopHelper('original kernel cancelled');
      else for (const owner of (kernelTurns.get(kernelId) || new Map()).values()) await endTurn(owner);
      kernelTurns.delete(kernelId);
    },
    async close() {
      if (closed) return;
      closed = true;
      for (const waiter of [...approvals.values()]) waiter.cancel();
      await stopHelper('receiving thread closed');
      grants.clear();
      if (failure) throw failure;
    },
  };
}

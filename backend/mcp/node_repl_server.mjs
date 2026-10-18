// MiniCode's local companion implements the observable node_repl MCP contract.
// The public Codex checkout supplies MCP/Guardian integration, not this executor.
import { createInterface } from 'node:readline';
import { Worker } from 'node:worker_threads';
import { Session } from 'node:inspector';
import process from 'node:process';
import path from 'node:path';
import { stat } from 'node:fs/promises';

const workerSource = String.raw`
(async () => {
const { parentPort, workerData } = require('node:worker_threads');
const { createRequire, isBuiltin } = require('node:module');
const { pathToFileURL } = require('node:url');
const path = require('node:path');
const os = require('node:os');
const util = require('node:util');
const vm = require('node:vm');
const { AsyncLocalStorage } = require('node:async_hooks');
const acorn = require('internal/deps/acorn/acorn/dist/acorn');
const cwd = process.cwd();
const moduleDirs = [...workerData.moduleDirs];
const executions = new AsyncLocalStorage();
const emit = item => {
  const call = executions.getStore();
  if (!call) throw new Error('nodeRepl output requires a js execution');
  parentPort.postMessage({ kind: call.completed ? 'late_output' : 'output', id: call.id, requestId: call.requestId, requestMeta: call.meta, item });
};
async function importModule(specifier, options) {
  if (specifier === 'node:process' || specifier === 'process') throw new Error('node:process is unavailable in node_repl');
  if (isBuiltin(specifier) || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(specifier)) return import(specifier, options);
  if (specifier.startsWith('.') || path.isAbsolute(specifier)) return import(pathToFileURL(path.resolve(cwd, specifier)).href, options);
  const resolvers = [createRequire(path.join(cwd, '__node_repl__.cjs')), ...moduleDirs.map(dir => createRequire(path.join(dir, '__node_repl__.cjs')))];
  let missing;
  for (const resolve of resolvers) {
    let resolved;
    try { resolved = resolve.resolve(specifier); }
    catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; missing = error; continue; }
    return import(pathToFileURL(resolved).href, options);
  }
  throw missing;
}
function prepare(code, callId) {
  const tree = acorn.parse(code, { ecmaVersion: 'latest', sourceType: 'script', allowAwaitOutsideFunction: true });
  // Keep the hashbang and the complete directive prologue in their original
  // positions so entering the request context cannot disable strict mode.
  let enterAt = code.startsWith('#!') ? (code.indexOf('\n') < 0 ? code.length : code.indexOf('\n') + 1) : 0;
  for (const statement of tree.body) {
    if (statement.directive === undefined) break;
    enterAt = statement.end;
  }
  const starts = [];
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    if (value.type === 'ImportExpression') starts.push(value.start);
    for (const member of Object.values(value)) {
      if (Array.isArray(member)) member.forEach(visit);
      else if (member && typeof member === 'object') visit(member);
    }
  }
  visit(tree);
  for (const start of starts.sort((a,b) => b-a)) code = code.slice(0,start) + '__minicodeNodeImport' + code.slice(start+6);
  return code.slice(0, enterAt) + '\n;await __minicodeNodeEnter(' + callId + ');\n' + code.slice(enterAt);
}
const nodeRepl = {
  cwd, homeDir: os.homedir(), tmpDir: os.tmpdir(),
  get requestMeta() { return executions.getStore()?.meta || {}; },
  write(value) { emit({ type: 'text', text: typeof value === 'string' ? value : util.inspect(value, { colors: false, depth: null }) }); },
  async emitImage(image) {
    let mime, data;
    if (typeof image === 'string') {
      const match = /^data:(image\/[^;,]+);base64,([\s\S]+)$/.exec(image);
      if (!match) throw new TypeError('emitImage expects an image data URL or {bytes, mimeType}');
      [,mime,data] = match;
    } else {
      mime = image.mimeType;
      data = image.bytes !== undefined ? Buffer.from(image.bytes).toString('base64') : image.data;
      if (typeof mime !== 'string' || !mime.startsWith('image/') || typeof data !== 'string') throw new TypeError('emitImage expects image bytes and mimeType');
    }
    emit({ type: 'image', data, mimeType: mime,
      ...(image?.detail ? { _meta: { 'codex/imageDetail': image.detail } } : {}) });
  },
};
const userConsole = Object.freeze({ log: (...args) => nodeRepl.write(util.format(...args)),
  info: (...args) => nodeRepl.write(util.format(...args)), warn: (...args) => nodeRepl.write(util.format(...args)),
  error: (...args) => nodeRepl.write(util.format(...args)), debug: (...args) => nodeRepl.write(util.format(...args)) });
const calls = new Map();
const globals = { nodeRepl: Object.freeze(nodeRepl), console: userConsole, __minicodeNodeImport: importModule,
  __minicodeNodeEnter: id => {
    executions.enterWith(calls.get(id));
    // Let Runtime.evaluate register its awaited execution before user code can
    // occupy the thread, so Inspector can dispatch terminateExecution as well.
    return new Promise(resolve => setImmediate(resolve));
  } };
for (const name of ['Buffer','URL','URLSearchParams','TextEncoder','TextDecoder','Blob','fetch','FormData','Headers','Request','Response',
    'AbortController','AbortSignal','setTimeout','clearTimeout','setInterval','clearInterval','setImmediate','clearImmediate',
    'queueMicrotask','structuredClone','atob','btoa','crypto']) globals[name] = globalThis[name];
const context = vm.createContext(globals, { name: 'MiniCode Node REPL' });
// Inspector stores an execution-context ID, not a strong reference to this
// object. Keep the VM alive for the worker's lifetime, including idle periods.
globalThis.__minicodeNodeContext = context;
vm.runInContext('globalThis.global = globalThis', context);
process.on('uncaughtExceptionMonitor', error => {
  const call = executions.getStore();
  parentPort.postMessage({ kind: 'uncaught', requestId: call?.requestId, requestMeta: call?.meta,
    error: error.stack || String(error) });
});
parentPort.on('message', message => {
  if (message.kind === 'retire') { process.exit(0); }
  if (message.kind === 'add_module_dir') { moduleDirs.push(message.path); return; }
  if (message.kind === 'finished') {
    calls.get(message.id).completed = true;
    calls.delete(message.id);
    parentPort.postMessage({ kind: 'finished', id: message.id });
    return;
  }
  const call = { id: message.id, requestId: message.requestId, meta: message.meta || {}, completed: false };
  calls.set(call.id, call);
  try {
    parentPort.postMessage({ kind: 'prepared', id: call.id, expression: prepare(message.code, call.id) });
  } catch (error) {
    call.completed = true;
    calls.delete(call.id);
    parentPort.postMessage({ kind: 'complete', id: call.id, error: error.stack || String(error) });
  }
});
parentPort.postMessage({ kind: 'ready' });
})().catch(error => { throw error; });
`;

const moduleDirs = [];
let worker, resetting, generation = 0, sequence = 0, queue = Promise.resolve();
const pending = new Map(), requests = new Map();
const debuggerSession = new Session(); debuggerSession.connect();
const kernels = new Map(), workerSessions = new Map(), commands = new Map();
let commandSequence = 0;
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
const text = value => ({ type: 'text', text: value });
const errorResult = message => ({ content: [text(message)], isError: true });
const logError = error => send({ jsonrpc: '2.0', method: 'notifications/message',
  params: { level: 'error', logger: 'node_repl', data: error.stack || String(error) } });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

debuggerSession.on('NodeWorker.attachedToWorker', ({params}) => {
  workerSessions.set(params.workerInfo.workerId, params.sessionId);
  kernels.get(params.workerInfo.workerId)?.attached.resolve(params.sessionId);
});
debuggerSession.on('NodeWorker.receivedMessageFromWorker', ({params}) => {
  const message = JSON.parse(params.message);
  if (message.id !== undefined) {
    const command = commands.get(message.id);
    if (command) {
      commands.delete(message.id);
      if (message.error) command.reject(new Error(message.error.message));
      else command.resolve(message.result);
    }
    return;
  }
  if (message.method === 'Runtime.executionContextCreated' && message.params.context.name === 'MiniCode Node REPL') {
    const current = [...kernels.values()].find(item => workerSessions.get(item.id) === params.sessionId);
    current?.context.resolve(message.params.context.id);
  }
});
await new Promise((resolve, reject) => debuggerSession.post('NodeWorker.enable', { waitForDebuggerOnStart: false },
  error => error ? reject(error) : resolve()));
async function postInspector(current, method, params = {}) {
  const sessionId = await current.attached.promise;
  return await new Promise((resolve, reject) => {
    const id = ++commandSequence;
    commands.set(id, { kernel: current, resolve, reject });
    debuggerSession.post('NodeWorker.sendMessageToWorker', { sessionId, message: JSON.stringify({ id, method, params }) }, error => {
      if (error) { commands.delete(id); reject(error); }
    });
  });
}
function createKernel() {
  // Node 22 exposes AsyncContextFrame through this flag; Node 24 enables it
  // by default and removes the flag. Keep ALS on that backend: interrupting
  // its older async_hooks backend leaves a native async-id stack unbalanced.
  const execArgv = ['--expose-internals'];
  if (process.allowedNodeEnvironmentFlags.has('--experimental-async-context-frame')) execArgv.push('--experimental-async-context-frame');
  const kernel = new Worker(workerSource, { eval: true, execArgv, workerData: { moduleDirs }, stdout: true, stderr: true });
  const current = { kernel, id: String(kernel.threadId), bootstrap: deferred(), attached: deferred(),
    context: deferred(), exited: deferred(), failure: null };
  kernels.set(current.id, current);
  if (workerSessions.has(current.id)) current.attached.resolve(workerSessions.get(current.id));
  const enabled = Promise.all([current.bootstrap.promise, current.attached.promise]).then(() => postInspector(current, 'Runtime.enable'));
  current.ready = Promise.all([enabled, current.context.promise]).then(([,context]) => context);
  kernel.on('message', message => {
    if (message.kind === 'ready') { current.bootstrap.resolve(); return; }
    if (message.kind === 'late_output' || message.kind === 'uncaught') {
      send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: message.kind === 'uncaught' ? 'error' : 'info', logger: 'node_repl',
        data: { requestId: message.requestId, requestMeta: message.requestMeta,
          content: message.kind === 'uncaught' ? [text(message.error)] : [message.item] } } });
      return;
    }
    const call = pending.get(message.id);
    if (!call || call.kernel !== current) return;
    if (message.kind === 'output') call.content.push(message.item);
    if (message.kind === 'prepared') {
      // Evaluation callbacks stay in the supervisor isolate. A Worker-local
      // Inspector Session can reenter JS inside V8's termination/disposal scope.
      postInspector(current, 'Runtime.evaluate', { expression: message.expression, contextId: call.context,
        replMode: true, awaitPromise: true, returnByValue: false }).then(result => {
        if (pending.get(message.id) !== call) return;
        const failure = result.exceptionDetails;
        call.error = failure ? failure.exception?.description || failure.text : null;
        kernel.postMessage({ kind: 'finished', id: message.id });
      }, error => {
        if (pending.get(message.id) !== call) return;
        call.error = error.stack || String(error);
        kernel.postMessage({ kind: 'finished', id: message.id });
      });
    }
    if (message.kind === 'complete' || message.kind === 'finished') {
      pending.delete(message.id);
      const error = message.kind === 'complete' ? message.error : call.error;
      call.resolve({ content: [...call.content, ...(error ? [text(error)] : [])], isError: Boolean(error) });
    }
  });
  kernel.on('error', error => { current.failure = error; logError(error); });
  kernel.once('exit', code => {
    const error = current.failure || new Error('Node kernel exited before execution completed (code ' + code + ')');
    current.bootstrap.reject(error);
    current.attached.reject(error);
    current.context.reject(error);
    for (const [id, command] of commands) if (command.kernel === current) { commands.delete(id); command.reject(error); }
    for (const [id, call] of pending) if (call.kernel === current) { pending.delete(id); call.reject(error); }
    if (worker === current) worker = undefined;
    workerSessions.delete(current.id);
    kernels.delete(current.id);
    current.exited.resolve(code);
  });
  kernel.stdout.on('data', chunk => send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: 'info', data: chunk.toString() } }));
  kernel.stderr.on('data', chunk => send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: 'warning', data: chunk.toString() } }));
  return current;
}
async function interruptKernel(previous) {
  await previous.ready;
  await postInspector(previous, 'Runtime.terminateExecution');
  await postInspector(previous, 'Runtime.disable');
  // Both Inspector replies precede the Worker's own exit turn. Reset completes
  // only after the native Worker exit event, before another kernel is admitted.
  previous.kernel.postMessage({ kind: 'retire' });
  const code = await previous.exited.promise;
  if (previous.failure || code !== 0) throw previous.failure || new Error('Node kernel retirement exited with code ' + code);
}
async function resetKernel(reason) {
  generation++;
  if (resetting) return resetting;
  const previous = worker; worker = undefined;
  const interrupted = [...pending.values()]; pending.clear();
  resetting = (async () => {
    try {
      if (previous) await interruptKernel(previous);
      for (const call of interrupted) call.resolve({ content: [...call.content, text(reason)], isError: true });
    } catch (error) { for (const call of interrupted) call.reject(error); throw error; }
  })();
  try { await resetting; } finally { resetting = undefined; }
}
async function execute(request, signal, expectedGeneration) {
  const params = request.params.arguments || {}, timeout = params.timeout_ms ?? 30000;
  if (typeof params.code !== 'string' || !Number.isSafeInteger(timeout) || timeout <= 0) return errorResult('js requires code and a positive integer timeout_ms');
  if (resetting) await resetting;
  if (expectedGeneration !== generation) return errorResult('Kernel was reset before this queued execution.');
  if (signal.aborted) return errorResult('JavaScript execution cancelled before start');
  const current = worker ||= createKernel(), id = ++sequence;
  let timer;
  const cancel = () => { void resetKernel('JavaScript execution cancelled; the kernel was reset.').catch(logError); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    return await new Promise((resolve, reject) => {
      // Register against this concrete kernel before awaiting its bootstrap;
      // an immediate reset must still own and settle the original request.
      const call = { kernel: current, resolve, reject, content: [] }; pending.set(id, call);
      current.ready.then(context => {
        if (pending.get(id) !== call) return;
        call.context = context;
        timer = setTimeout(() => { void resetKernel('JavaScript execution exceeded ' + timeout + ' ms; the kernel was reset.').catch(logError); }, timeout);
        current.kernel.postMessage({ id, requestId: request.id, code: params.code, meta: request.params._meta || {} });
      }, error => { if (pending.get(id) === call) { pending.delete(id); reject(error); } });
    });
  } finally { clearTimeout(timer); signal.removeEventListener('abort', cancel); }
}
const common = 'Use js for persistent Node JavaScript with top-level await and redeclarable bindings. Use dynamic import(); static imports and node:process are unavailable. nodeRepl.write(value) emits text; await nodeRepl.emitImage(dataUrl or {bytes,mimeType}) emits an image. nodeRepl.cwd, homeDir, tmpDir and requestMeta expose this execution context. This is a real Node kernel, separate from Code Mode.';
const tools = [
  { name: 'js', description: common + ' Default timeout_ms is 30000; timeout or cancellation resets the kernel.',
    inputSchema: { type: 'object', properties: { code: { type: 'string' }, timeout_ms: { type: 'integer', minimum: 1 }, title: { type: 'string' } }, required: ['code'], additionalProperties: false } },
  { name: 'js_reset', description: 'Reset the JavaScript kernel and clear all bindings. Added node_modules directories remain available.', inputSchema: { type: 'object', properties: {}, additionalProperties: false } },
  { name: 'js_add_node_module_dir', description: 'Add an absolute node_modules directory for package imports. The directory remains available after js_reset.', inputSchema: { type: 'object', properties: { path: { type: 'string' } }, required: ['path'], additionalProperties: false } },
].map(tool => ({ ...tool, annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true } }));
async function handle(request) {
  if (request.method === 'notifications/cancelled') { requests.get(request.params.requestId)?.abort(); return; }
  if (request.id === undefined) return;
  let result, cancelled = false;
  try {
    switch (request.method) {
      case 'initialize': result = { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'MiniCode local Node companion', version: '1' }, instructions: common }; break;
      case 'ping': result = {}; break;
      case 'tools/list': result = { tools }; break;
      case 'tools/call': {
        const name = request.params.name;
        if (name === 'js_reset') { await resetKernel('JavaScript kernel reset by js_reset.'); result = { content: [text('JavaScript kernel reset.')] }; break; }
        if (name === 'js_add_node_module_dir') {
          const dir = request.params.arguments?.path;
          if (typeof dir !== 'string' || !path.isAbsolute(dir) || !(await stat(dir)).isDirectory()) throw new Error('path must be an absolute node_modules directory');
          if (!moduleDirs.includes(dir)) { moduleDirs.push(dir); worker?.kernel.postMessage({ kind: 'add_module_dir', path: dir }); }
          result = { content: [text('Added node_modules directory: ' + dir)] }; break;
        }
        if (name !== 'js') { result = errorResult('Unknown Node companion tool: ' + name); break; }
        const controller = new AbortController(); requests.set(request.id, controller);
        const admittedGeneration = generation;
        const operation = queue.then(() => admittedGeneration === generation ? execute(request, controller.signal, admittedGeneration) : errorResult('Kernel was reset before this queued execution.'));
        queue = operation.then(() => {}, () => {});
        try { result = await operation; } finally { cancelled = controller.signal.aborted; requests.delete(request.id); }
        break;
      }
      default: send({ jsonrpc: '2.0', id: request.id, error: { code: -32601, message: 'Unsupported MCP method: ' + request.method } }); return;
    }
  } catch (error) { result = errorResult(error.stack || String(error)); }
  if (!cancelled) send({ jsonrpc: '2.0', id: request.id, result });
}
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on('line', line => { const request = JSON.parse(line); void handle(request); });
lines.on('close', () => { void resetKernel('Node companion closed.').then(() => process.exit(0)); });

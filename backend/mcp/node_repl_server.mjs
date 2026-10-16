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
const { Session } = require('node:inspector');
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
const session = new Session(); session.connect();
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
function prepare(code) {
  const tree = acorn.parse(code, { ecmaVersion: 'latest', sourceType: 'script', allowAwaitOutsideFunction: true });
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
  return code;
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
const contextReady = new Promise(resolve => session.on('Runtime.executionContextCreated', ({params}) => {
  if (params.context.name === 'MiniCode Node REPL') resolve(params.context.id);
}));
await new Promise((resolve,reject) => session.post('Runtime.enable', {}, error => error ? reject(error) : resolve()));
const globals = { nodeRepl: Object.freeze(nodeRepl), console: userConsole, __minicodeNodeImport: importModule };
for (const name of ['Buffer','URL','URLSearchParams','TextEncoder','TextDecoder','Blob','fetch','FormData','Headers','Request','Response',
    'AbortController','AbortSignal','setTimeout','clearTimeout','setInterval','clearInterval','setImmediate','clearImmediate',
    'queueMicrotask','structuredClone','atob','btoa','crypto']) globals[name] = globalThis[name];
const context = vm.createContext(globals, { name: 'MiniCode Node REPL' });
vm.runInContext('globalThis.global = globalThis', context);
const contextId = await contextReady;
parentPort.on('message', message => {
  if (message.kind === 'add_module_dir') { moduleDirs.push(message.path); return; }
  const call = { id: message.id, requestId: message.requestId, meta: message.meta || {}, completed: false };
  executions.run(call, async () => {
  try {
    const expression = prepare(message.code);
    const result = await new Promise((resolve,reject) => session.post('Runtime.evaluate', {
      expression, contextId, replMode: true, awaitPromise: true, returnByValue: false,
    }, (error,value) => error ? reject(error) : resolve(value)));
    const failure = result.exceptionDetails;
    parentPort.postMessage({ kind: 'complete', id: message.id,
      error: failure ? failure.exception?.description || failure.text : null });
  } catch (error) { parentPort.postMessage({ kind: 'complete', id: message.id, error: error.stack || String(error) }); }
  finally { call.completed = true; }
  });
});
parentPort.postMessage({ kind: 'ready' });
})().catch(error => { throw error; });
`;

const moduleDirs = [];
let worker, resetting, generation = 0, sequence = 0, queue = Promise.resolve();
const pending = new Map();
const requests = new Map();
const debuggerSession = new Session(); debuggerSession.connect();
const workerSessions = new Map(), workerAttachments = new Map(), interrupts = new Map();
let interruptSequence = 0;
debuggerSession.on('NodeWorker.attachedToWorker', ({params}) => {
  workerSessions.set(params.workerInfo.workerId, params.sessionId);
  workerAttachments.get(params.workerInfo.workerId)?.(params.sessionId);
});
debuggerSession.on('NodeWorker.receivedMessageFromWorker', ({params}) => {
  const message = JSON.parse(params.message);
  const complete = interrupts.get(message.id);
  if (complete) { interrupts.delete(message.id); complete(message); }
});
await new Promise((resolve,reject) => debuggerSession.post('NodeWorker.enable', {waitForDebuggerOnStart:false}, error => error ? reject(error) : resolve()));
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
const text = value => ({ type: 'text', text: value });
const errorResult = message => ({ content: [text(message)], isError: true });
function createKernel() {
  const kernel = new Worker(workerSource, { eval: true, execArgv: ['--expose-internals'], workerData: { moduleDirs }, stdout: true, stderr: true });
  const workerId = String(kernel.threadId);
  const attached = new Promise(resolve => workerSessions.has(workerId) ? resolve(workerSessions.get(workerId)) : workerAttachments.set(workerId, resolve));
  kernel.once('exit', () => { workerSessions.delete(workerId); workerAttachments.delete(workerId); });
  const ready = new Promise((resolve,reject) => {
    kernel.on('message', message => {
      if (message.kind === 'ready') { resolve(); return; }
      if (message.kind === 'late_output') {
        send({jsonrpc:'2.0',method:'notifications/message',params:{level:'info',logger:'node_repl',
          data:{requestId:message.requestId,requestMeta:message.requestMeta,content:[message.item]}}});
        return;
      }
      const call = pending.get(message.id);
      if (!call) return;
      if (message.kind === 'output') call.content.push(message.item);
      if (message.kind === 'complete') {
        pending.delete(message.id);
        call.resolve({ content: [...call.content, ...(message.error ? [text(message.error)] : [])], isError: Boolean(message.error) });
      }
    });
    kernel.on('error', error => { reject(error); for (const call of pending.values()) call.reject(error); pending.clear(); });
  });
  kernel.stdout.on('data', chunk => send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: 'info', data: chunk.toString() } }));
  kernel.stderr.on('data', chunk => send({ jsonrpc: '2.0', method: 'notifications/message', params: { level: 'warning', data: chunk.toString() } }));
  return { kernel, ready, attached };
}
async function interruptKernel(previous) {
  const sessionId = await previous.attached;
  const id = ++interruptSequence;
  const completed = new Promise((resolve,reject) => {
    interrupts.set(id, message => message.error ? reject(new Error(message.error.message)) : resolve());
    debuggerSession.post('NodeWorker.sendMessageToWorker', { sessionId,
      message: JSON.stringify({id,method:'Runtime.terminateExecution'}) }, error => {
      if (error) { interrupts.delete(id); reject(error); }
    });
  });
  await completed;
  await previous.kernel.terminate();
}
async function resetKernel(reason) {
  generation++;
  if (resetting) return resetting;
  const previous = worker; worker = undefined;
  const interrupted = [...pending.values()]; pending.clear();
  // Interrupt the inspector evaluation before disposing its isolate. Direct
  // Worker.terminate during an awaited import + CPU loop crashes Node 24/V8.
  resetting = (async () => {
    try {
      if (previous) await interruptKernel(previous);
      for (const call of interrupted) call.resolve({content:[...call.content,text(reason)],isError:true});
    } catch (error) { for (const call of interrupted) call.reject(error); throw error; }
  })();
  try { await resetting; } finally { resetting = undefined; }
}
async function execute(request, signal) {
  const params = request.params.arguments || {};
  const timeout = params.timeout_ms ?? 30000;
  if (typeof params.code !== 'string' || !Number.isSafeInteger(timeout) || timeout <= 0) return errorResult('js requires code and a positive integer timeout_ms');
  if (resetting) await resetting;
  worker ||= createKernel();
  await worker.ready;
  if (signal.aborted) return errorResult('JavaScript execution cancelled before start');
  const id = ++sequence;
  let timer;
  const cancel = () => { void resetKernel('JavaScript execution cancelled; the kernel was reset.'); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    return await new Promise((resolve,reject) => {
      pending.set(id, { resolve, reject, content: [] });
      timer = setTimeout(() => { void resetKernel('JavaScript execution exceeded ' + timeout + ' ms; the kernel was reset.'); }, timeout);
      worker.kernel.postMessage({ id, requestId: request.id, code: params.code, meta: request.params._meta || {} });
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
        const operation = queue.then(() => admittedGeneration === generation ? execute(request, controller.signal) : errorResult('Kernel was reset before this queued execution.'));
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

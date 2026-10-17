// Controlled CUA package boundary: real MCP/Node process, no desktop control.
import { createInterface } from 'node:readline';
import { appendFileSync } from 'node:fs';
import vm from 'node:vm';

const events = process.env.MINICODE_CUA_TEST_EVENTS;
const context = vm.createContext({});
const send = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
appendFileSync(events, JSON.stringify({ event: 'started', pid: process.pid }) + '\n');
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on('line', line => {
  const request = JSON.parse(line);
  if (request.id == null) return;
  if (request.method === 'initialize') {
    send(request.id, { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} },
      serverInfo: { name: 'controlled-cua-sdk-boundary', version: '1.0' } });
  } else if (request.method === 'tools/list') {
    send(request.id, { tools: [{ name: 'js', description: 'Observe a persistent fixture kernel without desktop actions',
      inputSchema: { type: 'object', properties: { code: { type: 'string' } }, required: ['code'] },
      annotations: { readOnlyHint: true } }] });
  } else if (request.method === 'tools/call') {
    appendFileSync(events, JSON.stringify({ event: 'call', pid: process.pid, meta: request.params._meta,
      arguments: request.params.arguments }) + '\n');
    const output = [];
    context.nodeRepl = { write: value => output.push(String(value)) };
    try {
      vm.runInContext(request.params.arguments.code, context);
      send(request.id, { content: output.map(text => ({ type: 'text', text })), isError: false });
    } catch (error) {
      send(request.id, { content: [{ type: 'text', text: error.message }], isError: true });
    }
  } else if (request.method === 'ping') {
    send(request.id, {});
  } else {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id,
      error: { code: -32601, message: 'Unsupported fixture method: ' + request.method } }) + '\n');
  }
});
lines.on('close', () => {
  appendFileSync(events, JSON.stringify({ event: 'closed', pid: process.pid }) + '\n');
});

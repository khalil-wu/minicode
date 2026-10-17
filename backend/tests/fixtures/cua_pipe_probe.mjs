import assert from 'node:assert/strict';
import net from 'node:net';
import os from 'node:os';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const [hostModule, transportModule, pipePath, eventsPath] = process.argv.slice(2);
const { createComputerUseHost } = await import(pathToFileURL(hostModule).href);
const host = await createComputerUseHost({ transportModulePath: transportModule,
  helperPath: eventsPath, codexCliPath: 'fixture-cli', codexHome: 'fixture-owned-home', pipePath });
async function client() {
  const socket = net.createConnection(pipePath);
  await new Promise((resolve, reject) => { socket.once('connect', resolve); socket.once('error', reject); });
  let sequence = 0, buffer = Buffer.alloc(0), onApproval;
  const pending = new Map();
  const send = message => {
    const bytes = Buffer.from(JSON.stringify(message)), frame = Buffer.alloc(bytes.length + 4);
    os.endianness() === 'LE' ? frame.writeUInt32LE(bytes.length, 0) : frame.writeUInt32BE(bytes.length, 0);
    bytes.copy(frame, 4);
    // Exercise the host's actual partial length-prefix and body consumption.
    socket.write(frame.subarray(0, 2)); socket.write(frame.subarray(2));
  };
  socket.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const length = os.endianness() === 'LE' ? buffer.readUInt32LE() : buffer.readUInt32BE();
      if (buffer.length < 4 + length) return;
      const message = JSON.parse(buffer.subarray(4, 4 + length)); buffer = buffer.subarray(4 + length);
      if (message.method === 'requestComputerUseApproval') { void onApproval(message); continue; }
      const promise = pending.get(message.id); pending.delete(message.id);
      message.error ? promise.reject(new Error(message.error.message)) : promise.resolve(message.result);
    }
  });
  return {
    send, socket, set approval(value) { onApproval = value; },
    request(method, params) {
      const id = ++sequence;
      return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); send({ id, jsonrpc: '2.0', method, params }); });
    },
  };
}
const one = await client(), two = await client();
const old = { session_id: 'same-thread', turn_id: 'old-turn' }, next = { session_id: 'same-thread', turn_id: 'next-turn' };
const invoke = (peer, method, metadata = old) => peer.request('request', { method, params: { actual: true }, codexTurnMetadata: metadata });
try {
  assert.equal(await one.request('ping'), 'pong');
  const observed = await invoke(one, 'observe');
  assert.deepEqual(observed.metadata, old);
  let approval;
  one.approval = async message => {
    approval = message;
    one.send({ id: message.id, jsonrpc: '2.0', result: { action: 'accept', content: { persist: 'always' } } });
  };
  const accepted = await invoke(one, 'approval');
  assert.equal(approval.method, 'requestComputerUseApproval');
  assert.match(approval.id, /^computer-use-approval:/);
  assert.equal(approval.params.meta.tool_params.app, 'fixture-app');
  assert.deepEqual(approval.params.meta.persist, ['session', 'always']);
  assert.equal(accepted.approval.content.persist, 'always');
  one.approval = async message => one.send({ id: message.id, jsonrpc: '2.0', result: { action: 'decline' } });
  await assert.rejects(invoke(one, 'approval'), /was not approved/);
  await assert.rejects(invoke(one, 'error'), /ACTUAL_SOURCE_PACKAGE_ERROR/);
  const slow = invoke(one, 'slow');
  await new Promise(resolve => setTimeout(resolve, 20));
  await assert.rejects(invoke(two, 'observe'), /already has an active request/);
  await slow;
  await invoke(one, 'observe', next);
  assert.equal(await host.closeActiveTurn({ sessionId: 'same-thread', turnId: 'old-turn' }), false);
  assert.equal(await host.closeActiveTurn({ sessionId: 'another-thread', turnId: 'next-turn' }), false);
  assert.equal(await host.closeActiveTurn({ sessionId: 'same-thread', turnId: 'next-turn' }), true);
  assert.equal(await host.closeActiveTurn({ sessionId: 'same-thread', turnId: 'next-turn' }), false);
  await invoke(two, 'observe', { session_id: 'second-thread', turn_id: 'owned' });
  assert.equal(await one.request('close'), null);
  const events = (await readFile(eventsPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.filter(event => event.method === 'closed').length, 2);
  assert.equal(events.filter(event => event.method === 'observe').length, 3);
  assert.deepEqual(events[0].metadata, old);
  await invoke(one, 'observe', next);
} finally { one.socket.destroy(); two.socket.destroy(); await host.dispose(); }
const settled = (await readFile(eventsPath, 'utf8')).trim().split('\n').map(JSON.parse);
assert.equal(settled.filter(event => event.method === 'closed').length, 3);
assert.equal(settled.at(-1).method, 'closed');
process.stdout.write(JSON.stringify({ partialFrames: true, approval: true, decline: true, packageError: true,
  globalSerialization: true, originalTurnOnly: true, originalThreadOnly: true, originalCloseDrained: true }) + '\n');

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { availablePort, externalPeerEnvironment } = require("./test-peer-env");

function oraclePage(workspace) {
  return `<!doctype html><meta charset="utf-8"><title>MiniCode external backend oracle</title><p>Real backend and native ownership verification</p><script>
  (async () => {
    const runtime = window.__MINICODE_RUNTIME__;
    const events = [], waiting = new Set();
    const result = { windowOwnerId:runtime.windowOwnerId, windowInstanceId:runtime.windowInstanceId,
      runtimeFields:Object.keys(runtime), nodeIntegrationBlocked:typeof window.require === 'undefined' };
    let socket;
    const report = async () => {
      await fetch('/oracle/report', { method:'POST', headers:{'content-type':'application/json', 'x-minicode-token':runtime.runtimeToken}, body:JSON.stringify(result) });
      await runtime.desktop.windowControls.close();
    };
    try {
      const token = btoa(runtime.runtimeToken).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
      const url = new URL('/ws', runtime.wsBaseUrl);
      url.searchParams.set('session_id', 'external-peer-session');
      url.searchParams.set('window_owner_id', runtime.windowOwnerId);
      url.searchParams.set('window_instance_id', runtime.windowInstanceId);
      socket = new WebSocket(url, ['minicode', 'minicode-token.' + token, 'minicode-window.' + runtime.windowBindingToken]);
      const waitEvent = predicate => {
        const prior = events.find(predicate);
        if (prior) return Promise.resolve(prior);
        return new Promise((resolve, reject) => {
          const item = { predicate, resolve, reject };
          item.timer = setTimeout(() => { waiting.delete(item); reject(new Error('WS event timed out: ' + events.map(event => event.type).join(','))); }, 25000);
          waiting.add(item);
        });
      };
      socket.addEventListener('message', message => {
        const event = JSON.parse(message.data); events.push(event);
        if (event.type === 'error') {
          for (const item of waiting) { clearTimeout(item.timer); item.reject(new Error(JSON.stringify(event))); }
          waiting.clear(); return;
        }
        for (const item of waiting) if (item.predicate(event)) {
          waiting.delete(item); clearTimeout(item.timer); item.resolve(event);
        }
      });
      await new Promise((resolve, reject) => {
        socket.addEventListener('open', resolve, {once:true});
        socket.addEventListener('error', () => reject(new Error('WebSocket admission failed')), {once:true});
        socket.addEventListener('close', event => reject(new Error('WebSocket closed during admission: ' + event.code)), {once:true});
      });
      result.acceptedProtocol = socket.protocol;
      const switched = waitEvent(event => event.type === 'conversation.switched');
      socket.send(JSON.stringify({ type:'conversation.create', conversation_id:'external-owned', title:'External owned task', workspace_root:${JSON.stringify(workspace)}, permission_mode:'confirm', memory_mode:'disabled' }));
      const created = await switched;
      result.conversationId = created.conversation_id;
      result.conversationPayloadId = created.conversation.id;
      await runtime.desktop.embeddedBrowser.create({ id:'external-tab', conversation_id:created.conversation_id, url:'about:blank' });
      const discovery = waitEvent(event => event.type === 'tool_result' && event.id === 'external-native-discovery');
      const tool = waitEvent(event => event.type === 'tool_result' && event.id === 'external-native-browser');
      const done = waitEvent(event => event.type === 'done');
      socket.send(JSON.stringify({ type:'user_message', content:'List the browser owned by this window.', conversation_id:created.conversation_id,
        workspace_root:${JSON.stringify(workspace)}, user_message_id:'external-user-message', assistant_message_id:'external-assistant-message' }));
      result.toolSearchResult = await discovery;
      result.toolResult = await tool;
      result.done = await done;
      const request = { window_owner_id:runtime.windowOwnerId, window_instance_id:runtime.windowInstanceId, client_id:'external-language',
        snapshot_revision:1, method:'hover', line:1, character:2,
        documents:[{document_id:'external-doc', model_version:1, path:'a.py', content:'value = 42\\nvalue\\n', language:'python'}] };
      const headers = { 'content-type':'application/json', 'x-minicode-token':runtime.runtimeToken };
      const languageUrl = '/api/workspace/language?workspace_root=' + encodeURIComponent(${JSON.stringify(workspace)});
      const foreign = await fetch(languageUrl, {method:'POST', headers:{...headers, 'x-minicode-window-binding-token':'0'.repeat(64)}, body:JSON.stringify(request)});
      result.foreignLanguageStatus = foreign.status;
      const language = await fetch(languageUrl, {method:'POST', headers:{...headers, 'x-minicode-window-binding-token':runtime.windowBindingToken}, body:JSON.stringify(request)});
      result.languageStatus = language.status;
      result.language = await language.json();
      result.languageClients = (await (await fetch('/oracle/language-state', {headers})).json()).clients;
      result.eventTypes = events.map(event => event.type);
      socket.close();
    } catch (error) {
      result.error = String(error); result.stack = error.stack;
      result.eventTypes = events.map(event => event.type);
      if (socket) socket.close();
    }
    await report();
  })();</script>`;
}

function captureOutput(child) {
  let value = "";
  child.stdout.on("data", chunk => { value += chunk; });
  child.stderr.on("data", chunk => { value += chunk; });
  return () => value;
}

async function waitUntil(read, predicate, message, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (predicate(value)) return value;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(message);
}

function waitExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve({code:child.exitCode, signal:child.signalCode});
  return new Promise((resolve, reject) => {
    const onExit = (code, signal) => { clearTimeout(timer); resolve({code, signal}); };
    const timer = setTimeout(() => { child.off("exit", onExit); reject(new Error(`Owned process ${child.pid} did not exit`)); }, timeoutMs);
    child.once("exit", onExit);
  });
}

test("external FastAPI attests its native host and runs real WS, browser and LSP ownership through desktop close", {timeout:90000}, async () => {
  const peer = await externalPeerEnvironment();
  const port = await availablePort();
  const origin = `http://127.0.0.1:${port}`;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-external-peer-e2e-"));
  const profile = path.join(root, "desktop"), state = path.join(root, "backend"), workspace = path.join(root, "workspace"), managed = path.join(root, "managed");
  for (const directory of [profile, path.join(state, "data"), workspace, managed]) fs.mkdirSync(directory, {recursive:true});
  fs.writeFileSync(path.join(workspace, "a.py"), "value = 41\nvalue\n", "utf8");
  fs.writeFileSync(path.join(state, "settings.json"), JSON.stringify({agent:{code_mode_only:false}, llm:{provider:"openai", openai:{base_url:"https://fixture.invalid/v1", model:"fixture", auth_header:false}}}), "utf8");
  fs.mkdirSync(path.join(state, ".minicode"));
  fs.writeFileSync(path.join(state, ".minicode", "models.json"), JSON.stringify({providers:{openai:{
    api:"openai-responses", base_url:"https://fixture.invalid/v1", auth_header:false,
    models:[{id:"fixture", name:"Native browser oracle", supports_search_tool:true}],
  }}}), "utf8");
  fs.writeFileSync(path.join(state, "data", "active_workspace.json"), JSON.stringify({root:workspace}), "utf8");
  fs.writeFileSync(path.join(state, "data", "trusted_workspaces.json"), JSON.stringify({roots:[workspace]}), "utf8");
  const html = path.join(root, "oracle.html");
  fs.writeFileSync(html, oraclePage(workspace), "utf8");
  const projectRoot = path.resolve(__dirname, "..");
  const runtimeToken = crypto.randomBytes(32).toString("hex");
  const env = {...process.env, ...peer.env, MINICODE_RUNTIME_TOKEN:runtimeToken, MINICODE_STATE_ROOT:state,
    MINICODE_SYSTEM_CONFIG_FILE:path.join(root, "absent-system.toml"), MINICODE_REQUIREMENTS_FILE:path.join(root, "absent-requirements.toml"),
    MINICODE_MANAGED_SETTINGS_DIR:managed, MINICODE_EDITOR_NODE:process.execPath,
    MINICODE_EDITOR_LANGUAGE_SERVICES_DIR:path.join(__dirname, "language-services"),
    MINICODE_ORACLE_BACKEND_PORT:String(port), MINICODE_ORACLE_FRONTEND_FILE:html,
    PYTHONPATH:[projectRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter), PYTHONUTF8:"1", PYTHONIOENCODING:"utf-8"};
  delete env.ELECTRON_RUN_AS_NODE;
  const backend = spawn(process.env.MINICODE_PYTHON || (process.platform === "win32" ? "python" : "python3"),
    ["-u", path.join(__dirname, "fixtures", "external_backend_oracle.py")], {cwd:workspace, env, windowsHide:true, stdio:["ignore", "pipe", "pipe"]});
  const backendOutput = captureOutput(backend);
  let desktop, desktopOutput = () => "";
  const request = async resource => {
    const response = await fetch(origin + resource, {headers:{"x-minicode-token":runtimeToken}, signal:AbortSignal.timeout(5000)});
    assert.equal(response.status, 200, await response.clone().text());
    return response.json();
  };
  try {
    const readiness = await waitUntil(async () => {
      if (backend.exitCode !== null) throw new Error(`External backend exited with ${backend.exitCode}`);
      try { return await request("/readyz"); }
      catch (error) { if (error.cause?.code === "ECONNREFUSED") return null; throw error; }
    }, value => value?.ready, "External backend did not become ready", 25000);
    assert.equal(readiness.desktop_bridge_fingerprint, peer.fingerprint);
    assert.equal(JSON.stringify(readiness).includes(peer.env.MINICODE_EMBEDDED_BROWSER_TOKEN), false);
    const electronArgs = ["--disable-gpu"];
    if (process.platform !== "win32") electronArgs.push("--no-sandbox");
    electronArgs.push(__dirname);
    desktop = spawn(require("electron"), electronArgs, {cwd:__dirname, env:{...env, MINICODE_SKIP_BACKEND:"1", MINICODE_USER_DATA_DIR:profile,
      MINICODE_API_BASE_URL:origin, MINICODE_WS_BASE_URL:origin.replace("http:", "ws:"), MINICODE_FRONTEND_URL:origin + "/oracle",
      MINICODE_BACKEND_STARTUP_TIMEOUT_MS:"15000", MINICODE_DISABLE_HARDWARE_ACCELERATION:"1"}, windowsHide:true, stdio:["ignore", "pipe", "pipe"]});
    desktopOutput = captureOutput(desktop);
    const observed = await waitUntil(() => request("/oracle/report"), value => value.report !== null, "External native oracle did not report", 45000);
    process.stdout.write(`[external-native-evidence] ${JSON.stringify(observed)}\n`);
    const report = observed.report;
    assert.equal(report.error, undefined, JSON.stringify(report));
    assert.equal(report.nodeIntegrationBlocked, true);
    assert.equal(report.windowOwnerId, "primary");
    assert.match(report.windowInstanceId, /^[0-9a-f-]{36}$/);
    assert.equal(report.acceptedProtocol, "minicode");
    assert.equal(report.conversationId, report.conversationPayloadId);
    assert.ok(report.conversationId);
    assert.equal(report.toolSearchResult.is_error, false);
    assert.match(report.toolSearchResult.summary, /browser_control/);
    assert.equal(report.toolResult.is_error, false);
    assert.match(report.toolResult.summary, /external-tab/);
    assert.match(report.toolResult.summary, /about:blank/);
    assert.equal(report.done.status, "completed");
    assert.equal(report.foreignLanguageStatus, 403);
    assert.equal(report.languageStatus, 200, JSON.stringify(report.language));
    assert.match(JSON.stringify(report.language.result), /42/);
    assert.deepEqual(report.languageClients.map(client => [client.window_instance_id, client.client_id]), [[report.windowInstanceId, "external-language"]]);
    assert.ok(report.languageClients[0].pid > 0);
    assert.equal(report.done.provider_raw.loop_metrics.provider_call_count, 3);
    assert.equal(observed.models.length, 1);
    assert.equal(observed.models[0].saw_browser_result, true);
    assert.equal(observed.models[0].host_secret_leaked, false);
    assert.equal(report.runtimeFields.some(key => /embeddedBrowserToken|desktopLifecycleToken|bridgeToken/i.test(key)), false);
    assert.deepEqual(await waitExit(desktop, 10000), {code:0, signal:null});
    assert.equal(backend.exitCode, null, "Closing the desktop must preserve its externally owned backend process");
    assert.deepEqual((await request("/oracle/language-state")).clients, []);
    assert.equal((await request("/readyz")).ready, true);
    const desktopLog = fs.readFileSync(path.join(profile, "desktop.log"), "utf8");
    assert.doesNotMatch(desktopLog, /cleanup failed|startup attempt failed/);
    assert.equal(desktopLog.includes(peer.env.MINICODE_EMBEDDED_BROWSER_TOKEN), false);
    process.stdout.write(`External FastAPI + production Electron main: native WS admission, model-visible browser result, real Pyright PID ${report.languageClients[0].pid}, foreign-proof rejection and close-time LSP release passed.\n`);
  } catch (error) {
    error.message += `\n--- external backend ---\n${backendOutput()}\n--- Electron ---\n${desktopOutput()}`;
    const desktopLog = path.join(profile, "desktop.log");
    if (fs.existsSync(desktopLog)) error.message += `\n--- desktop log ---\n${fs.readFileSync(desktopLog, "utf8")}`;
    throw new Error(error.message, {cause:error});
  } finally {
    if (desktop && desktop.exitCode === null && desktop.signalCode === null) { desktop.kill(); await waitExit(desktop, 5000); }
    if (backend.exitCode === null && backend.signalCode === null) {
      try {
        await fetch(origin + "/api/runtime/shutdown", {method:"POST", headers:{"x-minicode-token":runtimeToken}, signal:AbortSignal.timeout(5000)});
        await waitExit(backend, 10000);
      } catch {
        backend.kill(); await waitExit(backend, 5000);
      }
    }
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()));
    fs.rmSync(root, {recursive:true, force:true});
  }
});

"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const test = require("node:test");

const APP_PATH = process.env.MINICODE_PACKAGED_APP_PATH
  ? path.resolve(process.env.MINICODE_PACKAGED_APP_PATH)
  : path.join(__dirname, "release", "win-unpacked", "MiniCode.exe");

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode, timedOut: false });
  }
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off("exit", onExit);
      resolve({ code: child.exitCode, signal: child.signalCode, timedOut: true });
    }, timeoutMs);
    const onExit = (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, timedOut: false });
    };
    child.once("exit", onExit);
  });
}

async function allocateLoopbackPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  if (!port) throw new Error("Failed to allocate a loopback port.");
  return port;
}

async function waitForJson(url, predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (response.ok) {
        const payload = await response.json();
        if (predicate(payload)) return payload;
      } else {
        lastError = new Error(`${label} returned HTTP ${response.status}.`);
      }
    } catch (error) {
      lastError = error;
    }
    await delay(200);
  }
  throw new Error(`${label} did not become ready: ${lastError?.message || "timeout"}`);
}

async function waitForPortClosed(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const open = await new Promise((resolve) => {
      const socket = net.createConnection({ host: "127.0.0.1", port });
      const settle = (value) => {
        socket.removeAllListeners();
        socket.destroy();
        resolve(value);
      };
      socket.setTimeout(500, () => settle(false));
      socket.once("connect", () => settle(true));
      socket.once("error", () => settle(false));
    });
    if (!open) return;
    await delay(150);
  }
  throw new Error(`Loopback port ${port} remained open after application exit.`);
}

function createCdpClient(webSocketUrl) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketUrl);
    const pending = new Map();
    let nextId = 1;
    const openingTimer = setTimeout(() => {
      socket.close();
      reject(new Error("CDP WebSocket connection timed out."));
    }, 5000);

    const closeWithError = (error) => {
      for (const request of pending.values()) {
        clearTimeout(request.timer);
        request.reject(error);
      }
      pending.clear();
    };

    socket.addEventListener("error", () => closeWithError(new Error("CDP WebSocket failed.")));
    socket.addEventListener("close", () => closeWithError(new Error("CDP WebSocket closed.")));
    socket.addEventListener("message", async (event) => {
      try {
        const raw = typeof event.data === "string" ? event.data : await event.data.text();
        const message = JSON.parse(raw);
        const request = pending.get(message.id);
        if (!request) return;
        pending.delete(message.id);
        clearTimeout(request.timer);
        if (message.error) {
          request.reject(new Error(message.error.message || "CDP command failed."));
        } else {
          request.resolve(message.result);
        }
      } catch (error) {
        closeWithError(error instanceof Error ? error : new Error(String(error)));
      }
    });
    socket.addEventListener("open", () => {
      clearTimeout(openingTimer);
      resolve({
        call(method, params = {}, timeoutMs = 10000) {
          return new Promise((resolveCall, rejectCall) => {
            const id = nextId++;
            const timer = setTimeout(() => {
              pending.delete(id);
              rejectCall(new Error(`${method} timed out.`));
            }, timeoutMs);
            pending.set(id, { resolve: resolveCall, reject: rejectCall, timer });
            socket.send(JSON.stringify({ id, method, params }));
          });
        },
        close() {
          socket.close();
        },
        send(method, params = {}) {
          socket.send(JSON.stringify({ id: nextId++, method, params }));
        },
      });
    }, { once: true });
  });
}

async function evaluate(client, expression, timeoutMs = 15000) {
  const response = await client.call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, timeoutMs);
  if (response?.exceptionDetails) {
    const description = response.exceptionDetails.exception?.description
      || response.exceptionDetails.text
      || "Runtime evaluation failed.";
    throw new Error(description);
  }
  return response?.result?.value;
}

async function waitForRendererReady(client, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      // Poll from the host: a Promise in the startup document is destroyed
      // when Electron commits the application document.
      const state = await evaluate(client, `({
        ready: document.readyState === "complete" && Boolean(window.__MINICODE_RUNTIME__),
        readyState: document.readyState,
        hasRuntime: Boolean(window.__MINICODE_RUNTIME__),
        href: location.href,
      })`);
      if (state.ready) return state;
    } catch (error) {
      if (error.message !== "Execution context was destroyed.") throw error;
    }
    await delay(50);
  }
  throw new Error("Packaged renderer did not finish loading its runtime.");
}

function listDescendantProcesses(rootPid) {
  const script = [
    "$ErrorActionPreference = 'Stop'",
    "$all = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine)",
    `$queue = [System.Collections.Generic.Queue[uint32]]::new(); $queue.Enqueue([uint32]${rootPid})`,
    "$seen = [System.Collections.Generic.HashSet[uint32]]::new()",
    "$result = [System.Collections.Generic.List[object]]::new()",
    "while ($queue.Count -gt 0) {",
    "  $parent = $queue.Dequeue()",
    "  foreach ($item in $all | Where-Object { $_.ParentProcessId -eq $parent }) {",
    "    if ($seen.Add([uint32]$item.ProcessId)) { $result.Add($item); $queue.Enqueue([uint32]$item.ProcessId) }",
    "  }",
    "}",
    "$result | ConvertTo-Json -Compress",
  ].join("\n");
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
    encoding: "utf8",
    windowsHide: true,
    timeout: 15000,
  });
  if (result.status !== 0) {
    throw new Error(`Failed to inspect packaged process tree: ${String(result.stderr || "").trim()}`);
  }
  const raw = String(result.stdout || "").trim();
  if (!raw) return [];
  const parsed = JSON.parse(raw);
  return Array.isArray(parsed) ? parsed : [parsed];
}

function isProcessAlive(pid) {
  const result = spawnSync("powershell.exe", [
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    `if (Get-Process -Id ${pid} -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }`,
  ], { windowsHide: true, timeout: 5000 });
  return result.status === 0;
}

function terminateProcessTree(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return;
  spawnSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
    windowsHide: true,
    timeout: 10000,
    stdio: "ignore",
  });
}

function scrubRuntimeToken(output, runtimeToken) {
  return String(output || "").split(runtimeToken).join("<redacted-runtime-token>");
}

test("packaged Windows app boots renderer, preload, IPC, and managed Python sidecar", { timeout: 90000 }, async () => {
  assert.equal(process.platform, "win32", "The packaged smoke currently targets the Windows release artifact.");
  assert.equal(fs.existsSync(APP_PATH), true, `Packaged executable is missing: ${APP_PATH}`);

  const backendPort = await allocateLoopbackPort();
  let cdpPort = await allocateLoopbackPort();
  while (cdpPort === backendPort) cdpPort = await allocateLoopbackPort();
  const runtimeToken = crypto.randomBytes(32).toString("base64url");
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-packaged-smoke-"));
  const nestedFile = path.join(userDataDir, "记得日记", "remember-diary", "src", "backup", "backupService.native.ts");
  fs.mkdirSync(path.dirname(nestedFile), { recursive: true });
  fs.writeFileSync(nestedFile, "export const backupFormat = 'JSON';\n", "utf8");
  const wave = Buffer.alloc(1644);
  wave.write("RIFF"); wave.writeUInt32LE(1636, 4); wave.write("WAVEfmt ", 8);
  wave.writeUInt32LE(16, 16); wave.writeUInt16LE(1, 20); wave.writeUInt16LE(1, 22);
  wave.writeUInt32LE(8000, 24); wave.writeUInt32LE(16000, 28);
  wave.writeUInt16LE(2, 32); wave.writeUInt16LE(16, 34); wave.write("data", 36); wave.writeUInt32LE(1600, 40);
  let providerCalls = 0;
  let firstUserInput = null;
  const provider = http.createServer(async (request, response) => {
    if (request.method === "GET") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ data: [{ id: "smoke-model", object: "model" }] }));
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const mainRequest = payload.tools?.some(tool => tool.function?.name === "tool_exec") === true;
    if (mainRequest) {
      providerCalls += 1;
      if (providerCalls === 1) firstUserInput = payload.messages.filter(message => message.role === "user").at(-1).content;
    }
    const toolReply = mainRequest && providerCalls === 1;
    const delta = toolReply ? { tool_calls: [{ index: 0, id: "audio-code", type: "function",
      function: { name: "tool_exec", arguments: JSON.stringify({ code: `audio({data:${JSON.stringify(wave.toString("base64"))},media_type:"audio/wav"});` }) } }] }
      : { content: "The audio fixture is ready for playback." };
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end(`data: ${JSON.stringify({ choices: [{ delta, finish_reason: toolReply ? "tool_calls" : "stop" }] })}\n\ndata: [DONE]\n\n`);
  });
  await new Promise(resolve => provider.listen(0, "127.0.0.1", resolve));
  const childEnv = { ...process.env };
  for (const name of [
    "ELECTRON_RUN_AS_NODE",
    "MINICODE_API_BASE_URL",
    "MINICODE_FRONTEND_URL",
    "MINICODE_SKIP_BACKEND",
    "MINICODE_WS_BASE_URL",
  ]) {
    delete childEnv[name];
  }
  Object.assign(childEnv, {
    ELECTRON_ENABLE_LOGGING: "1",
    MINICODE_BACKEND_PORT: String(backendPort),
    MINICODE_BACKEND_STARTUP_TIMEOUT_MS: "45000",
    MINICODE_BROWSER_DEBUG_PORT: String(cdpPort),
    MINICODE_DISABLE_HARDWARE_ACCELERATION: "1",
    MINICODE_ENABLE_EMBEDDED_BROWSER_CDP: "1",
    MINICODE_RUNTIME_TOKEN: runtimeToken,
    MINICODE_USER_DATA_DIR: userDataDir,
    LLM_PROVIDER: "custom",
    CUSTOM_API_KEY: "packaged-smoke-placeholder-no-generation",
    CUSTOM_BASE_URL: `http://127.0.0.1:${provider.address().port}/v1`,
    CUSTOM_MODEL: "smoke-model",
    CUSTOM_WIRE_API: "chat",
  });

  const child = spawn(APP_PATH, [], {
    cwd: userDataDir,
    env: childEnv,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

  let cdp = null;
  let pythonProcess = null;
  try {
    const health = await waitForJson(
      `http://127.0.0.1:${backendPort}/health`,
      (payload) => payload?.ready === true && ["ok", "degraded"].includes(payload.status),
      50000,
      "Packaged backend health endpoint",
    );
    assert.equal(health.ready, true);

    for (const name of ["browser", "code-review", "verify", "skill-creator", "plugin-creator"]) {
      const skillPath = path.join(path.dirname(APP_PATH), "resources", "skills", name, "SKILL.md");
      assert.equal(fs.existsSync(skillPath), true, `Bundled skill is missing: ${name}`);
      const assetUrl = new URL(`http://127.0.0.1:${backendPort}/api/skills/asset`);
      assetUrl.searchParams.set("skill_path", skillPath);
      const asset = await fetch(assetUrl, {
        headers: { "x-minicode-token": runtimeToken },
        signal: AbortSignal.timeout(5000),
      });
      assert.equal(asset.status, 200, `Bundled skill icon was not discovered: ${name}`);
      assert.match(asset.headers.get("content-type"), /^image\//);
      assert.ok((await asset.arrayBuffer()).byteLength > 0);
    }

    const targets = await waitForJson(
      `http://127.0.0.1:${cdpPort}/json/list`,
      (payload) => Array.isArray(payload) && payload.some((target) => target.type === "page" && target.url.startsWith("file:") && target.webSocketDebuggerUrl),
      30000,
      "Packaged renderer CDP endpoint",
    );
    const target = targets.find((candidate) => candidate.type === "page" && candidate.url.startsWith("file:") && candidate.webSocketDebuggerUrl);
    assert.ok(target);
    cdp = await createCdpClient(target.webSocketDebuggerUrl);

    const rendererReady = await waitForRendererReady(cdp, 30000);
    assert.equal(rendererReady.ready, true, `Packaged renderer did not become ready: ${JSON.stringify(rendererReady)}`);

    const probe = await evaluate(cdp, `
      (async () => {
        const runtime = window.__MINICODE_RUNTIME__;
        const diagnostics = await runtime?.desktop?.diagnostics?.export?.();
        return {
          readyState: document.readyState,
          protocol: location.protocol,
          hasRuntime: Boolean(runtime),
          isDesktop: runtime?.desktop?.platformInfo?.isDesktop === true,
          nodeIntegrationBlocked: typeof window.require === "undefined",
          apiBaseUrl: runtime?.apiBaseUrl || "",
          wsBaseUrl: runtime?.wsBaseUrl || "",
          tokenLength: typeof runtime?.runtimeToken === "string" ? runtime.runtimeToken.length : 0,
          diagnosticsOk: Boolean(diagnostics?.path),
          diagnosticsHasElectron: Boolean(diagnostics?.payload?.runtime?.electron),
        };
      })()
    `);
    assert.ok(["interactive", "complete"].includes(probe.readyState));
    assert.equal(probe.protocol, "file:");
    assert.equal(probe.hasRuntime, true);
    assert.equal(probe.isDesktop, true);
    assert.equal(probe.nodeIntegrationBlocked, true);
    assert.equal(probe.apiBaseUrl, `http://127.0.0.1:${backendPort}`);
    assert.equal(probe.wsBaseUrl, `ws://127.0.0.1:${backendPort}`);
    assert.equal(probe.tokenLength, runtimeToken.length);
    assert.equal(probe.diagnosticsOk, true);
    assert.equal(probe.diagnosticsHasElectron, true);

    const nestedRead = await evaluate(cdp, `window.__MINICODE_RUNTIME__.desktop.fs.readFile(${JSON.stringify(nestedFile)})`);
    assert.equal(nestedRead.content, "export const backupFormat = 'JSON';\n");

    // Exercise the shipped websocket handler as well as native filesystem IPC.
    // Both projects are confined to this smoke test's temporary workspace.
    const alpha = path.join(userDataDir, "记得日记");
    const beta = path.join(userDataDir, "另一个项目");
    fs.mkdirSync(beta);
    await evaluate(cdp, `Promise.all([${JSON.stringify(alpha)}, ${JSON.stringify(beta)}].map(root => window.__MINICODE_RUNTIME__.desktop.trustWorkspace(root)))`);
    const events = [];
    const socket = new WebSocket(`ws://127.0.0.1:${backendPort}/ws?session_id=packaged-project-test`, ["minicode", `minicode-token.${Buffer.from(runtimeToken).toString("base64url")}`]);
    socket.addEventListener("message", event => events.push(JSON.parse(event.data)));
    try {
      await new Promise((resolve, reject) => {
        socket.addEventListener("open", resolve, { once: true });
        socket.addEventListener("error", reject, { once: true });
      });
      const command = payload => new Promise((resolve, reject) => {
        const id = crypto.randomUUID();
        const timer = setTimeout(() => {
          socket.removeEventListener("message", receive);
          reject(new Error(`Timed out waiting for ${payload.type}: ${events.filter(event => event.type === "error").at(-1)?.message || "no backend error"}`));
        }, 15000);
        const receive = event => {
          const value = JSON.parse(event.data);
          if (value.type !== "command.result" || value.data?.client_command_id !== id) return;
          clearTimeout(timer);
          socket.removeEventListener("message", receive);
          resolve(value);
        };
        socket.addEventListener("message", receive);
        socket.send(JSON.stringify({ ...payload, client_command_id: id }));
      });
      const first = await command({ type: "workspace.set", path: alpha });
      assert.equal(first.level, "success", first.message);
      const second = await command({ type: "workspace.set", path: beta });
      assert.equal(second.level, "success", second.message);
      assert.notEqual(first.data.conversation_id, second.data.conversation_id);
      const inventory = events.filter(event => event.type === "conversation.list").at(-1).conversations;
      assert.equal(inventory.find(item => item.id === first.data.conversation_id).workspace_root, alpha);
      assert.equal(inventory.find(item => item.id === second.data.conversation_id).workspace_root, beta);
      const archived = await command({ type: "conversation.archive", conversation_id: first.data.conversation_id, archived: true });
      assert.equal(archived.level, "success", archived.message);
      const projects = events.filter(event => event.type === "workspace.recent.list").at(-1).projects;
      assert.ok(projects.some(project => project.path === alpha));
      const savedProjects = JSON.parse(fs.readFileSync(path.join(userDataDir, "data", "recent_projects.json"), "utf8"));
      assert.ok(savedProjects.some(project => project.path === alpha), "Archiving the last task removed its saved project.");

      const audioConversation = second.data.conversation_id;
      const memoryMode = await command({ type: "conversation.memory_mode.set", conversation_id: audioConversation, memory_mode: "disabled" });
      assert.equal(memoryMode.data.memory_mode, "disabled");
      const completion = new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`Audio query did not complete: ${JSON.stringify(events.slice(-4))}`)), 30000);
        const receive = event => {
          const value = JSON.parse(event.data);
          if (value.type !== "done" || value.conversation_id !== audioConversation) return;
          clearTimeout(timer); socket.removeEventListener("message", receive); resolve(value);
        };
        socket.addEventListener("message", receive);
      });
      socket.send(JSON.stringify({ type: "user_message", content: "Create the audio playback fixture.", conversation_id: audioConversation }));
      const done = await completion;
      assert.equal(done.status, "completed", JSON.stringify(done));
      const audio = events.find(event => event.type === "artifact.preview" && event.media_type === "audio/wav");
      assert.ok(audio, "Selected code audio did not produce a preview artifact.");
      assert.equal(providerCalls, 2);
      assert.ok(Array.isArray(firstUserInput), "Runtime context and the real request were flattened into one text block.");
      assert.equal(firstUserInput.at(-1).text, "Create the audio playback fixture.");
      const switched = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("Audio conversation did not reload")), 10000);
        const receive = event => {
          const value = JSON.parse(event.data);
          if (value.type !== "conversation.switched" || value.conversation_id !== audioConversation) return;
          clearTimeout(timer); socket.removeEventListener("message", receive); resolve(value);
        };
        socket.addEventListener("message", receive);
        socket.send(JSON.stringify({ type: "conversation.switch", conversation_id: audioConversation }));
      });
      const restored = switched.conversation;
      assert.ok(restored.transcript.some(message => message.artifacts?.some(artifact => artifact.artifactId === audio.artifact_id)),
        "Audio artifact was not retained in the conversation transcript.");
      await command({ type: "read_artifact", artifact_id: audio.artifact_id, conversation_id: audioConversation });
      const content = events.filter(event => event.type === "artifact_content").at(-1);
      assert.equal(content.content, "");
      assert.equal(content.url, undefined);

      await evaluate(cdp, `(() => {
        const store = window.__zustandStore;
        store.getState().ensureCodeLayout();
        store.setState({ previewOwnerConversationId: ${JSON.stringify(audioConversation)}, rightPanelOpen: true });
        store.getState().setConversationPreviewArtifact(${JSON.stringify(audioConversation)}, {
          artifactId: ${JSON.stringify(audio.artifact_id)}, name: "audio-smoke.wav", content: "", mediaType: "audio/wav", source: "artifact"
        });
        store.getState().setRightStackTab("preview");
      })()`);
      const playback = await evaluate(cdp, `(async () => {
        const deadline = Date.now() + 10000;
        while (!document.querySelector("audio") && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
        const audio = document.querySelector("audio");
        if (!audio) throw new Error("Audio player was not rendered");
        if (audio.error) throw new Error("Audio decode failed: " + audio.error.code);
        if (audio.readyState < 1) await new Promise((resolve, reject) => {
          audio.addEventListener("loadedmetadata", resolve, { once: true });
          audio.addEventListener("error", () => reject(new Error("Audio decode failed: " + audio.error?.code)), { once: true });
        });
        audio.muted = true; await audio.play(); audio.pause(); audio.currentTime = 0.05;
        return { duration: audio.duration, currentTime: audio.currentTime, controls: audio.controls, source: audio.src, error: audio.error?.code || null };
      })()`);
      assert.equal(playback.controls, true);
      assert.equal(playback.error, null);
      assert.ok(playback.duration > 0 && playback.currentTime > 0);
      assert.ok(playback.source.includes("/api/artifacts/raw"));
    } finally {
      socket.close();
    }

    const descendants = listDescendantProcesses(child.pid);
    pythonProcess = descendants.find((processInfo) => {
      const executable = String(processInfo.ExecutablePath || "").replace(/\\/g, "/").toLowerCase();
      return executable.endsWith("/resources/python-runtime/python.exe");
    });
    assert.ok(pythonProcess, "The packaged app did not own an embedded Python sidecar process.");

    // Closing the window also closes CDP; process exit is the acknowledgement.
    cdp.send("Runtime.evaluate", { expression: "window.__MINICODE_RUNTIME__.desktop.windowControls.close()" });
    const exit = await waitForExit(child, 15000);
    assert.equal(exit.timedOut, false, "Packaged Electron main process did not exit after closing its window.");
    assert.equal(exit.code, 0);
    cdp.close();
    cdp = null;
    await waitForPortClosed(backendPort, 10000);
    await waitForPortClosed(cdpPort, 10000);
    assert.equal(isProcessAlive(Number(pythonProcess.ProcessId)), false, "Embedded Python sidecar survived Electron shutdown.");
  } catch (error) {
    const details = [
      error instanceof Error ? error.message : String(error),
      `appPid=${child.pid} exitCode=${child.exitCode} signalCode=${child.signalCode}`,
      `backendPort=${backendPort} cdpPort=${cdpPort}`,
      "--- packaged stdout ---",
      scrubRuntimeToken(stdout, runtimeToken),
      "--- packaged stderr ---",
      scrubRuntimeToken(stderr, runtimeToken),
    ].join("\n");
    throw new Error(details, { cause: error });
  } finally {
    cdp?.close();
    const exit = await waitForExit(child, 2000);
    if (exit.timedOut) {
      terminateProcessTree(child.pid);
      await waitForExit(child, 5000);
    }
    if (pythonProcess?.ProcessId && isProcessAlive(Number(pythonProcess.ProcessId))) {
      terminateProcessTree(Number(pythonProcess.ProcessId));
    }
    fs.rmSync(userDataDir, { recursive: true, force: true });
    await new Promise(resolve => provider.close(resolve));
  }
});

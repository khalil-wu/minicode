"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
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
  return evaluate(client, `
    new Promise((resolve) => {
      const deadline = Date.now() + ${timeoutMs};
      const poll = () => {
        const readyState = document.readyState;
        const hasRuntime = Boolean(window.__MINICODE_RUNTIME__);
        if (["interactive", "complete"].includes(readyState) && hasRuntime) {
          resolve({ ready: true, readyState, hasRuntime, href: location.href });
          return;
        }
        if (Date.now() >= deadline) {
          resolve({ ready: false, readyState, hasRuntime, href: location.href });
          return;
        }
        setTimeout(poll, 50);
      };
      poll();
    })
  `, timeoutMs + 5000);
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
  });

  const child = spawn(APP_PATH, [], {
    cwd: path.dirname(APP_PATH),
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

    const targets = await waitForJson(
      `http://127.0.0.1:${cdpPort}/json/list`,
      (payload) => Array.isArray(payload) && payload.some((target) => target.type === "page" && target.webSocketDebuggerUrl),
      30000,
      "Packaged renderer CDP endpoint",
    );
    const target = targets.find((candidate) => candidate.type === "page" && candidate.webSocketDebuggerUrl);
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

    const descendants = listDescendantProcesses(child.pid);
    pythonProcess = descendants.find((processInfo) => {
      const executable = String(processInfo.ExecutablePath || "").replace(/\\/g, "/").toLowerCase();
      return executable.endsWith("/resources/python-runtime/python.exe");
    });
    assert.ok(pythonProcess, "The packaged app did not own an embedded Python sidecar process.");

    const closeScheduled = await evaluate(cdp, `
      (() => {
        setTimeout(() => {
          window.__MINICODE_RUNTIME__.desktop.windowControls.close().catch(() => undefined);
        }, 0);
        return true;
      })()
    `, 10000);
    assert.equal(closeScheduled, true);
    cdp.close();
    cdp = null;

    const exit = await waitForExit(child, 15000);
    assert.equal(exit.timedOut, false, "Packaged Electron main process did not exit after closing its window.");
    assert.equal(exit.code, 0);
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
  }
});

"use strict";

const { spawn } = require("node:child_process");
const { StringDecoder } = require("node:string_decoder");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let backendProcess = null;
let backendManagedByApp = false;
let backendStopRequested = false;
let backendRestartAttempt = 0;
let backendRestartTimer = null;
let backendLaunchPromise = null;

// Dependencies injected via init()
let writeStdout = () => {};
let writeStderr = () => {};
let getAppRoot = () => process.cwd();
let sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let spawnProcess = spawn;
let restartBackend = null;

// Configuration injected via init()
let config = {};

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  if (typeof deps.writeStdout === "function") writeStdout = deps.writeStdout;
  if (typeof deps.writeStderr === "function") writeStderr = deps.writeStderr;
  if (typeof deps.getAppRoot === "function") getAppRoot = deps.getAppRoot;
  if (typeof deps.sleep === "function") sleep = deps.sleep;
  if (typeof deps.spawnProcess === "function") spawnProcess = deps.spawnProcess;
  if (typeof deps.restartBackend === "function") restartBackend = deps.restartBackend;
  config = deps.config || {};
}

// ---------------------------------------------------------------------------
// Restart scheduling (exponential back-off with jitter)
// ---------------------------------------------------------------------------

function clearBackendRestartTimer() {
  if (!backendRestartTimer) return;
  clearTimeout(backendRestartTimer);
  backendRestartTimer = null;
}

function nextBackendRestartDelayMs() {
  const {
    restartInitialDelayMs = 800,
    restartMaxDelayMs = 20000,
    restartJitterRatio = 0.15,
  } = config;

  const exponent = Math.min(backendRestartAttempt, 8);
  const baseDelay = Math.min(
    restartInitialDelayMs * 2 ** exponent,
    restartMaxDelayMs,
  );
  backendRestartAttempt += 1;
  const jitter = baseDelay * restartJitterRatio * (Math.random() * 2 - 1);
  return Math.max(100, Math.floor(baseDelay + jitter));
}

function scheduleBackendRestart(reason) {
  const { manageBackend = true } = config;
  if (!manageBackend || backendStopRequested || backendProcess || backendRestartTimer) {
    return;
  }

  const delay = nextBackendRestartDelayMs();
  writeStderr(`[backend] scheduling restart in ${delay}ms (${reason})\n`);
  backendRestartTimer = setTimeout(() => {
    backendRestartTimer = null;
    const restart = restartBackend || (() => startBackendSidecar());
    Promise.resolve()
      .then(() => restart(reason))
      .catch((error) => {
        writeStderr(`[backend] restart failed: ${error.message}\n`);
        scheduleBackendRestart("restart attempt failed");
      });
  }, delay);
}

// ---------------------------------------------------------------------------
// Start / Stop
// ---------------------------------------------------------------------------

function startBackendSidecar() {
  const { manageBackend = true } = config;
  if (!manageBackend || backendStopRequested) return null;
  if (backendProcess) return backendProcess;

  clearBackendRestartTimer();

  const {
    pythonCommand = "python3",
    backendHost = "127.0.0.1",
    resolvedBackendPort = 8000,
    resolvedApiBaseUrl = "",
    resolvedWsBaseUrl = "",
    resolvedFrontendUrl = "",
    runtimeToken = "",
    stateRoot = "",
    desktopDir = "",
    documentsDir = "",
    downloadsDir = "",
  } = config;

  const child = spawnProcess(pythonCommand, ["-m", "backend"], {
    cwd: getAppRoot(),
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      PYTHONDONTWRITEBYTECODE: "1",
      MINICODE_BACKEND_HOST: backendHost,
      MINICODE_BACKEND_PORT: String(resolvedBackendPort),
      MINICODE_API_BASE_URL: resolvedApiBaseUrl,
      MINICODE_WS_BASE_URL: resolvedWsBaseUrl,
      MINICODE_FRONTEND_URL: resolvedFrontendUrl,
      MINICODE_RUNTIME_TOKEN: runtimeToken,
      MINICODE_STATE_ROOT: stateRoot,
      MINICODE_DESKTOP_DIR: desktopDir,
      MINICODE_DOCUMENTS_DIR: documentsDir,
      MINICODE_DOWNLOADS_DIR: downloadsDir,
      PYTHONPATH: [getAppRoot(), process.env.PYTHONPATH].filter(Boolean).join(require("node:path").delimiter),
    },
    windowsHide: true,
  });

  backendProcess = child;
  backendManagedByApp = true;

  const stdoutDecoder = new StringDecoder("utf8");
  const stderrDecoder = new StringDecoder("utf8");
  const writeDecoded = (decoder, writer, chunk) => {
    const text = decoder.write(chunk);
    if (text) writer(`[backend] ${text}`);
  };
  const flushDecoded = (decoder, writer) => {
    const text = decoder.end();
    if (text) writer(`[backend] ${text}`);
  };

  child.stdout?.on("data", (chunk) => {
    writeDecoded(stdoutDecoder, writeStdout, chunk);
  });
  child.stdout?.on("end", () => flushDecoded(stdoutDecoder, writeStdout));

  child.stderr?.on("data", (chunk) => {
    writeDecoded(stderrDecoder, writeStderr, chunk);
  });
  child.stderr?.on("end", () => flushDecoded(stderrDecoder, writeStderr));

  child.on("exit", (code, signal) => {
    const reason = signal ? `signal=${signal}` : `code=${code}`;
    writeStderr(`[backend] exited (${reason})\n`);
    if (backendProcess !== child) return;
    const shouldRestart = backendManagedByApp && !backendStopRequested;
    backendProcess = null;
    backendManagedByApp = false;
    if (shouldRestart) {
      scheduleBackendRestart(`process exited with ${reason}`);
    }
  });

  child.on("error", (error) => {
    writeStderr(`[backend] failed to start: ${error.message}\n`);
    if (backendProcess !== child) return;
    const shouldRestart = backendManagedByApp && !backendStopRequested;
    backendProcess = null;
    backendManagedByApp = false;
    if (shouldRestart) scheduleBackendRestart("spawn error");
  });

  return child;
}

function childHasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitForChildExit(child, timeoutMs) {
  if (childHasExited(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let timer = null;
    const finish = (exited) => {
      if (timer !== null) clearTimeout(timer);
      child.removeListener("exit", onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    child.once("exit", onExit);
    timer = setTimeout(() => finish(false), timeoutMs);
    if (childHasExited(child)) finish(true);
  });
}

async function terminateProcessTree(child) {
  if (childHasExited(child)) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      spawnProcess("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true })
        .once("exit", resolve)
        .once("error", resolve);
    });
    return;
  }
  child.kill("SIGTERM");
}

async function retireBackendSidecar(child, reason = "readiness failure") {
  if (backendProcess !== child || !backendManagedByApp) return false;
  writeStderr(`[backend] retiring unready process (${reason})\n`);
  await terminateProcessTree(child);
  if (!await waitForChildExit(child, 3000) && !childHasExited(child)) {
    child.kill("SIGKILL");
    await waitForChildExit(child, 1000);
  }
  if (backendProcess === child) {
    throw new Error("Backend process did not exit after retirement.");
  }
  return true;
}

async function stopBackendSidecar() {
  backendStopRequested = true;
  clearBackendRestartTimer();

  if (!backendProcess || !backendManagedByApp) return;

  const child = backendProcess;
  const { resolvedApiBaseUrl = "", runtimeToken = "" } = config;
  try {
    if (resolvedApiBaseUrl) {
      await fetch(`${resolvedApiBaseUrl}/api/runtime/shutdown`, {
        method: "POST",
        headers: runtimeToken ? { "x-minicode-token": runtimeToken } : {},
        signal: AbortSignal.timeout(2000),
      });
    }
    if (!await waitForChildExit(child, 5000)) {
      await terminateProcessTree(child);
    }
  } catch (error) {
    writeStderr(`[backend] failed to stop: ${error.message}\n`);
    await terminateProcessTree(child);
  }
  if (!await waitForChildExit(child, 3000) && !childHasExited(child)) {
    child.kill("SIGKILL");
    await waitForChildExit(child, 1000);
  }
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

async function waitForBackendReady(apiBaseUrl, timeoutMs = 30000, { signal } = {}) {
  const deadline = Date.now() + timeoutMs;
  // `/readyz` is the canonical MiniCode readiness contract.  A desktop may
  // also attach to an already-running compatible backend from an older build
  // which only exposes `/health`; accept that explicit legacy endpoint when
  // the canonical route is absent, while retaining the same readiness loop.
  const readinessUrl = `${apiBaseUrl}/readyz`;
  const legacyHealthUrl = `${apiBaseUrl}/health`;
  let endpointUrl = readinessUrl;

  while (Date.now() < deadline) {
    if (signal?.aborted) {
      throw signal.reason instanceof Error
        ? signal.reason
        : new Error(`Backend readiness check aborted: ${readinessUrl}`);
    }
    const remainingMs = Math.max(1, deadline - Date.now());
    const deadlineSignal = AbortSignal.timeout(remainingMs);
    const requestSignal = signal
      ? AbortSignal.any([signal, deadlineSignal])
      : deadlineSignal;
    try {
      const response = await fetch(endpointUrl, { signal: requestSignal });
      if (response.ok) {
        backendRestartAttempt = 0;
        return;
      }
      if (endpointUrl === readinessUrl && response.status === 404) {
        endpointUrl = legacyHealthUrl;
      }
    } catch (error) {
      if (signal?.aborted) {
        throw signal.reason instanceof Error ? signal.reason : error;
      }
    }
    const retryDelayMs = Math.min(400, Math.max(0, deadline - Date.now()));
    if (retryDelayMs > 0) await sleep(retryDelayMs);
  }

  throw new Error(`Backend readiness check timed out: ${endpointUrl}`);
}

async function waitForOwnedBackendReady(child, runtime, timeoutMs) {
  const controller = new AbortController();
  const abortForExit = () => {
    controller.abort(new Error("Backend sidecar exited before becoming ready."));
  };
  child.once("exit", abortForExit);
  child.once("error", abortForExit);
  if (childHasExited(child)) abortForExit();
  try {
    await waitForBackendReady(runtime.apiBaseUrl, timeoutMs, {
      signal: controller.signal,
    });
    if (backendProcess !== child) {
      throw new Error("Backend sidecar changed while readiness was being checked.");
    }
  } finally {
    child.removeListener("exit", abortForExit);
    child.removeListener("error", abortForExit);
  }
}

async function launchBackendSidecar({ resolveRuntime, getRuntime, timeoutMs = 30000 }) {
  if (backendLaunchPromise) return backendLaunchPromise;

  const launch = (async () => {
    let child = backendProcess;
    const runtime = child ? getRuntime() : await resolveRuntime();
    child = child || startBackendSidecar();
    if (!child) {
      throw new Error("Backend sidecar did not start.");
    }
    try {
      await waitForOwnedBackendReady(child, runtime, timeoutMs);
      return runtime;
    } catch (error) {
      await retireBackendSidecar(child, "readiness check failed");
      throw error;
    }
  })();

  backendLaunchPromise = launch;
  try {
    return await launch;
  } finally {
    if (backendLaunchPromise === launch) {
      backendLaunchPromise = null;
    }
  }
}

// ---------------------------------------------------------------------------
// Accessors
// ---------------------------------------------------------------------------

function getBackendProcess() {
  return backendProcess;
}

function isBackendManagedByApp() {
  return backendManagedByApp;
}

function getBackendRestartAttempt() {
  return backendRestartAttempt;
}

function resetStopRequested() {
  backendStopRequested = false;
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  startBackendSidecar,
  retireBackendSidecar,
  stopBackendSidecar,
  waitForBackendReady,
  launchBackendSidecar,
  getBackendProcess,
  isBackendManagedByApp,
  getBackendRestartAttempt,
  resetStopRequested,
  clearBackendRestartTimer,
};

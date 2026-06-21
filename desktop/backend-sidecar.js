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

// Dependencies injected via init()
let writeStdout = () => {};
let writeStderr = () => {};
let getAppRoot = () => process.cwd();
let sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let onStart = null;

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
  if (typeof deps.onStart === "function") onStart = deps.onStart;
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
    startBackendSidecar();
  }, delay);
}

// ---------------------------------------------------------------------------
// Start / Stop
// ---------------------------------------------------------------------------

function startBackendSidecar() {
  const { manageBackend = true } = config;
  if (!manageBackend || backendStopRequested || backendProcess) return;

  clearBackendRestartTimer();

  const {
    pythonCommand = "python3",
    backendHost = "127.0.0.1",
    resolvedBackendPort = 8000,
    resolvedApiBaseUrl = "",
    resolvedWsBaseUrl = "",
    resolvedFrontendUrl = "",
    runtimeToken = "",
  } = config;

  const child = spawn(pythonCommand, ["-m", "backend"], {
    cwd: getAppRoot(),
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      MINICODE_BACKEND_HOST: backendHost,
      MINICODE_BACKEND_PORT: String(resolvedBackendPort),
      MINICODE_API_BASE_URL: resolvedApiBaseUrl,
      MINICODE_WS_BASE_URL: resolvedWsBaseUrl,
      MINICODE_FRONTEND_URL: resolvedFrontendUrl,
      MINICODE_RUNTIME_TOKEN: runtimeToken,
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
    const shouldRestart = backendManagedByApp && !backendStopRequested;
    backendProcess = null;
    backendManagedByApp = false;
    if (shouldRestart) {
      scheduleBackendRestart(`process exited with ${reason}`);
    }
  });

  child.on("error", (error) => {
    writeStderr(`[backend] failed to start: ${error.message}\n`);
    backendProcess = null;
    backendManagedByApp = false;
    scheduleBackendRestart("spawn error");
  });
}

function stopBackendSidecar() {
  backendStopRequested = true;
  clearBackendRestartTimer();

  if (!backendProcess || !backendManagedByApp) return;

  try {
    backendProcess.kill();
  } catch (error) {
    writeStderr(`[backend] failed to stop: ${error.message}\n`);
  }
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

async function waitForBackendReady(apiBaseUrl, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  const healthUrl = `${apiBaseUrl}/health`;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(healthUrl);
      if (response.ok) {
        backendRestartAttempt = 0;
        return;
      }
    } catch {
      // Retry until timeout.
    }
    await sleep(400);
  }

  throw new Error(`Backend health check timed out: ${healthUrl}`);
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
  stopBackendSidecar,
  waitForBackendReady,
  getBackendProcess,
  isBackendManagedByApp,
  getBackendRestartAttempt,
  resetStopRequested,
  clearBackendRestartTimer,
};

"use strict";

const path = require("node:path");
const desktopPreferences = require("./preferences");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const ptySessions = new Map();
let ptyIdCounter = 1;
let ptyCleanupDone = false;

// Bounded per-session scrollback so a renderer refresh/reconnect can restore
// history (the renderer's xterm buffer is lost on reload). Kept in the main
// process and exposed via pty.list / pty.snapshot.
const PTY_SCROLLBACK_MAX_CHARS = 200000;
const PTY_SNAPSHOT_DEFAULT_CHARS = 100000;
const PTY_LIST_PREVIEW_CHARS = 8000;
const PTY_EXITED_SESSION_TTL_MS = 30 * 60 * 1000;
const PTY_EXITED_SESSION_MAX = 24;
const PTY_WRITE_CHUNK_CHARS = 8192;
const PTY_WRITE_MAX_CHARS = 1024 * 1024;
const PTY_KILL_EXIT_TIMEOUT_MS = 3000;
const PTY_FORCE_KILL_EXIT_TIMEOUT_MS = 1000;
const PTY_LIVE_SESSION_MAX = Math.max(
  1,
  Math.min(Number(process.env.MINICODE_PTY_MAX_LIVE_SESSIONS) || 8, 32),
);

// Dependencies injected via init()
let pty = null;
let sanitizedPtyEnv = () => ({});
let appendDesktopLog = () => {};
let getWindowByInstanceId = () => null;
let assertTrustedPath = null;
let killExitTimeoutMs = PTY_KILL_EXIT_TIMEOUT_MS;
let forceKillExitTimeoutMs = PTY_FORCE_KILL_EXIT_TIMEOUT_MS;

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  if (deps.pty !== undefined) pty = deps.pty;
  if (typeof deps.sanitizedPtyEnv === "function") sanitizedPtyEnv = deps.sanitizedPtyEnv;
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
  if (typeof deps.getWindowByInstanceId === "function") getWindowByInstanceId = deps.getWindowByInstanceId;
  if (typeof deps.assertTrustedPath === "function") assertTrustedPath = deps.assertTrustedPath;
  killExitTimeoutMs = Number.isFinite(deps.killExitTimeoutMs)
    ? Math.max(1, Math.floor(deps.killExitTimeoutMs))
    : PTY_KILL_EXIT_TIMEOUT_MS;
  forceKillExitTimeoutMs = Number.isFinite(deps.forceKillExitTimeoutMs)
    ? Math.max(1, Math.floor(deps.forceKillExitTimeoutMs))
    : PTY_FORCE_KILL_EXIT_TIMEOUT_MS;
}

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------

function getSendTarget(session) {
  const win = session.window;
  if (win && !win.isDestroyed()) {
    return win.webContents;
  }
  return null;
}

function normalizeConversationId(conversationId) {
  return typeof conversationId === "string" ? conversationId.trim() : "";
}

function requireConversationId(conversationId) {
  const normalized = normalizeConversationId(conversationId);
  if (!normalized) {
    throw new Error("Terminal conversation owner is required.");
  }
  return normalized;
}

function isOwnedBy(session, conversationId, windowInstanceId) {
  const owner = normalizeConversationId(conversationId);
  return Boolean(owner && session && session.conversationId === owner && session.windowInstanceId === windowInstanceId);
}

function appendSessionOutput(sessionId, session, data, emit = true) {
  const text = typeof data === "string" ? data : String(data);
  if (!text) return;
  const startCursor = session.outputCursor;
  session.outputCursor += text.length;
  session.scrollback += text;
  if (session.scrollback.length > PTY_SCROLLBACK_MAX_CHARS) {
    session.scrollback = session.scrollback.slice(session.scrollback.length - PTY_SCROLLBACK_MAX_CHARS);
  }
  if (!emit) return;
  const wc = getSendTarget(session);
  if (wc) {
    wc.send("minicode:pty:data", {
      sessionId,
      conversationId: session.conversationId,
      data: text,
      startCursor,
      endCursor: session.outputCursor,
    });
  }
}

function pruneExitedSessions(now = Date.now()) {
  const exited = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    if (session.isAlive !== false) continue;
    if (!Number.isFinite(session.exitedAt) || now - session.exitedAt > PTY_EXITED_SESSION_TTL_MS) {
      ptySessions.delete(sessionId);
      continue;
    }
    exited.push([sessionId, session]);
  }
  exited.sort((a, b) => (b[1].exitedAt || 0) - (a[1].exitedAt || 0));
  for (const [sessionId] of exited.slice(PTY_EXITED_SESSION_MAX)) {
    ptySessions.delete(sessionId);
  }
}

function resolveTerminalCwd(cwd) {
  if (cwd === undefined || cwd === null || cwd === "") {
    return path.resolve(process.cwd());
  }
  if (typeof cwd !== "string" || !cwd.trim()) {
    throw new Error("Terminal cwd must be a path string.");
  }
  if (typeof assertTrustedPath === "function") {
    return assertTrustedPath(cwd, "Terminal cwd");
  }
  throw new Error("Terminal cwd validation is unavailable.");
}

function normalizeTerminalSize(cols, rows) {
  const normalizedCols = Math.max(20, Math.min(Number(cols) || 80, 500));
  const normalizedRows = Math.max(5, Math.min(Number(rows) || 24, 200));
  return {
    cols: Math.floor(normalizedCols),
    rows: Math.floor(normalizedRows),
  };
}

function spawnSession(cwd, conversationId, windowInstanceId) {
  if (ptyCleanupDone) {
    throw new Error("Terminal manager is shutting down.");
  }
  pruneExitedSessions();
  const ownerConversationId = requireConversationId(conversationId);
  const window = getWindowByInstanceId(windowInstanceId);
  if (!window || window.isDestroyed()) throw new Error("Terminal window owner is unavailable.");
  const liveSessionCount = Array.from(ptySessions.values()).filter((session) => session.isAlive !== false).length;
  if (liveSessionCount >= PTY_LIVE_SESSION_MAX) {
    throw new Error(`Terminal session limit reached (${PTY_LIVE_SESSION_MAX}). Close a terminal before opening another.`);
  }
  const target = desktopPreferences.resolveTerminalShell();
  const shellStr = target.executable;
  const shellArgs = target.args;
  const resolvedCwd = resolveTerminalCwd(cwd);

  // Source owns the Windows PTY; node-pty owns Unix PTYs. A missing native
  // owner is an actual startup failure, never a substituted pipe terminal.
  if (!pty) {
    appendDesktopLog("[desktop] terminal spawn failed: native PTY owner unavailable");
    throw new Error(
      "Terminal unavailable: the native PTY owner is unavailable. Reinstall MiniCode desktop or run scripts/verify-pty-runtime.js.",
    );
  }
  let ptyProcess;
  try {
    ptyProcess = pty.spawn(shellStr, shellArgs, {
      name: "xterm-256color",
      cols: 80,
      rows: 24,
      cwd: resolvedCwd,
      env: sanitizedPtyEnv(),
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    appendDesktopLog(`[desktop] native PTY spawn failed: ${err instanceof Error ? err.stack || err.message : String(err)}`);
    throw new Error(`Terminal spawn failed: ${detail}`);
  }

  const sessionId = `term_${ptyIdCounter++}`;
  let resolveExit;
  const exitPromise = new Promise((resolve) => {
    resolveExit = resolve;
  });
  const session = {
    process: ptyProcess,
    conversationId: ownerConversationId,
    windowInstanceId,
    window,
    cwd: resolvedCwd,
    shell: shellStr,
    scrollback: "",
    outputCursor: 0,
    isAlive: true,
    exitCode: null,
    exitSignal: null,
    exitedAt: null,
    exitPromise,
    resolveExit,
    exitSettled: false,
    killPromise: null,
  };
  ptySessions.set(sessionId, session);

  ptyProcess.onData((data) => {
    if (session.isAlive) appendSessionOutput(sessionId, session, data);
  });

  ptyProcess.onExit(({ exitCode, signal }) => {
    if (ptySessions.get(sessionId) !== session || session.exitSettled) return;
    // Never normalize a signal/unknown termination into a fake code 0.
    const hasRealExitCode = Number.isFinite(exitCode);
    const exitLabel = hasRealExitCode
      ? `code ${exitCode}`
      : `signal ${signal || "unknown"}`;
    appendSessionOutput(sessionId, session, `\r\n[Process exited with ${exitLabel}]\r\n`);
    session.isAlive = false;
    session.exitCode = hasRealExitCode ? exitCode : null;
    session.exitSignal = hasRealExitCode ? null : (signal || "unknown");
    session.exitedAt = Date.now();
    session.exitSettled = true;
    session.resolveExit();
    const wc = getSendTarget(session);
    if (wc) {
      wc.send("minicode:pty:exit", {
        sessionId,
        conversationId: session.conversationId,
        exitCode: session.exitCode,
        exitSignal: session.exitSignal,
        exitedAt: session.exitedAt,
      });
    }
    pruneExitedSessions();
  });

  return {
    session_id: sessionId,
    conversation_id: ownerConversationId,
    pid: ptyProcess.pid,
    shell: shellStr,
    cwd: resolvedCwd,
  };
}

function writeToSession(sessionId, data, conversationId, windowInstanceId) {
  if (typeof sessionId !== "string" || typeof data !== "string") {
    return false;
  }
  if (data.length > PTY_WRITE_MAX_CHARS || data.includes("\0")) {
    appendDesktopLog(`[desktop] rejected invalid pty write for ${sessionId}`);
    return false;
  }
  const session = ptySessions.get(sessionId);
  if (session?.isAlive && isOwnedBy(session, conversationId, windowInstanceId)) {
    for (let offset = 0; offset < data.length; offset += PTY_WRITE_CHUNK_CHARS) {
      if (session.process.write(data.slice(offset, offset + PTY_WRITE_CHUNK_CHARS)) === false) return false;
    }
    return true;
  }
  return false;
}

function resizeSession(sessionId, cols, rows, conversationId, windowInstanceId) {
  const session = ptySessions.get(sessionId);
  if (session?.isAlive && isOwnedBy(session, conversationId, windowInstanceId)) {
    try {
      const size = normalizeTerminalSize(cols, rows);
      session.process.resize(size.cols, size.rows);
    } catch (e) {
      // noop
    }
  }
}

function killUnixProcessGroup(pid, fallback, signal = "SIGTERM") {
  if (!Number.isFinite(pid) || pid <= 0) {
    try { fallback(); } catch {}
    return Promise.resolve();
  }
  try { process.kill(-pid, signal); } catch { try { fallback(); } catch {} }
  return Promise.resolve();
}

async function waitForSessionExit(session, timeoutMs) {
  if (session.exitSettled || session.isAlive === false) return true;
  let timeout;
  const timedOut = new Promise((resolve) => {
    timeout = setTimeout(() => resolve(false), timeoutMs);
  });
  const exited = await Promise.race([
    session.exitPromise.then(() => true),
    timedOut,
  ]);
  clearTimeout(timeout);
  return exited;
}

async function terminateSession(sessionId, session) {
  if (session.exitSettled || session.isAlive === false) return true;
  if (!session.killPromise) {
    session.killPromise = (async () => {
      if (process.platform === "win32") {
        session.process.kill();
      } else {
        await killUnixProcessGroup(session.process.pid, () => session.process.kill());
      }
      if (await waitForSessionExit(session, killExitTimeoutMs)) return true;

      appendDesktopLog(`[desktop] pty ${sessionId} did not exit after termination; forcing kill`);
      if (process.platform === "win32") session.process.forceKill();
      // node-pty's plain kill() defaults to SIGHUP on the leader process. A
      // force kill must SIGKILL the whole process group a shell that ignores
      // TERM/HUP left behind (Source pty.rs kills the captured group).
      else await killUnixProcessGroup(session.process.pid, () => session.process.kill("SIGKILL"), "SIGKILL");
      if (!await waitForSessionExit(session, forceKillExitTimeoutMs)) {
        appendDesktopLog(`[desktop] pty ${sessionId} did not report exit after forced kill`);
        return false;
      }
      return true;
    })();
  }
  try {
    return await session.killPromise;
  } finally {
    session.killPromise = null;
  }
}

async function killSession(sessionId, conversationId, windowInstanceId) {
  const session = ptySessions.get(sessionId);
  if (isOwnedBy(session, conversationId, windowInstanceId)) {
    if (!await terminateSession(sessionId, session)) return false;
    if (ptySessions.get(sessionId) === session) ptySessions.delete(sessionId);
    return true;
  }
  return false;
}

async function restartSession(sessionId, conversationId, windowInstanceId) {
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId, windowInstanceId)) return null;
  const cwd = session.cwd;
  appendDesktopLog(`[desktop] restarting pty ${sessionId} for ${conversationId}`);
  if (!await killSession(sessionId, conversationId, windowInstanceId)) return null;
  try {
    const replacement = spawnSession(cwd, conversationId, windowInstanceId);
    appendDesktopLog(`[desktop] restarted pty ${sessionId} as ${replacement.session_id}`);
    return replacement;
  } catch (error) {
    appendDesktopLog(`[desktop] pty restart failed for ${sessionId}: ${error.message}`);
    throw error;
  }
}

async function killConversation(conversationId, preserveHistory = false, windowInstanceId) {
  const owner = requireConversationId(conversationId);
  const ownedSessionIds = Array.from(ptySessions.entries())
    .filter(([, session]) => isOwnedBy(session, owner, windowInstanceId))
    .map(([sessionId]) => sessionId);
  const results = await Promise.all(
    ownedSessionIds.map((sessionId) => preserveHistory
      ? terminateSession(sessionId, ptySessions.get(sessionId))
      : killSession(sessionId, owner, windowInstanceId)),
  );
  return results.filter(Boolean).length;
}

function acknowledgeExitedSession(sessionId, conversationId, windowInstanceId) {
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId, windowInstanceId) || session.isAlive !== false) return false;
  ptySessions.delete(sessionId);
  return true;
}

function _snapshotEntry(sessionId, session, maxChars) {
  const limit = Math.max(1, Math.min(Number(maxChars) || PTY_SNAPSHOT_DEFAULT_CHARS, PTY_SCROLLBACK_MAX_CHARS));
  const full = session.scrollback || "";
  const truncated = full.length > limit;
  const output = truncated ? full.slice(full.length - limit) : full;
  const outputEndCursor = Number.isFinite(session.outputCursor) ? session.outputCursor : full.length;
  return {
    session_id: sessionId,
    conversation_id: session.conversationId,
    pid: session.process.pid,
    shell: session.shell || session.process.process || "shell",
    cwd: session.cwd,
    output,
    output_chars: output.length,
    total_output_chars: outputEndCursor,
    output_start_cursor: outputEndCursor - output.length,
    output_end_cursor: outputEndCursor,
    truncated,
    is_alive: session.isAlive !== false,
    exit_code: session.exitCode,
    exit_signal: session.exitSignal ?? null,
    exited_at: session.exitedAt,
  };
}

function snapshotSession(sessionId, maxChars, conversationId, windowInstanceId) {
  pruneExitedSessions();
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId, windowInstanceId)) {
    return null;
  }
  return _snapshotEntry(sessionId, session, maxChars);
}

function clearSession(sessionId, conversationId, windowInstanceId) {
  pruneExitedSessions();
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId, windowInstanceId)) {
    return { cleared: false, outputCursor: 0 };
  }
  // Preserve the monotonic output cursor so the renderer can reject a data
  // event emitted before this clear but delivered after the invoke resolves.
  session.scrollback = "";
  return {
    cleared: true,
    outputCursor: Number.isFinite(session.outputCursor) ? session.outputCursor : 0,
  };
}

function listSessions(conversationId, maxChars, windowInstanceId) {
  pruneExitedSessions();
  const owner = normalizeConversationId(conversationId);
  if (!owner) return [];
  const list = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    if (!isOwnedBy(session, owner, windowInstanceId)) continue;
    list.push(_snapshotEntry(sessionId, session, maxChars || PTY_LIST_PREVIEW_CHARS));
  }
  return list;
}

function listActiveSessions() {
  pruneExitedSessions();
  const list = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    if (session.isAlive === false) continue;
    list.push({
      session_id: sessionId,
      conversation_id: session.conversationId,
      window_instance_id: session.windowInstanceId,
    });
  }
  return list.sort((left, right) => left.session_id.localeCompare(right.session_id));
}

async function killAllSessions() {
  if (ptyCleanupDone) return;
  const terminations = Array.from(ptySessions.entries()).map(([sessionId, session]) =>
    killSession(sessionId, session.conversationId, session.windowInstanceId),
  );
  const results = await Promise.all(terminations);
  if (results.some((terminated) => !terminated)) throw new Error("Terminal cleanup did not finish for all owned sessions.");
  ptyCleanupDone = true;
}

async function releaseWindow(windowInstanceId) {
  const owned = Array.from(ptySessions.entries()).filter(([, session]) => session.windowInstanceId === windowInstanceId);
  const results = await Promise.all(owned.map(([sessionId, session]) => killSession(sessionId, session.conversationId, windowInstanceId)));
  if (results.some((terminated) => !terminated)) throw new Error(`Terminal cleanup did not finish for window ${windowInstanceId}.`);
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  spawnSession,
  writeToSession,
  resizeSession,
  killSession,
  restartSession,
  killConversation,
  listSessions,
  listActiveSessions,
  snapshotSession,
  clearSession,
  acknowledgeExitedSession,
  pruneExitedSessions,
  killAllSessions,
  releaseWindow,
  resolveTerminalCwd,
  normalizeTerminalSize,
};

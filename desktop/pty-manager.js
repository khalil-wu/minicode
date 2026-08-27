"use strict";

const path = require("node:path");
const { spawn } = require("node:child_process");

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
const PTY_LIVE_SESSION_MAX = Math.max(
  1,
  Math.min(Number(process.env.MINICODE_PTY_MAX_LIVE_SESSIONS) || 8, 32),
);

// Dependencies injected via init()
let pty = null;
let sanitizedPtyEnv = () => ({});
let appendDesktopLog = () => {};
let getMainWindow = () => null;
let assertTrustedPath = null;

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  if (deps.pty !== undefined) pty = deps.pty;
  if (typeof deps.sanitizedPtyEnv === "function") sanitizedPtyEnv = deps.sanitizedPtyEnv;
  if (typeof deps.appendDesktopLog === "function") appendDesktopLog = deps.appendDesktopLog;
  if (typeof deps.getMainWindow === "function") getMainWindow = deps.getMainWindow;
  if (typeof deps.assertTrustedPath === "function") assertTrustedPath = deps.assertTrustedPath;
}

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------

function getSendTarget() {
  const win = getMainWindow();
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

function isOwnedBy(session, conversationId) {
  const owner = normalizeConversationId(conversationId);
  return Boolean(owner && session && session.conversationId === owner);
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
  const wc = getSendTarget();
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

function windowsPowerShellArgs() {
  const initCommand = [
    "$__minicodeUtf8 = [System.Text.UTF8Encoding]::new($false)",
    "[Console]::InputEncoding = $__minicodeUtf8",
    "[Console]::OutputEncoding = $__minicodeUtf8",
    "chcp 65001 | Out-Null",
    "Clear-Host",
  ].join("; ");
  return ["-NoLogo", "-NoProfile", "-NoExit", "-Command", initCommand];
}

function spawnSession(cwd, conversationId) {
  if (ptyCleanupDone) {
    throw new Error("Terminal manager is shutting down.");
  }
  pruneExitedSessions();
  const ownerConversationId = requireConversationId(conversationId);
  const liveSessionCount = Array.from(ptySessions.values()).filter((session) => session.isAlive !== false).length;
  if (liveSessionCount >= PTY_LIVE_SESSION_MAX) {
    throw new Error(`Terminal session limit reached (${PTY_LIVE_SESSION_MAX}). Close a terminal before opening another.`);
  }
  const shellStr = process.platform === "win32" ? "powershell.exe" : (process.env.SHELL || "bash");
  const shellArgs = process.platform === "win32" ? windowsPowerShellArgs() : [];
  const resolvedCwd = resolveTerminalCwd(cwd);

  // Single authoritative execution path: a real PTY.  There is deliberately
  // no child_process fallback — a missing/broken node-pty runtime is an
  // explicit startup failure surfaced to the renderer, never a silently
  // degraded pipe session with different semantics.
  if (!pty) {
    appendDesktopLog("[desktop] terminal spawn failed: node-pty module unavailable");
    throw new Error(
      "Terminal unavailable: the node-pty runtime module failed to load. Reinstall MiniCode desktop or run scripts/verify-node-pty-runtime.js.",
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
    appendDesktopLog(`[desktop] node-pty spawn failed: ${err instanceof Error ? err.stack || err.message : String(err)}`);
    throw new Error(`Terminal spawn failed: ${detail}`);
  }

  const sessionId = `term_${ptyIdCounter++}`;
  const session = {
    process: ptyProcess,
    conversationId: ownerConversationId,
    cwd: resolvedCwd,
    shell: shellStr,
    scrollback: "",
    outputCursor: 0,
    isAlive: true,
    exitCode: null,
    exitedAt: null,
  };
  ptySessions.set(sessionId, session);

  ptyProcess.onData((data) => {
    if (session.isAlive) appendSessionOutput(sessionId, session, data);
  });

  ptyProcess.onExit(({ exitCode, signal }) => {
    if (ptySessions.get(sessionId) !== session || session.isAlive === false) return;
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
    const wc = getSendTarget();
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

function writeToSession(sessionId, data, conversationId) {
  if (typeof sessionId !== "string" || typeof data !== "string") {
    return false;
  }
  if (data.length > PTY_WRITE_MAX_CHARS || data.includes("\0")) {
    appendDesktopLog(`[desktop] rejected invalid pty write for ${sessionId}`);
    return false;
  }
  const session = ptySessions.get(sessionId);
  if (session?.isAlive && isOwnedBy(session, conversationId)) {
    for (let offset = 0; offset < data.length; offset += PTY_WRITE_CHUNK_CHARS) {
      session.process.write(data.slice(offset, offset + PTY_WRITE_CHUNK_CHARS));
    }
    return true;
  }
  return false;
}

function resizeSession(sessionId, cols, rows, conversationId) {
  const session = ptySessions.get(sessionId);
  if (session?.isAlive && isOwnedBy(session, conversationId)) {
    try {
      const size = normalizeTerminalSize(cols, rows);
      session.process.resize(size.cols, size.rows);
    } catch (e) {
      // noop
    }
  }
}

function killProcessTreeAndWait(pid, fallback) {
  if (!Number.isFinite(pid) || pid <= 0) {
    try { fallback(); } catch {}
    return Promise.resolve();
  }
  if (process.platform !== "win32") {
    try { process.kill(-pid, "SIGTERM"); } catch { try { fallback(); } catch {} }
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const killer = spawn("taskkill", ["/pid", String(pid), "/t", "/f"], { windowsHide: true });
    let settled = false;
    const finish = (useFallback) => {
      if (settled) return;
      settled = true;
      if (useFallback) {
        try { fallback(); } catch {}
      }
      resolve();
    };
    killer.once("error", () => finish(true));
    killer.once("close", (code) => finish(code !== 0));
  });
}

async function killSession(sessionId, conversationId) {
  const session = ptySessions.get(sessionId);
  if (isOwnedBy(session, conversationId)) {
    if (session.isAlive) {
      await killProcessTreeAndWait(session.process.pid, () => session.process.kill());
    }
    if (ptySessions.get(sessionId) === session) ptySessions.delete(sessionId);
    return true;
  }
  return false;
}

async function restartSession(sessionId, conversationId) {
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId)) return null;
  const cwd = session.cwd;
  appendDesktopLog(`[desktop] restarting pty ${sessionId} for ${conversationId}`);
  if (!await killSession(sessionId, conversationId)) return null;
  try {
    const replacement = spawnSession(cwd, conversationId);
    appendDesktopLog(`[desktop] restarted pty ${sessionId} as ${replacement.session_id}`);
    return replacement;
  } catch (error) {
    appendDesktopLog(`[desktop] pty restart failed for ${sessionId}: ${error.message}`);
    throw error;
  }
}

async function killConversation(conversationId) {
  const owner = requireConversationId(conversationId);
  const ownedSessionIds = Array.from(ptySessions.entries())
    .filter(([, session]) => session.conversationId === owner)
    .map(([sessionId]) => sessionId);
  const results = await Promise.all(
    ownedSessionIds.map((sessionId) => killSession(sessionId, owner)),
  );
  return results.filter(Boolean).length;
}

function acknowledgeExitedSession(sessionId, conversationId) {
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId) || session.isAlive !== false) return false;
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

function snapshotSession(sessionId, maxChars, conversationId) {
  pruneExitedSessions();
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId)) {
    return null;
  }
  return _snapshotEntry(sessionId, session, maxChars);
}

function clearSession(sessionId, conversationId) {
  pruneExitedSessions();
  const session = ptySessions.get(sessionId);
  if (!isOwnedBy(session, conversationId)) {
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

function listSessions(conversationId, maxChars) {
  pruneExitedSessions();
  const owner = normalizeConversationId(conversationId);
  if (!owner) return [];
  const list = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    if (session.conversationId !== owner) continue;
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
    });
  }
  return list.sort((left, right) => left.session_id.localeCompare(right.session_id));
}

async function killAllSessions() {
  if (ptyCleanupDone) return;
  ptyCleanupDone = true;
  const terminations = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    session.isAlive = false;
    try {
      terminations.push(
        killProcessTreeAndWait(session.process.pid, () => session.process.kill())
          .catch((error) => {
            appendDesktopLog(`[desktop] failed to kill pty ${sessionId}: ${error.message}`);
          }),
      );
    } catch (error) {
      appendDesktopLog(`[desktop] failed to kill pty ${sessionId}: ${error.message}`);
    }
  }
  ptySessions.clear();
  await Promise.allSettled(terminations);
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
  resolveTerminalCwd,
  normalizeTerminalSize,
  windowsPowerShellArgs,
};

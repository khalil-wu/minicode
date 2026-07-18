"use strict";

const path = require("node:path");
const { StringDecoder } = require("node:string_decoder");

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
    wc.send("minicode:pty:data", { sessionId, data: text, startCursor, endCursor: session.outputCursor });
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

function spawnSession(cwd) {
  pruneExitedSessions();
  const liveSessionCount = Array.from(ptySessions.values()).filter((session) => session.isAlive !== false).length;
  if (liveSessionCount >= PTY_LIVE_SESSION_MAX) {
    throw new Error(`Terminal session limit reached (${PTY_LIVE_SESSION_MAX}). Close a terminal before opening another.`);
  }
  const shellStr = process.platform === "win32" ? "powershell.exe" : (process.env.SHELL || "bash");
  const shellArgs = process.platform === "win32" ? windowsPowerShellArgs() : [];
  const resolvedCwd = resolveTerminalCwd(cwd);

  let ptyProcess;
  if (pty) {
    try {
      ptyProcess = pty.spawn(shellStr, shellArgs, {
        name: "xterm-256color",
        cols: 80,
        rows: 24,
        cwd: resolvedCwd,
        env: sanitizedPtyEnv(),
      });
    } catch (err) {
      console.error("[desktop] node-pty spawn failed, falling back to child_process:", err);
    }
  }

  if (!ptyProcess) {
    try {
      const cp = require("node:child_process");
      const sub = cp.spawn(shellStr, shellArgs, {
        cwd: resolvedCwd,
        env: sanitizedPtyEnv(),
        windowsHide: true,
      });
      const stdoutDecoder = new StringDecoder("utf8");
      const stderrDecoder = new StringDecoder("utf8");

      ptyProcess = {
        pid: sub.pid,
        write: (data) => {
          if (sub.stdin && !sub.stdin.destroyed) {
            sub.stdin.write(data);
          }
        },
        resize: () => {},
        kill: () => {
          sub.kill();
        },
        onData: (cb) => {
          sub.stdout.on("data", (chunk) => {
            const text = stdoutDecoder.write(chunk);
            if (text) cb(text);
          });
          sub.stdout.on("end", () => {
            const text = stdoutDecoder.end();
            if (text) cb(text);
          });
          sub.stderr.on("data", (chunk) => {
            const text = stderrDecoder.write(chunk);
            if (text) cb(text);
          });
          sub.stderr.on("end", () => {
            const text = stderrDecoder.end();
            if (text) cb(text);
          });
        },
        onExit: (cb) => {
          sub.on("exit", (exitCode) => cb({ exitCode: exitCode ?? 0 }));
        },
      };
    } catch (cpErr) {
      throw new Error("Terminal process spawn failed: " + cpErr.message);
    }
  }

  const sessionId = `term_${ptyIdCounter++}`;
  const session = {
    process: ptyProcess,
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

  ptyProcess.onExit(({ exitCode }) => {
    if (ptySessions.get(sessionId) !== session || session.isAlive === false) return;
    const normalizedExitCode = Number.isFinite(exitCode) ? exitCode : 0;
    appendSessionOutput(sessionId, session, `\r\n[Process exited with code ${normalizedExitCode}]\r\n`);
    session.isAlive = false;
    session.exitCode = normalizedExitCode;
    session.exitedAt = Date.now();
    const wc = getSendTarget();
    if (wc) {
      wc.send("minicode:pty:exit", { sessionId, exitCode: normalizedExitCode, exitedAt: session.exitedAt });
    }
    pruneExitedSessions();
  });

  return { session_id: sessionId, pid: ptyProcess.pid, shell: shellStr, cwd: resolvedCwd };
}

function writeToSession(sessionId, data) {
  if (typeof sessionId !== "string" || typeof data !== "string") {
    return false;
  }
  if (data.length > PTY_WRITE_MAX_CHARS || data.includes("\0")) {
    appendDesktopLog(`[desktop] rejected invalid pty write for ${sessionId}`);
    return false;
  }
  const session = ptySessions.get(sessionId);
  if (session?.isAlive) {
    for (let offset = 0; offset < data.length; offset += PTY_WRITE_CHUNK_CHARS) {
      session.process.write(data.slice(offset, offset + PTY_WRITE_CHUNK_CHARS));
    }
    return true;
  }
  return false;
}

function resizeSession(sessionId, cols, rows) {
  const session = ptySessions.get(sessionId);
  if (session?.isAlive) {
    try {
      const size = normalizeTerminalSize(cols, rows);
      session.process.resize(size.cols, size.rows);
    } catch (e) {
      // noop
    }
  }
}

function killSession(sessionId) {
  const session = ptySessions.get(sessionId);
  if (session) {
    ptySessions.delete(sessionId);
    if (session.isAlive) session.process.kill();
  }
}

function acknowledgeExitedSession(sessionId) {
  const session = ptySessions.get(sessionId);
  if (!session || session.isAlive !== false) return false;
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
    exited_at: session.exitedAt,
  };
}

function snapshotSession(sessionId, maxChars) {
  pruneExitedSessions();
  const session = ptySessions.get(sessionId);
  if (!session) {
    return null;
  }
  return _snapshotEntry(sessionId, session, maxChars);
}

function listSessions(maxChars) {
  pruneExitedSessions();
  const list = [];
  for (const [sessionId, session] of ptySessions.entries()) {
    list.push(_snapshotEntry(sessionId, session, maxChars || PTY_LIST_PREVIEW_CHARS));
  }
  return list;
}

function killAllSessions() {
  if (ptyCleanupDone) return;
  ptyCleanupDone = true;
  for (const [sessionId, session] of ptySessions.entries()) {
    try {
      session.process.kill();
    } catch (error) {
      appendDesktopLog(`[desktop] failed to kill pty ${sessionId}: ${error.message}`);
    }
  }
  ptySessions.clear();
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
  listSessions,
  snapshotSession,
  acknowledgeExitedSession,
  pruneExitedSessions,
  killAllSessions,
  resolveTerminalCwd,
  normalizeTerminalSize,
  windowsPowerShellArgs,
};

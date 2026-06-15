"use strict";

const path = require("node:path");

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
  return path.resolve(cwd);
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
      });

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
          sub.stdout.on("data", (chunk) => cb(chunk.toString("utf8")));
          sub.stderr.on("data", (chunk) => cb(chunk.toString("utf8")));
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
  };
  ptySessions.set(sessionId, session);

  ptyProcess.onData((data) => {
    const text = typeof data === "string" ? data : String(data);
    session.scrollback += text;
    if (session.scrollback.length > PTY_SCROLLBACK_MAX_CHARS) {
      session.scrollback = session.scrollback.slice(session.scrollback.length - PTY_SCROLLBACK_MAX_CHARS);
    }
    const wc = getSendTarget();
    if (wc) {
      wc.send("minicode:pty:data", { sessionId, data });
    }
  });

  ptyProcess.onExit(({ exitCode }) => {
    ptySessions.delete(sessionId);
    const wc = getSendTarget();
    if (wc) {
      wc.send("minicode:pty:exit", { sessionId, exitCode });
    }
  });

  return { session_id: sessionId, pid: ptyProcess.pid, shell: shellStr, cwd: resolvedCwd };
}

function writeToSession(sessionId, data) {
  if (typeof sessionId !== "string" || typeof data !== "string") {
    return false;
  }
  if (data.length > 8192 || data.includes("\0")) {
    appendDesktopLog(`[desktop] rejected invalid pty write for ${sessionId}`);
    return false;
  }
  const session = ptySessions.get(sessionId);
  if (session) {
    session.process.write(data);
    return true;
  }
  return false;
}

function resizeSession(sessionId, cols, rows) {
  const session = ptySessions.get(sessionId);
  if (session) {
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
    session.process.kill();
    ptySessions.delete(sessionId);
  }
}

function _snapshotEntry(sessionId, session, maxChars) {
  const limit = Math.max(1, Math.min(Number(maxChars) || PTY_SNAPSHOT_DEFAULT_CHARS, PTY_SCROLLBACK_MAX_CHARS));
  const full = session.scrollback || "";
  const truncated = full.length > limit;
  return {
    session_id: sessionId,
    pid: session.process.pid,
    shell: session.shell || session.process.process || "shell",
    cwd: session.cwd,
    output: truncated ? full.slice(full.length - limit) : full,
    output_chars: truncated ? limit : full.length,
    total_output_chars: full.length,
    truncated,
    is_alive: true,
  };
}

function snapshotSession(sessionId, maxChars) {
  const session = ptySessions.get(sessionId);
  if (!session) {
    return null;
  }
  return _snapshotEntry(sessionId, session, maxChars);
}

function listSessions(maxChars) {
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
  killAllSessions,
  resolveTerminalCwd,
  normalizeTerminalSize,
  windowsPowerShellArgs,
};

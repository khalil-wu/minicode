"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { exec } = require("node:child_process");
const { ipcMain, dialog, shell, app } = require("electron");

const {
  isHttpUrl,
  isProbablyTextBuffer,
  hashFileContent,
  countDirEntries,
  isSamePath,
} = require("./utils");

const {
  assertTrustedPath,
  assertMutableTrustedPath,
  isSafeWorkspacePath,
  isWithinTrustedWorkspace,
  rememberTrustedWorkspaceRoot,
} = require("./security");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let ipcHandlersRegistered = false;

// Dependencies injected via init()
let ctx = {};

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

function init(deps) {
  ctx = deps || {};
}

// ---------------------------------------------------------------------------
// Workspace search (internal helpers)
// ---------------------------------------------------------------------------

const MAX_WORKSPACE_SEARCH_DEPTH =
  Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_DEPTH) > 0
    ? Math.floor(Number(process.env.MINICODE_WORKSPACE_SEARCH_MAX_DEPTH))
    : 12;

const WORKSPACE_SEARCH_IGNORED_DIRS = new Set([
  ".git", ".idea", ".vscode", ".venv", "venv",
  "node_modules", "__pycache__", "dist", "build",
]);

function scoreWorkspaceSearchMatch(relativePath, query) {
  const normalizedQuery = String(query || "").trim().toLowerCase();
  const normalizedPath = relativePath.replace(/\\/g, "/").toLowerCase();
  if (!normalizedQuery) {
    return { score: 1, matched_indices: [] };
  }

  let queryIndex = 0;
  const matchedIndices = [];
  let score = 0;
  for (let pathIndex = 0; pathIndex < normalizedPath.length && queryIndex < normalizedQuery.length; pathIndex += 1) {
    if (normalizedPath[pathIndex] !== normalizedQuery[queryIndex]) {
      continue;
    }

    matchedIndices.push(pathIndex);
    score += 1;
    if (pathIndex === 0 || ["/", "\\", "-", "_", "."].includes(relativePath[pathIndex - 1])) {
      score += 10;
    }
    if (matchedIndices.length > 1 && matchedIndices[matchedIndices.length - 2] === pathIndex - 1) {
      score += 5;
    }
    queryIndex += 1;
  }

  if (queryIndex < normalizedQuery.length) {
    return null;
  }

  const fileName = path.basename(relativePath).toLowerCase();
  if (fileName.includes(normalizedQuery)) {
    score += 50;
  }

  return { score, matched_indices: matchedIndices };
}

async function searchWorkspaceFiles(rootPath, query, limit = 20, kind = "file") {
  const workspaceRoot = assertTrustedPath(rootPath, "Workspace root");
  if (!fs.existsSync(workspaceRoot)) {
    throw new Error(`Directory not found: ${rootPath}`);
  }

  const maxResults = Math.max(1, Math.min(Number(limit) || 20, 100));
  const searchKind = ["file", "folder", "all"].includes(kind) ? kind : "file";
  const results = [];

  async function visitDirectory(directoryPath, depth = 0) {
    if (depth > MAX_WORKSPACE_SEARCH_DEPTH) {
      return;
    }
    const dirents = await fs.promises.readdir(directoryPath, { withFileTypes: true });
    for (const dirent of dirents) {
      if (dirent.name.startsWith(".") || WORKSPACE_SEARCH_IGNORED_DIRS.has(dirent.name)) {
        continue;
      }
      if (dirent.isSymbolicLink()) {
        continue;
      }

      const fullPath = path.join(directoryPath, dirent.name);
      if (!isWithinTrustedWorkspace(fullPath)) {
        continue;
      }
      if (dirent.isDirectory()) {
        if (searchKind === "folder" || searchKind === "all") {
          const relativePath = path.relative(workspaceRoot, fullPath).replace(/\\/g, "/");
          const match = scoreWorkspaceSearchMatch(relativePath, query);
          if (match) {
            results.push({
              path: relativePath,
              name: dirent.name,
              score: match.score,
              matched_indices: match.matched_indices,
              kind: "folder",
            });
          }
        }
        await visitDirectory(fullPath, depth + 1);
        continue;
      }

      if (!dirent.isFile() || searchKind === "folder") {
        continue;
      }

      const relativePath = path.relative(workspaceRoot, fullPath).replace(/\\/g, "/");
      const match = scoreWorkspaceSearchMatch(relativePath, query);
      if (!match) {
        continue;
      }

      results.push({
        path: relativePath,
        name: dirent.name,
        score: match.score,
        matched_indices: match.matched_indices,
        kind: "file",
      });
    }
  }

  await visitDirectory(workspaceRoot);
  results.sort((a, b) => b.score - a.score || a.path.localeCompare(b.path));

  return {
    query: String(query || ""),
    results: results.slice(0, maxResults),
  };
}

// ---------------------------------------------------------------------------
// Environment detection
// ---------------------------------------------------------------------------

function checkCommand(command) {
  return new Promise((resolve) => {
    exec(`${command} --version`, (error) => {
      resolve(!error);
    });
  });
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

function registerIpcHandlers() {
  if (ipcHandlersRegistered) return;
  ipcHandlersRegistered = true;

  const {
    getMainWindow,
    showDesktopNotification,
    dispatchDeepLink,
    attemptAppStartup,
    startupFailureState,
    getDesktopLogPath,
    appendDesktopLog,
    exportDesktopDiagnostics,
    discoverChromeCdp,
    captureChromeTargetScreenshot,
    navigateChromeTarget,
    clickChromeTarget,
    typeIntoChromeTarget,
    ptyManager,
    lastPickedWorkspaceRoot: getLastPickedWorkspaceRoot,
    setLastPickedWorkspaceRoot,
  } = ctx;

  // Helper to safely get the main window
  const getWin = () => {
    const fn = typeof getMainWindow === "function" ? getMainWindow : () => null;
    return fn();
  };

  // -----------------------------------------------------------------------
  // Window controls
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:window:minimize", () => {
    const win = getWin();
    if (!win || win.isDestroyed()) return false;
    win.minimize();
    return true;
  });

  ipcMain.handle("minicode:window:maximize", () => {
    const win = getWin();
    if (!win || win.isDestroyed()) return false;
    if (win.isMaximized()) {
      win.unmaximize();
    } else {
      win.maximize();
    }
    return true;
  });

  ipcMain.handle("minicode:window:close", () => {
    const win = getWin();
    if (!win || win.isDestroyed()) return false;
    win.close();
    return true;
  });

  // -----------------------------------------------------------------------
  // Notifications & deep links
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:notify", (_event, payload) => {
    return typeof showDesktopNotification === "function"
      ? showDesktopNotification(payload)
      : false;
  });

  ipcMain.handle("minicode:pickDirectory", async () => {
    const owner = (() => { const w = getWin(); return w && !w.isDestroyed() ? w : undefined; })();
    const result = await dialog.showOpenDialog(owner, {
      properties: ["openDirectory"],
    });
    if (result.canceled || result.filePaths.length === 0) {
      return "";
    }
    if (typeof setLastPickedWorkspaceRoot === "function") {
      setLastPickedWorkspaceRoot(path.resolve(result.filePaths[0]));
    }
    return rememberTrustedWorkspaceRoot(result.filePaths[0]);
  });

  ipcMain.handle("minicode:workspace:trust", (_event, targetPath) => {
    if (typeof targetPath !== "string" || !targetPath.trim()) {
      return "";
    }
    const resolved = path.resolve(targetPath);
    const lastPicked = typeof getLastPickedWorkspaceRoot === "function" ? getLastPickedWorkspaceRoot() : "";
    const cameFromNativePicker = isSamePath(resolved, lastPicked);
    const alreadyInsideTrustedRoot = isWithinTrustedWorkspace(resolved);
    const isSafePath = isSafeWorkspacePath(resolved);
    if (!cameFromNativePicker && !alreadyInsideTrustedRoot && !isSafePath) {
      if (typeof appendDesktopLog === "function") {
        appendDesktopLog(`[desktop] rejected renderer-requested workspace trust: ${resolved}`);
      }
      return "";
    }
    return rememberTrustedWorkspaceRoot(resolved);
  });

  ipcMain.handle("minicode:openExternal", async (_event, target) => {
    if (typeof target !== "string" || !target.trim() || !isHttpUrl(target)) {
      return false;
    }
    await shell.openExternal(target.trim());
    return true;
  });

  ipcMain.handle("minicode:revealPath", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    try {
      const resolved = path.resolve(target.trim());
      if (!isWithinTrustedWorkspace(resolved)) {
        throw new Error("Path is outside the trusted workspace.");
      }
      shell.showItemInFolder(resolved);
      return true;
    } catch (error) {
      if (typeof appendDesktopLog === "function") {
        appendDesktopLog(`[desktop] revealPath denied: ${error.message}`);
      }
      return false;
    }
  });

  ipcMain.handle("minicode:deepLink:open", (_event, target) => {
    if (typeof target !== "string" || !target.trim()) {
      return false;
    }
    return typeof dispatchDeepLink === "function" ? dispatchDeepLink(target) : false;
  });

  // -----------------------------------------------------------------------
  // Startup / diagnostics
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:startup:getState", () => startupFailureState);
  ipcMain.handle("minicode:startup:retry", async () => {
    return typeof attemptAppStartup === "function" ? attemptAppStartup("startup-retry") : false;
  });
  ipcMain.handle("minicode:startup:quit", () => {
    app.quit();
    return true;
  });
  ipcMain.handle("minicode:startup:openLogs", () => {
    const logPath = typeof getDesktopLogPath === "function" ? getDesktopLogPath() : "";
    if (typeof appendDesktopLog === "function") {
      appendDesktopLog("[desktop] open logs requested from startup failure surface");
    }
    shell.showItemInFolder(logPath);
    return true;
  });
  ipcMain.handle("minicode:diagnostics:export", () => {
    return typeof exportDesktopDiagnostics === "function" ? exportDesktopDiagnostics() : {};
  });

  // -----------------------------------------------------------------------
  // Browser / CDP
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:browser:discover", async (_event, endpoint) => {
    return typeof discoverChromeCdp === "function" ? discoverChromeCdp(endpoint) : { status: "error" };
  });
  ipcMain.handle("minicode:browser:captureScreenshot", async (_event, endpoint, targetId) => {
    return typeof captureChromeTargetScreenshot === "function" ? captureChromeTargetScreenshot(endpoint, targetId) : null;
  });
  ipcMain.handle("minicode:browser:navigate", async (_event, endpoint, targetId, url, options) => {
    return typeof navigateChromeTarget === "function" ? navigateChromeTarget(endpoint, targetId, url, options) : null;
  });
  ipcMain.handle("minicode:browser:click", async (_event, endpoint, targetId, selector) => {
    return typeof clickChromeTarget === "function" ? clickChromeTarget(endpoint, targetId, selector) : null;
  });
  ipcMain.handle("minicode:browser:type", async (_event, endpoint, targetId, selector, text) => {
    return typeof typeIntoChromeTarget === "function" ? typeIntoChromeTarget(endpoint, targetId, selector, text) : null;
  });

  // -----------------------------------------------------------------------
  // Filesystem
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:fs:listTree", async (_event, targetPath) => {
    const rootPath = assertTrustedPath(targetPath, "Directory");
    if (!fs.existsSync(rootPath)) {
      throw new Error(`Directory not found: ${targetPath}`);
    }
    const stat = await fs.promises.stat(rootPath);
    if (!stat.isDirectory()) {
      throw new Error(`Path is not a directory: ${targetPath}`);
    }

    const dirents = await fs.promises.readdir(rootPath, { withFileTypes: true });
    const entries = [];
    for (const dirent of dirents) {
      if (dirent.isSymbolicLink()) continue;
      const fullPath = path.join(rootPath, dirent.name);
      if (!isWithinTrustedWorkspace(fullPath)) continue;
      try {
        const childStat = await fs.promises.lstat(fullPath);
        entries.push({
          name: dirent.name,
          path: fullPath,
          is_dir: dirent.isDirectory(),
          size_bytes: dirent.isFile() ? childStat.size : null,
          modified_at: childStat.mtime.toISOString(),
        });
      } catch (e) {
        // Skip inaccessible files
      }
    }

    entries.sort((a, b) => {
      if (a.is_dir === b.is_dir) return a.name.localeCompare(b.name);
      return a.is_dir ? -1 : 1;
    });

    return {
      workspace_root: rootPath,
      requested_path: rootPath,
      entries: entries,
    };
  });

  ipcMain.handle("minicode:fs:searchFiles", async (_event, rootPath, query, limit, kind) => {
    return searchWorkspaceFiles(rootPath, query, limit, kind);
  });

  ipcMain.handle("minicode:fs:readFile", async (_event, targetPath) => {
    const fullPath = assertTrustedPath(targetPath, "File");
    const stat = await fs.promises.stat(fullPath);
    const MAX_READ_SIZE = 2 * 1024 * 1024; // Keep desktop editor reads aligned with the backend workspace API.
    if (stat.size > MAX_READ_SIZE) {
      throw new Error(`File too large (${(stat.size / 1024 / 1024).toFixed(1)}MB). Max ${MAX_READ_SIZE / 1024 / 1024}MB.`);
    }
    const raw = await fs.promises.readFile(fullPath);
    if (!isProbablyTextBuffer(raw, fullPath)) {
      throw new Error("Only UTF-8 text files are supported.");
    }
    const content = raw.toString("utf8");
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text",
    };
  });

  ipcMain.handle("minicode:fs:writeFile", async (_event, targetPath, content) => {
    const fullPath = assertMutableTrustedPath(targetPath, "File");
    if (fs.existsSync(fullPath)) {
      throw new Error("Direct writeFile cannot overwrite existing files. Use compareWriteFile with an expected hash.");
    }
    await fs.promises.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.promises.writeFile(fullPath, content, "utf8");
    const stat = await fs.promises.stat(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text",
    };
  });

  ipcMain.handle("minicode:fs:compareWriteFile", async (_event, targetPath, expectedHash, content) => {
    const fullPath = assertMutableTrustedPath(targetPath, "File");
    if (fs.existsSync(fullPath) && fs.statSync(fullPath).isDirectory()) {
      throw new Error("Cannot write content into a directory path.");
    }

    let currentHash = "";
    if (fs.existsSync(fullPath)) {
      const raw = fs.readFileSync(fullPath);
      if (!isProbablyTextBuffer(raw, fullPath)) {
        throw new Error("Only UTF-8 text files are supported.");
      }
      currentHash = hashFileContent(raw.toString("utf8"));
    }

    const normalizedExpected = String(expectedHash || "").trim().toLowerCase();
    if (currentHash !== normalizedExpected) {
      const error = new Error("File has changed on disk.");
      error.code = "ERR_FILE_CHANGED";
      error.expectedHash = normalizedExpected;
      error.actualHash = currentHash;
      throw error;
    }

    fs.mkdirSync(path.dirname(fullPath), { recursive: true });
    fs.writeFileSync(fullPath, content, "utf8");
    const stat = fs.statSync(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      content,
      content_hash: hashFileContent(content),
      size_bytes: stat.size,
      modified_at: stat.mtime.toISOString(),
      language_hint: path.extname(fullPath).slice(1) || "text",
    };
  });

  ipcMain.handle("minicode:fs:createDirectory", async (_event, targetPath) => {
    const fullPath = assertMutableTrustedPath(targetPath, "Directory");
    await fs.promises.mkdir(fullPath, { recursive: true });
    const stat = await fs.promises.stat(fullPath);
    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      name: path.basename(fullPath),
      is_dir: true,
      size_bytes: null,
      modified_at: stat.mtime.toISOString(),
    };
  });

  ipcMain.handle("minicode:fs:renamePath", async (_event, oldPath, newPath) => {
    const fullOldPath = assertMutableTrustedPath(oldPath, "Source path");
    const fullNewPath = assertMutableTrustedPath(newPath, "Destination path");
    await fs.promises.rename(fullOldPath, fullNewPath);
    const stat = await fs.promises.stat(fullNewPath);
    return {
      workspace_root: path.dirname(fullNewPath),
      path: fullNewPath,
      name: path.basename(fullNewPath),
      is_dir: stat.isDirectory(),
      size_bytes: stat.isFile() ? stat.size : null,
      modified_at: stat.mtime.toISOString(),
    };
  });

  ipcMain.handle("minicode:fs:deletePath", async (_event, targetPath, recursive, confirm) => {
    const fullPath = assertMutableTrustedPath(targetPath, "Path");
    const stat = await fs.promises.stat(fullPath);
    const isDir = stat.isDirectory();

    if (isDir && recursive && !confirm) {
      const count = await countDirEntries(fullPath, 51);
      if (count > 50) {
        return { needsConfirmation: true, path: fullPath, entryCount: count };
      }
    }

    await shell.trashItem(fullPath);

    return {
      workspace_root: path.dirname(fullPath),
      path: fullPath,
      deleted: true,
      is_dir: isDir,
    };
  });

  // -----------------------------------------------------------------------
  // PTY
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:pty:spawn", (_event, cwd) => {
    return ptyManager.spawnSession(cwd);
  });

  ipcMain.handle("minicode:pty:write", (_event, sessionId, data) => {
    return ptyManager.writeToSession(sessionId, data);
  });

  ipcMain.handle("minicode:pty:resize", (_event, sessionId, cols, rows) => {
    ptyManager.resizeSession(sessionId, cols, rows);
  });

  ipcMain.handle("minicode:pty:kill", (_event, sessionId) => {
    ptyManager.killSession(sessionId);
  });

  ipcMain.handle("minicode:pty:list", () => {
    return ptyManager.listSessions();
  });

  ipcMain.handle("minicode:pty:snapshot", (_event, sessionId, maxChars) => {
    return ptyManager.snapshotSession(sessionId, maxChars);
  });

  // -----------------------------------------------------------------------
  // Environment detection
  // -----------------------------------------------------------------------

  ipcMain.handle("minicode:env:detect", async () => {
    const [hasGit, hasPython, hasNode, hasDocker, hasOllama] = await Promise.all([
      checkCommand("git"),
      checkCommand("python"),
      checkCommand("node"),
      checkCommand("docker"),
      checkCommand("ollama"),
    ]);
    return {
      git: hasGit,
      python: hasPython,
      node: hasNode,
      docker: hasDocker,
      ollama: hasOllama,
      home: require("node:os").homedir(),
    };
  });
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  registerIpcHandlers,
};

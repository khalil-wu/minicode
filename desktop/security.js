"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { normalizeWithTrailingSeparator, isSamePath } = require("./utils");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let trustedWorkspaceRoots = new Set();
let appendDesktopLog = () => {};

function init({ initialRoots, logger }) {
  trustedWorkspaceRoots = initialRoots || new Set();
  if (typeof logger === "function") {
    appendDesktopLog = logger;
  }
}

function getTrustedWorkspaceRoots() {
  return trustedWorkspaceRoots;
}

// ---------------------------------------------------------------------------
// Safe workspace path validation
// ---------------------------------------------------------------------------

function isSafeWorkspacePath(resolved) {
  const normalized = resolved.replace(/\\/g, "/");
  if (/^[A-Za-z]:\/?$/.test(normalized) || normalized === "/") return false;
  const segments = normalized.split("/").filter(Boolean);
  if (segments.length < 2) return false;
  const systemPrefixes = [
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev",
    "/var/run", "/var/log", "/root",
  ];
  const winSystemPrefixes = [
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/recovery", "c:/system volume information",
  ];
  const lower = normalized.toLowerCase();
  for (const prefix of [...systemPrefixes, ...winSystemPrefixes]) {
    if (lower === prefix || lower.startsWith(prefix + "/")) return false;
  }
  return true;
}

function rememberTrustedWorkspaceRoot(targetPath) {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    return "";
  }
  const resolved = path.resolve(targetPath);
  if (!isSafeWorkspacePath(resolved)) {
    appendDesktopLog(`[desktop] rejected unsafe workspace path: ${resolved}`);
    return "";
  }
  trustedWorkspaceRoots.add(resolved);
  return resolved;
}

function isTrustedWorkspaceRootPath(targetPath) {
  const resolved = path.resolve(targetPath);
  for (const root of trustedWorkspaceRoots) {
    if (isSamePath(resolved, root)) return true;
  }
  return false;
}

function isWithinTrustedWorkspace(targetPath) {
  const resolved = path.resolve(targetPath);
  const isWindows = process.platform === "win32";
  for (const root of trustedWorkspaceRoots) {
    if (isWindows) {
      const resolvedLower = resolved.toLowerCase();
      const rootLower = root.toLowerCase();
      if (resolvedLower === rootLower || resolvedLower.startsWith(normalizeWithTrailingSeparator(rootLower))) {
        return true;
      }
    } else {
      if (resolved === root || resolved.startsWith(normalizeWithTrailingSeparator(root))) {
        return true;
      }
    }
  }
  return false;
}

function trustedPathCandidates(targetPath) {
  const rawPath = targetPath.trim();
  if (path.isAbsolute(rawPath)) {
    return [path.resolve(rawPath)];
  }
  return Array.from(trustedWorkspaceRoots, (root) => path.resolve(root, rawPath));
}

function assertTrustedPath(targetPath, label = "Path") {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    throw new Error(`${label} is required.`);
  }
  const candidates = trustedPathCandidates(targetPath);
  const resolved = candidates.find((candidate) => isWithinTrustedWorkspace(candidate) && fs.existsSync(candidate))
    || candidates.find((candidate) => isWithinTrustedWorkspace(candidate))
    || path.resolve(targetPath);
  if (!isWithinTrustedWorkspace(resolved)) {
    throw new Error(`${label} is outside the trusted workspace.`);
  }
  return resolved;
}

// ---------------------------------------------------------------------------
// Protected write paths
// ---------------------------------------------------------------------------

const PROTECTED_WRITE_FILE_NAMES = new Set([
  ".gitconfig", ".gitmodules", ".mcp.json", ".claude.json",
  ".codex.json", "settings.json", "settings.local.json",
]);
const PROTECTED_WRITE_PATH_PARTS = new Set([".git", ".claude", ".codex"]);

function isProtectedWritePath(resolvedPath) {
  const basename = path.basename(resolvedPath).toLowerCase();
  if (PROTECTED_WRITE_FILE_NAMES.has(basename)) return true;
  const parts = resolvedPath.split(path.sep).map(p => p.toLowerCase());
  for (const part of parts) {
    if (PROTECTED_WRITE_PATH_PARTS.has(part)) return true;
  }
  // Handle worktree: .git may be a file containing "gitdir: <real-path>"
  for (const root of trustedWorkspaceRoots) {
    const dotGit = path.join(root, ".git");
    try {
      const stat = fs.statSync(dotGit);
      if (stat.isFile()) {
        const content = fs.readFileSync(dotGit, "utf8").trim();
        const match = content.match(/^gitdir:\s*(.+)$/m);
        if (match) {
          const realGitDir = path.resolve(root, match[1]);
          if (process.platform === "win32") {
            if (resolvedPath.toLowerCase() === realGitDir.toLowerCase() ||
                resolvedPath.toLowerCase().startsWith(realGitDir.toLowerCase() + path.sep)) {
              return true;
            }
          } else {
            if (resolvedPath === realGitDir || resolvedPath.startsWith(realGitDir + path.sep)) {
              return true;
            }
          }
        }
      }
    } catch {
      // .git doesn't exist or isn't readable — skip
    }
  }
  return false;
}

function assertMutableTrustedPath(targetPath, label = "Path") {
  const resolved = assertTrustedPath(targetPath, label);
  if (isTrustedWorkspaceRootPath(resolved)) {
    throw new Error(`${label} cannot be a trusted workspace root.`);
  }
  if (isProtectedWritePath(resolved)) {
    throw new Error(`${label} targets a protected path and cannot be modified.`);
  }
  return resolved;
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  init,
  getTrustedWorkspaceRoots,
  isSafeWorkspacePath,
  rememberTrustedWorkspaceRoot,
  isTrustedWorkspaceRootPath,
  isWithinTrustedWorkspace,
  assertTrustedPath,
  assertMutableTrustedPath,
  isProtectedWritePath,
  PROTECTED_WRITE_FILE_NAMES,
  PROTECTED_WRITE_PATH_PARTS,
};

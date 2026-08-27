"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { normalizeWithTrailingSeparator, isSamePath } = require("./utils");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let trustedWorkspaceRoots = new Set();
let approvedWorkspaceRoots = new Set();
let appReadOnlyRoots = new Set();
let userOutputRoots = new Set();
let appendDesktopLog = () => {};
let trustedRootsFile = "";

const SAFE_USER_OUTPUT_EXTENSIONS = new Set([
  ".7z", ".aac", ".avif", ".bmp", ".csv", ".doc", ".docx", ".epub",
  ".flac", ".gif", ".ico", ".jpeg", ".jpg", ".json", ".m4a", ".md",
  ".mov", ".mp3", ".mp4", ".odp", ".ods", ".odt", ".ogg", ".pdf",
  ".png", ".ppt", ".pptx", ".rtf", ".tar", ".tif", ".tiff",
  ".tsv", ".txt", ".wav", ".webm", ".webp", ".xlsx", ".xls", ".xml",
  ".yaml", ".yml", ".zip",
]);

function canonicalizeRootSet(roots) {
  const result = new Set();
  for (const root of roots || []) {
    const canonical = tryCanonicalizePath(root);
    if (canonical && isSafeWorkspacePath(canonical)) result.add(canonical);
  }
  return result;
}

function init({ initialRoots, readOnlyRoots, userOutputRoots: outputRoots, logger, trustedRootsFile: rootsFile }) {
  trustedWorkspaceRoots = canonicalizeRootSet(initialRoots);
  trustedRootsFile = typeof rootsFile === "string" ? rootsFile : "";
  approvedWorkspaceRoots = readApprovedWorkspaceRoots();
  appReadOnlyRoots = canonicalizeRootSet(readOnlyRoots);
  userOutputRoots = canonicalizeRootSet(outputRoots);
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

function systemProtectedPrefixes(environment = process.env) {
  const prefixes = new Set([
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/recovery", "c:/system volume information",
  ]);
  const windowsRoots = [
    environment.SystemRoot,
    environment.windir,
    environment.ProgramFiles,
    environment["ProgramFiles(x86)"],
    environment.ProgramW6432,
    environment.ProgramData,
  ];
  for (const rawPath of windowsRoots) {
    if (typeof rawPath !== "string" || !rawPath.trim()) continue;
    const normalized = rawPath.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    if (/^[a-z]:\//.test(normalized)) prefixes.add(normalized);
  }
  const systemDrive = String(environment.SystemDrive || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  if (/^[a-z]:$/.test(systemDrive)) {
    prefixes.add(`${systemDrive}/recovery`);
    prefixes.add(`${systemDrive}/system volume information`);
  }
  return prefixes;
}

function isSafeWorkspacePath(resolved) {
  const normalized = resolved.replace(/\\/g, "/");
  if (/^[A-Za-z]:\/?$/.test(normalized) || normalized === "/") return false;
  const segments = normalized.split("/").filter(Boolean);
  if (segments.length < 2) return false;
  const systemPrefixes = [
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev",
    "/var/run", "/var/log", "/root",
  ];
  const lower = normalized.toLowerCase();
  for (const prefix of [...systemPrefixes, ...systemProtectedPrefixes()]) {
    if (lower === prefix || lower.startsWith(prefix + "/")) return false;
  }
  return true;
}

function canonicalizePath(targetPath) {
  const resolved = path.resolve(targetPath);
  try {
    return fs.realpathSync.native(resolved);
  } catch (error) {
    if (error?.code !== "ENOENT" && error?.code !== "ENOTDIR") throw error;
    const suffix = [];
    let cursor = resolved;
    while (!fs.existsSync(cursor)) {
      const parent = path.dirname(cursor);
      if (parent === cursor) break;
      suffix.unshift(path.basename(cursor));
      cursor = parent;
    }
    const canonicalParent = fs.existsSync(cursor) ? fs.realpathSync.native(cursor) : cursor;
    return path.resolve(canonicalParent, ...suffix);
  }
}

function tryCanonicalizePath(targetPath) {
  try {
    return canonicalizePath(targetPath);
  } catch {
    return null;
  }
}

function readApprovedWorkspaceRoots() {
  const roots = new Set();
  if (!trustedRootsFile) return roots;
  try {
    const payload = JSON.parse(fs.readFileSync(trustedRootsFile, "utf8"));
    const values = Array.isArray(payload) ? payload : payload?.roots;
    if (!Array.isArray(values)) return roots;
    for (const value of values) {
      if (typeof value !== "string" || !value.trim()) continue;
      const canonical = tryCanonicalizePath(value);
      if (!canonical || !isSafeWorkspacePath(canonical)) continue;
      try {
        if (fs.statSync(canonical).isDirectory()) roots.add(canonical);
      } catch {
        // Ignore stale workspace entries.
      }
    }
  } catch {
    // A missing ledger is normal before the first native folder selection.
  }
  return roots;
}

function persistApprovedWorkspaceRoots() {
  if (!trustedRootsFile) return;
  const directory = path.dirname(trustedRootsFile);
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(
    trustedRootsFile,
    `${JSON.stringify({ version: 1, roots: Array.from(approvedWorkspaceRoots).sort() }, null, 2)}\n`,
    "utf8",
  );
}

function rememberTrustedWorkspaceRoot(targetPath) {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    return "";
  }
  const resolved = tryCanonicalizePath(targetPath);
  if (!resolved || !isSafeWorkspacePath(resolved)) {
    appendDesktopLog(`[desktop] rejected unsafe or inaccessible workspace path: ${path.resolve(targetPath)}`);
    return "";
  }
  try {
    if (!fs.statSync(resolved).isDirectory()) return "";
  } catch {
    return "";
  }
  trustedWorkspaceRoots.add(resolved);
  approvedWorkspaceRoots.add(resolved);
  persistApprovedWorkspaceRoots();
  return resolved;
}

function isTrustedWorkspaceRootPath(targetPath) {
  const resolved = tryCanonicalizePath(targetPath);
  if (!resolved) return false;
  for (const root of trustedWorkspaceRoots) {
    if (isSamePath(resolved, root)) return true;
  }
  return false;
}

function isWithinTrustedWorkspace(targetPath) {
  return isWithinRootSet(targetPath, trustedWorkspaceRoots);
}

function isWithinRootSet(targetPath, roots) {
  const resolved = tryCanonicalizePath(targetPath);
  if (!resolved) return false;
  const isWindows = process.platform === "win32";
  for (const root of roots) {
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

function isWithinAppReadOnlyData(targetPath) {
  return isWithinRootSet(targetPath, appReadOnlyRoots);
}

function isReadableUserOutput(targetPath) {
  const resolved = tryCanonicalizePath(targetPath);
  if (!resolved) return false;
  if (!isWithinRootSet(resolved, userOutputRoots)) return false;
  if (!SAFE_USER_OUTPUT_EXTENSIONS.has(path.extname(resolved).toLowerCase())) return false;
  try {
    return fs.statSync(resolved).isFile();
  } catch {
    return false;
  }
}

function isReadablePath(targetPath) {
  return isWithinTrustedWorkspace(targetPath)
    || isWithinAppReadOnlyData(targetPath)
    || isReadableUserOutput(targetPath);
}

function restoreTrustedWorkspaceRoot(targetPath) {
  if (typeof targetPath !== "string" || !targetPath.trim() || !trustedRootsFile) {
    return "";
  }
  try {
    const requestedRoot = canonicalizePath(targetPath);
    if (!isSafeWorkspacePath(requestedRoot) || !fs.statSync(requestedRoot).isDirectory()) {
      return "";
    }
    const persistedRoots = readApprovedWorkspaceRoots();
    const approvedRoot = Array.from(persistedRoots).find((root) => isSamePath(root, requestedRoot));
    if (!approvedRoot) {
      appendDesktopLog(`[desktop] rejected non-authoritative restored workspace: ${requestedRoot}`);
      return "";
    }
    approvedWorkspaceRoots.add(approvedRoot);
    trustedWorkspaceRoots.add(approvedRoot);
    return approvedRoot;
  } catch {
    return "";
  }
}

function trustedPathCandidates(targetPath) {
  const rawPath = targetPath.trim();
  if (path.isAbsolute(rawPath)) {
    const canonical = tryCanonicalizePath(rawPath);
    return canonical ? [canonical] : [];
  }
  return Array.from(
    trustedWorkspaceRoots,
    (root) => tryCanonicalizePath(path.resolve(root, rawPath)),
  ).filter(Boolean);
}

const IPC_CAPABILITIES = new Set([
  "minicode:browser:captureScreenshot", "minicode:browser:click", "minicode:browser:discover",
  "minicode:browser:navigate", "minicode:browser:type", "minicode:deepLink:ack",
  "minicode:embeddedBrowser:activate", "minicode:embeddedBrowser:clearSiteData", "minicode:embeddedBrowser:close",
  "minicode:embeddedBrowser:closeConversation",
  "minicode:embeddedBrowser:create", "minicode:embeddedBrowser:list", "minicode:embeddedBrowser:navigate",
  "minicode:embeddedBrowser:getSettings", "minicode:embeddedBrowser:inspect",
  "minicode:embeddedBrowser:runAction", "minicode:embeddedBrowser:setBounds", "minicode:embeddedBrowser:setSettings",
  "minicode:deepLink:open", "minicode:diagnostics:export", "minicode:env:detect",
  "minicode:fs:compareWriteFile", "minicode:fs:createDirectory", "minicode:fs:deletePath",
  "minicode:fs:listTree", "minicode:fs:readFile", "minicode:fs:renamePath",
  "minicode:fs:searchFiles", "minicode:fs:writeFile", "minicode:notify",
  "minicode:openExternal", "minicode:openPath", "minicode:pickDirectory",
  "minicode:pty:ackExit", "minicode:pty:clear", "minicode:pty:kill", "minicode:pty:killConversation", "minicode:pty:list", "minicode:pty:restart",
  "minicode:pty:resize", "minicode:pty:snapshot", "minicode:pty:spawn",
  "minicode:pty:write", "minicode:revealPath", "minicode:startup:getState",
  "minicode:startup:openLogs", "minicode:startup:quit", "minicode:startup:retry",
  "minicode:update:activity", "minicode:update:check", "minicode:update:download", "minicode:update:install",
  "minicode:update:preflight", "minicode:update:status:get",
  "minicode:window:close", "minicode:window:maximize", "minicode:window:minimize",
  "minicode:workspace:trust",
]);

function assertIpcCapability(channel, { logger } = {}) {
  if (typeof channel === "string" && IPC_CAPABILITIES.has(channel)) {
    return channel;
  }
  const writeLog = typeof logger === "function" ? logger : appendDesktopLog;
  writeLog(`[desktop] blocked undeclared IPC capability: ${String(channel || "unknown")}`);
  const error = new Error(`Undeclared IPC capability: ${String(channel || "unknown")}`);
  error.code = "ERR_UNDECLARED_IPC_CAPABILITY";
  throw error;
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

function assertReadablePath(targetPath, label = "Path") {
  if (typeof targetPath !== "string" || !targetPath.trim()) {
    throw new Error(`${label} is required.`);
  }
  const rawPath = targetPath.trim();
  const candidates = trustedPathCandidates(rawPath);
  const absoluteCandidate = path.isAbsolute(rawPath) ? tryCanonicalizePath(rawPath) : null;
  const resolved = candidates.find((candidate) => isReadablePath(candidate) && fs.existsSync(candidate))
    || (absoluteCandidate && isReadablePath(absoluteCandidate) ? absoluteCandidate : null)
    || candidates.find((candidate) => isReadablePath(candidate))
    || path.resolve(rawPath);
  if (!isReadablePath(resolved)) {
    throw new Error(`${label} is outside the trusted workspace, MiniCode read-only data, and safe user output folders.`);
  }
  return resolved;
}

// ---------------------------------------------------------------------------
// Protected write paths
// ---------------------------------------------------------------------------

const PROTECTED_WRITE_FILE_NAMES = new Set([
  ".gitconfig", ".gitmodules", ".mcp.json", ".minicode.json",
  "settings.json", "settings.local.json",
]);
const PROTECTED_WRITE_PATH_PARTS = new Set([".git", ".minicode"]);

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
  canonicalizePath,
  rememberTrustedWorkspaceRoot,
  restoreTrustedWorkspaceRoot,
  isTrustedWorkspaceRootPath,
  isWithinTrustedWorkspace,
  isWithinAppReadOnlyData,
  isReadableUserOutput,
  isReadablePath,
  assertTrustedPath,
  assertReadablePath,
  assertMutableTrustedPath,
  isProtectedWritePath,
  assertIpcCapability,
  IPC_CAPABILITIES,
  systemProtectedPrefixes,
  PROTECTED_WRITE_FILE_NAMES,
  PROTECTED_WRITE_PATH_PARTS,
  SAFE_USER_OUTPUT_EXTENSIONS,
};

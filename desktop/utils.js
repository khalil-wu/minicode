"use strict";

const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const crypto = require("node:crypto");
const { TextDecoder } = require("node:util");
const { app } = require("electron");
const writeFileAtomic = require("write-file-atomic");

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BACKEND_HOST = process.env.MINICODE_BACKEND_HOST || "127.0.0.1";

const BINARY_READ_DENY_EXTENSIONS = new Set([
  ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
  ".zip", ".rar", ".7z", ".tar", ".gz",
  ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
  ".exe", ".dll", ".bin", ".wasm",
]);

const _SENSITIVE_ENV_PREFIXES = [
  "OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_",
  "AWS_SECRET", "GITHUB_TOKEN", "GH_TOKEN", "MINICODE_RUNTIME_TOKEN",
];
const _SENSITIVE_ENV_NAMES = new Set([
  "API_KEY", "SECRET_KEY", "PRIVATE_KEY",
  "ACCESS_TOKEN", "AUTH_TOKEN",
  "DATABASE_URL", "REDIS_URL", "MONGO_URI",
  "MINICODE_RUNTIME_TOKEN", "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
  "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS",
]);

// ---------------------------------------------------------------------------
// Path / URL helpers
// ---------------------------------------------------------------------------

function normalizeWithTrailingSeparator(targetPath) {
  const resolved = path.resolve(targetPath);
  return resolved.endsWith(path.sep) ? resolved : `${resolved}${path.sep}`;
}

function isHttpUrl(target) {
  try {
    const parsed = new URL(String(target));
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function isSamePath(left, right) {
  if (!left || !right) return false;
  const leftPath = path.resolve(left);
  const rightPath = path.resolve(right);
  return process.platform === "win32"
    ? leftPath.toLowerCase() === rightPath.toLowerCase()
    : leftPath === rightPath;
}

// ---------------------------------------------------------------------------
// File helpers
// ---------------------------------------------------------------------------

function isProbablyTextBuffer(buffer, filePath) {
  const extension = path.extname(filePath).toLowerCase();
  if (BINARY_READ_DENY_EXTENSIONS.has(extension)) {
    return false;
  }
  if (buffer.includes(0)) {
    return false;
  }
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    return true;
  } catch {
    return false;
  }
}

function hashFileContent(content) {
  return crypto.createHash("sha256").update(String(content), "utf8").digest("hex");
}

const fileMutationQueues = new Map();
let fileMutationRegistrationQueue = Promise.resolve();

async function fileMutationQueueKey(filePath) {
  const resolved = path.resolve(String(filePath));
  let canonical = resolved;
  try {
    canonical = await fs.promises.realpath(resolved);
  } catch (error) {
    if (error?.code !== "ENOENT" && error?.code !== "ENOTDIR") throw error;
  }
  return process.platform === "win32" ? canonical.toLowerCase() : canonical;
}

/**
 * Serialize mutations touching any of the same files while allowing unrelated
 * paths to proceed in parallel. Registration is globally ordered and keys are
 * canonicalized through realpath, matching Pi's file-mutation-queue contract.
 */
async function withFileMutationQueues(filePaths, operation) {
  const registration = fileMutationRegistrationQueue.then(async () => {
    const keys = [...new Set(await Promise.all(filePaths.map(fileMutationQueueKey)))].sort();
    return keys.map((key) => {
      const currentQueue = fileMutationQueues.get(key) ?? Promise.resolve();
      let releaseNext;
      const nextQueue = new Promise((resolve) => { releaseNext = resolve; });
      const chainedQueue = currentQueue.then(() => nextQueue);
      fileMutationQueues.set(key, chainedQueue);
      return { key, currentQueue, chainedQueue, releaseNext };
    });
  });
  fileMutationRegistrationQueue = registration.then(() => undefined, () => undefined);

  const reservations = await registration;
  await Promise.all(reservations.map((reservation) => reservation.currentQueue));
  try {
    return await operation();
  } finally {
    for (const reservation of reservations) reservation.releaseNext();
    for (const reservation of reservations) {
      if (fileMutationQueues.get(reservation.key) === reservation.chainedQueue) {
        fileMutationQueues.delete(reservation.key);
      }
    }
  }
}

async function withFileMutationQueue(filePath, operation) {
  return withFileMutationQueues([filePath], operation);
}

async function atomicWriteText(filePath, content) {
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true });
  await writeFileAtomic(filePath, String(content), { encoding: "utf8", fsync: true });
}

function atomicWriteTextSync(filePath, content) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileAtomic.sync(filePath, String(content), { encoding: "utf8", fsync: true });
}

async function countDirEntries(dirPath, limit) {
  let count = 0;
  const stack = [dirPath];
  while (stack.length > 0 && count <= limit) {
    const dir = stack.pop();
    let entries;
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      count++;
      if (count > limit) return count;
      if (entry.isDirectory()) {
        stack.push(path.join(dir, entry.name));
      }
    }
  }
  return count;
}

// ---------------------------------------------------------------------------
// General helpers
// ---------------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Environment helpers
// ---------------------------------------------------------------------------

function sanitizedPtyEnv() {
  const env = { ...process.env, TERM: "xterm-256color" };
  for (const key of Object.keys(env)) {
    const upper = key.toUpperCase();
    if (_SENSITIVE_ENV_NAMES.has(upper)) { delete env[key]; continue; }
    if (_SENSITIVE_ENV_PREFIXES.some((p) => upper.startsWith(p))) { delete env[key]; }
    if (/(?:^|_)(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CREDENTIALS?)(?:$|_)/.test(upper)) {
      delete env[key];
    }
  }
  return env;
}

// ---------------------------------------------------------------------------
// Port helpers
// ---------------------------------------------------------------------------

function canUsePort(port, host = BACKEND_HOST) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ port, host }, () => {
      server.close(() => resolve(true));
    });
  });
}

async function findAvailablePort(startPort, host = BACKEND_HOST) {
  const firstPort = Number.isFinite(startPort) && startPort > 0 ? Math.floor(startPort) : 8000;
  for (let port = firstPort; port < firstPort + 100; port += 1) {
    if (await canUsePort(port, host)) {
      return port;
    }
  }
  throw new Error(`No available backend port found from ${firstPort}`);
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function getDesktopLogPath() {
  return path.join(app.getPath("userData"), "desktop.log");
}

function appendDesktopLog(message) {
  try {
    const line = `[${new Date().toISOString()}] ${String(message)}\n`;
    const logPath = getDesktopLogPath();
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    fs.appendFileSync(logPath, line, "utf8");
  } catch {
    // Best-effort logging only.
  }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

module.exports = {
  // Constants
  BACKEND_HOST,
  BINARY_READ_DENY_EXTENSIONS,

  // Path / URL
  normalizeWithTrailingSeparator,
  isHttpUrl,
  isSamePath,

  // File helpers
  isProbablyTextBuffer,
  hashFileContent,
  withFileMutationQueue,
  withFileMutationQueues,
  atomicWriteText,
  atomicWriteTextSync,
  countDirEntries,

  // General
  sleep,

  // Environment
  sanitizedPtyEnv,

  // Port
  canUsePort,
  findAvailablePort,

  // Logging
  getDesktopLogPath,
  appendDesktopLog,
};

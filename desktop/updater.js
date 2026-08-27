"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { atomicWriteTextSync } = require("./utils");

let autoUpdater = null;
let appRef = null;
let getMainWindow = () => null;
let appendDesktopLog = () => {};
let initialized = false;
let healthFile = "";
let currentVersion = "";
let updateHealth = {};
let lastStatus = { status: "idle", sequence: 0 };
let getActivePtySessions = () => [];
let activitySnapshot = null;
let activityReceivedAt = 0;
let activityInvalidReason = "activity.uninitialized";
const retiredActivityRendererIds = new Set();
const pendingActivityRequests = new Map();
let installInFlight = false;
let statusSequence = 0;

const UPDATE_ACTIVITY_FIELDS = [
  "activeTurns",
  "sideChatStreams",
  "pendingPrompts",
  "uploadingAttachments",
  "dirtyEditors",
  "backgroundTasks",
];
const UPDATE_ACTIVITY_MAX_ITEMS = 1000;
const UPDATE_ACTIVITY_MAX_ID_CHARS = 4096;
const UPDATE_ACTIVITY_STALE_MS = 15_000;
const UPDATE_ACTIVITY_REQUEST_TIMEOUT_MS = 3_000;

function normalizeActivityIds(value, field) {
  if (!Array.isArray(value)) {
    throw new Error(`Update activity field ${field} must be an array.`);
  }
  if (value.length > UPDATE_ACTIVITY_MAX_ITEMS) {
    throw new Error(`Update activity field ${field} exceeds ${UPDATE_ACTIVITY_MAX_ITEMS} items.`);
  }
  const normalized = value.map((item) => {
    if (typeof item !== "string") {
      throw new Error(`Update activity field ${field} contains a non-string identifier.`);
    }
    const id = item.trim();
    if (!id || id.length > UPDATE_ACTIVITY_MAX_ID_CHARS || id.includes("\0")) {
      throw new Error(`Update activity field ${field} contains an invalid identifier.`);
    }
    return id;
  });
  return [...new Set(normalized)].sort();
}

function normalizeUpdateActivitySnapshot(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Update activity snapshot must be an object.");
  }
  const revision = Number(value.revision);
  if (!Number.isSafeInteger(revision) || revision <= 0) {
    throw new Error("Update activity revision must be a positive safe integer.");
  }
  const rendererInstanceId = typeof value.rendererInstanceId === "string"
    ? value.rendererInstanceId.trim()
    : "";
  if (!rendererInstanceId || rendererInstanceId.length > 200 || rendererInstanceId.includes("\0")) {
    throw new Error("Update activity renderer instance is invalid.");
  }
  if (typeof value.runtimeReady !== "boolean") {
    throw new Error("Update activity runtimeReady must be a boolean.");
  }
  const normalized = { revision, rendererInstanceId, runtimeReady: value.runtimeReady };
  for (const field of UPDATE_ACTIVITY_FIELDS) {
    normalized[field] = normalizeActivityIds(value[field], field);
  }
  return normalized;
}

function acceptUpdateActivitySnapshot(current, value) {
  const normalized = normalizeUpdateActivitySnapshot(value);
  if (
    current
    && normalized.rendererInstanceId === current.rendererInstanceId
    && normalized.revision <= current.revision
  ) {
    return { accepted: false, snapshot: current };
  }
  return { accepted: true, snapshot: normalized };
}

function normalizeActivePtySessions(value) {
  if (!Array.isArray(value)) {
    throw new Error("Active terminal query did not return an array.");
  }
  return value.map((item) => {
    const sessionId = String(item?.session_id || item?.sessionId || "").trim();
    const conversationId = String(item?.conversation_id || item?.conversationId || "").trim();
    if (!sessionId || !conversationId) {
      throw new Error("Active terminal query returned an unowned session.");
    }
    return { sessionId, conversationId };
  }).sort((left, right) => (
    left.sessionId.localeCompare(right.sessionId)
    || left.conversationId.localeCompare(right.conversationId)
  ));
}

function updateCheck(code, severity, message, details = {}) {
  return {
    code,
    severity,
    message,
    ...(Object.keys(details).length > 0 ? { details } : {}),
  };
}

function buildUpdatePreflight({
  activity = null,
  activePtys = [],
  ptyError = "",
  updateReady = false,
  readyVersion = "",
  installLocked = false,
  activityStale = false,
  activityInvalid = "",
} = {}) {
  const checks = [];
  const version = String(readyVersion || "").trim();
  checks.push(updateReady && version
    ? updateCheck("update.ready", "pass", `Update ${version} is ready to install.`)
    : updateCheck("update.not_ready", "blocking", "Download an update before installing it."));
  checks.push(installLocked
    ? updateCheck("install.locked", "blocking", "Another update installation is already in progress.")
    : updateCheck("install.unlocked", "pass", "No update installation is in progress."));

  let normalizedActivity = null;
  if (activityInvalid) {
    checks.push(updateCheck("activity.invalid", "blocking", "The app activity snapshot is not valid.", { reason: activityInvalid }));
  } else if (!activity) {
    checks.push(updateCheck("activity.unknown", "blocking", "The app activity snapshot is not initialized."));
  } else {
    try {
      normalizedActivity = normalizeUpdateActivitySnapshot(activity);
      checks.push(normalizedActivity.runtimeReady
        ? updateCheck("runtime.ready", "pass", "Runtime session restore is complete.")
        : updateCheck("runtime.not_ready", "blocking", "Runtime session restore is not complete."));
      checks.push(activityStale
        ? updateCheck("activity.stale", "blocking", "The app activity snapshot is stale.")
        : updateCheck("activity.fresh", "pass", "The app activity snapshot is fresh."));
      const activityChecks = [
        ["activeTurns", "turn.running", "Agent turns are still running."],
        ["sideChatStreams", "side_chat.running", "Side chat responses are still running."],
        ["pendingPrompts", "prompt.pending", "Approval or user-input prompts are still pending."],
        ["uploadingAttachments", "attachment.uploading", "Attachments are still uploading."],
        ["dirtyEditors", "editor.dirty", "Editor tabs contain unsaved changes."],
        ["backgroundTasks", "task.running", "Background tasks are still running."],
      ];
      for (const [field, code, message] of activityChecks) {
        const items = normalizedActivity[field];
        checks.push(items.length > 0
          ? updateCheck(code, "blocking", message, { count: items.length, ids: items })
          : updateCheck(`${code}.clear`, "pass", `${message.slice(0, -1)} check passed.`));
      }
    } catch (error) {
      checks.push(updateCheck("activity.invalid", "blocking", error.message));
    }
  }

  let normalizedPtys = [];
  if (ptyError) {
    checks.push(updateCheck("pty.unknown", "blocking", "Active terminal sessions could not be verified.", { error: String(ptyError) }));
  } else {
    try {
      normalizedPtys = normalizeActivePtySessions(activePtys);
      checks.push(normalizedPtys.length > 0
        ? updateCheck("pty.running", "blocking", "Terminal sessions are still running.", { count: normalizedPtys.length, sessions: normalizedPtys })
        : updateCheck("pty.clear", "pass", "No terminal sessions are running."));
    } catch (error) {
      checks.push(updateCheck("pty.invalid", "blocking", error.message));
    }
  }

  const fingerprintPayload = {
    version,
    updateReady: Boolean(updateReady),
    activity: normalizedActivity ? Object.fromEntries(
      Object.entries(normalizedActivity).filter(([key]) => !["revision", "rendererInstanceId"].includes(key)),
    ) : null,
    activePtys: normalizedPtys,
  };
  const fingerprint = crypto
    .createHash("sha256")
    .update(JSON.stringify(fingerprintPayload))
    .digest("hex");
  return {
    allowed: !checks.some((check) => check.severity === "blocking"),
    checks,
    fingerprint,
    version,
  };
}

function emit(status, detail = {}) {
  lastStatus = { status, sequence: ++statusSequence, ...detail };
  const mainWindow = getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("minicode:update:status", lastStatus);
  }
}

function getStatus() {
  return { ...lastStatus };
}

function downloadedUpdateVersion() {
  return updateHealth.status === "downloaded"
    ? String(updateHealth.pending_version || "").trim()
    : "";
}

function validatedFeedUrl(rawFeedUrl) {
  const feedUrl = String(rawFeedUrl || "").trim();
  if (!feedUrl) return "";
  const parsed = new URL(feedUrl);
  const allowInsecure = process.env.MINICODE_ALLOW_INSECURE_UPDATE_FEED === "1";
  const isLoopback = ["127.0.0.1", "localhost", "::1"].includes(parsed.hostname.toLowerCase());
  if (parsed.protocol !== "https:" && !(allowInsecure && isLoopback)) {
    throw new Error("Update feed must use HTTPS; insecure feeds are limited to explicit loopback testing.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Update feed URLs cannot contain credentials.");
  }
  const allowedHosts = String(process.env.MINICODE_UPDATE_ALLOWED_HOSTS || "")
    .split(",")
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
  if (allowedHosts.length > 0 && !allowedHosts.includes(parsed.hostname.toLowerCase())) {
    throw new Error(`Update feed host is not allowlisted: ${parsed.hostname}`);
  }
  return parsed.toString().replace(/\/$/, "");
}

function readHealthState(filePath) {
  try {
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function writeHealthState(filePath, value) {
  if (!filePath) return;
  atomicWriteTextSync(filePath, JSON.stringify(value, null, 2));
}

function safeVersionSegment(version) {
  return String(version || "unknown").replace(/[^A-Za-z0-9._-]/g, "_");
}

function isPathWithin(rootPath, candidatePath) {
  const root = path.resolve(rootPath);
  const candidate = path.resolve(candidatePath);
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function rollbackStoreRoot(app) {
  return path.join(app.getPath("userData"), "update-backups");
}

function expectedRollbackDirectory(app, version) {
  return path.join(rollbackStoreRoot(app), safeVersionSegment(version));
}

async function createRollbackBackup(app) {
  const executablePath = path.resolve(app.getPath("exe"));
  const installRoot = path.dirname(executablePath);
  const backupStore = rollbackStoreRoot(app);
  if (isPathWithin(installRoot, backupStore)) {
    throw new Error("Rollback storage cannot be placed inside the active installation.");
  }

  const backupDirectory = expectedRollbackDirectory(app, currentVersion);
  const executableRelativePath = path.relative(installRoot, executablePath);
  if (!executableRelativePath || executableRelativePath.startsWith("..") || path.isAbsolute(executableRelativePath)) {
    throw new Error("Cannot derive a safe rollback executable path.");
  }
  const backupExecutable = path.resolve(backupDirectory, executableRelativePath);
  if (fs.existsSync(backupExecutable)) {
    return { backupDirectory, executableRelativePath };
  }

  const stagingDirectory = `${backupDirectory}.staging-${process.pid}`;
  if (!isPathWithin(backupStore, stagingDirectory) || !isPathWithin(backupStore, backupDirectory)) {
    throw new Error("Rollback backup path escaped its storage root.");
  }
  await fs.promises.mkdir(backupStore, { recursive: true });
  await fs.promises.rm(stagingDirectory, { recursive: true, force: true });
  await fs.promises.cp(installRoot, stagingDirectory, {
    recursive: true,
    force: true,
    errorOnExist: false,
  });
  if (!fs.existsSync(path.resolve(stagingDirectory, executableRelativePath))) {
    await fs.promises.rm(stagingDirectory, { recursive: true, force: true });
    throw new Error("Rollback backup is incomplete: executable is missing.");
  }
  await fs.promises.rm(backupDirectory, { recursive: true, force: true });
  await fs.promises.rename(stagingDirectory, backupDirectory);
  return { backupDirectory, executableRelativePath };
}

function resolveRollbackExecutable(app, state) {
  const previousVersion = String(state.previous_version || "");
  if (!previousVersion) return "";
  const expectedDirectory = path.resolve(expectedRollbackDirectory(app, previousVersion));
  const storedDirectory = path.resolve(String(state.rollback_backup_dir || expectedDirectory));
  if (storedDirectory !== expectedDirectory) return "";
  const relativeExecutable = String(state.rollback_executable_relative || "");
  if (!relativeExecutable || relativeExecutable.startsWith("..") || path.isAbsolute(relativeExecutable)) return "";
  const executable = path.resolve(expectedDirectory, relativeExecutable);
  if (!isPathWithin(expectedDirectory, executable) || !fs.existsSync(executable)) return "";
  return executable;
}

async function attemptAutomaticRollback(app, state) {
  const executable = resolveRollbackExecutable(app, state);
  if (!executable) return false;
  updateHealth = {
    ...state,
    status: "recovery_required",
    rollback_launch_started_at: new Date().toISOString(),
  };
  writeHealthState(healthFile, updateHealth);
  emit("rollback_launching", {
    version: state.previous_version || "",
    failedVersion: state.pending_version || currentVersion,
  });

  if (typeof app.releaseSingleInstanceLock === "function") {
    app.releaseSingleInstanceLock();
  }
  const environment = { ...process.env };
  delete environment.ELECTRON_RUN_AS_NODE;
  environment.MINICODE_ROLLBACK_FROM_VERSION = String(state.pending_version || currentVersion);

  try {
    const child = spawn(executable, [], {
      cwd: path.dirname(executable),
      detached: true,
      stdio: "ignore",
      env: environment,
      windowsHide: false,
    });
    await new Promise((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", reject);
    });
    child.unref();
    app.quit();
    return true;
  } catch (error) {
    if (typeof app.requestSingleInstanceLock === "function") {
      app.requestSingleInstanceLock();
    }
    appendDesktopLog(`[updater] automatic rollback launch failed: ${error.message}`);
    emit("recovery_required", {
      version: currentVersion,
      previousVersion: state.previous_version || "",
      message: error.message,
    });
    return false;
  }
}

function beginBootHealthCheck(app) {
  healthFile = path.join(app.getPath("userData"), "update-health.json");
  currentVersion = String(app.getVersion() || "");
  updateHealth = readHealthState(healthFile);
  if (
    updateHealth.pending_version === currentVersion
    && ["installing", "booting"].includes(updateHealth.status)
  ) {
    const previousStatus = updateHealth.status;
    const attempts = Number(updateHealth.boot_attempts || 0) + 1;
    updateHealth = {
      ...updateHealth,
      // State, not a guessed retry threshold, is authoritative: installing
      // enters the first boot; seeing booting again means that boot never
      // crossed markHealthy(), so the previous version must be restored.
      status: previousStatus === "booting" ? "recovery_required" : "booting",
      boot_attempts: attempts,
      last_boot_at: new Date().toISOString(),
    };
    writeHealthState(healthFile, updateHealth);
  } else if (
    updateHealth.pending_version
    && updateHealth.previous_version === currentVersion
    && updateHealth.status === "recovery_required"
  ) {
    updateHealth = {
      ...updateHealth,
      status: "rolled_back",
      rolled_back_at: new Date().toISOString(),
    };
    writeHealthState(healthFile, updateHealth);
  }
  return updateHealth;
}

async function init({ app, getMainWindow: nextGetMainWindow, logger, getActivePtySessions: nextGetActivePtySessions } = {}) {
  if (initialized) return false;
  initialized = true;
  appRef = app || null;
  getMainWindow = typeof nextGetMainWindow === "function" ? nextGetMainWindow : getMainWindow;
  appendDesktopLog = typeof logger === "function" ? logger : appendDesktopLog;
  getActivePtySessions = typeof nextGetActivePtySessions === "function"
    ? nextGetActivePtySessions
    : getActivePtySessions;
  if (!app?.isPackaged) return false;

  const bootHealth = beginBootHealthCheck(app);
  if (bootHealth.status === "recovery_required") {
    appendDesktopLog(
      `[updater] version ${currentVersion} failed its previous startup health check; launching ${bootHealth.previous_version || "the previous version"}`,
    );
    emit("recovery_required", {
      version: currentVersion,
      previousVersion: bootHealth.previous_version || "",
    });
    return attemptAutomaticRollback(app, bootHealth);
  }

  let feedUrl;
  try {
    feedUrl = validatedFeedUrl(process.env.MINICODE_UPDATE_FEED_URL);
  } catch (error) {
    appendDesktopLog(`[updater] ${error.message}`);
    emit("error", { message: error.message });
    return false;
  }
  try {
    ({ autoUpdater } = require("electron-updater"));
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    if (feedUrl) {
      autoUpdater.setFeedURL({ provider: "generic", url: feedUrl });
      appendDesktopLog(`[updater] using validated runtime feed override: ${feedUrl}`);
    } else {
      appendDesktopLog("[updater] using bundled signed release feed configuration");
    }
    autoUpdater.on("checking-for-update", () => {
      const readyVersion = downloadedUpdateVersion();
      emit(readyVersion ? "ready" : "checking", readyVersion ? { version: readyVersion } : {});
    });
    autoUpdater.on("update-available", (info) => emit("available", { version: info.version }));
    autoUpdater.on("update-not-available", (info) => {
      const readyVersion = downloadedUpdateVersion();
      emit(readyVersion ? "ready" : "current", { version: readyVersion || info.version });
    });
    autoUpdater.on("download-progress", (progress) => emit("downloading", { percent: progress.percent }));
    autoUpdater.on("update-downloaded", (info) => {
      updateHealth = {
        status: "downloaded",
        previous_version: currentVersion,
        pending_version: String(info.version || ""),
        boot_attempts: 0,
        downloaded_at: new Date().toISOString(),
      };
      writeHealthState(healthFile, updateHealth);
      emit("ready", { version: info.version });
    });
    autoUpdater.on("error", (error) => {
      appendDesktopLog(`[updater] ${error.message}`);
      const readyVersion = downloadedUpdateVersion();
      emit(readyVersion ? "ready" : "error", {
        ...(readyVersion ? { version: readyVersion } : {}),
        message: error.message,
      });
    });

    if (bootHealth.status === "rolled_back") {
      appendDesktopLog(
        `[updater] automatically restored version ${currentVersion} after ${bootHealth.pending_version || "an update"} failed health checks`,
      );
      emit("rolled_back", {
        version: currentVersion,
        failedVersion: bootHealth.pending_version || "",
      });
      return false;
    }

    setTimeout(() => {
      void check().catch((error) => {
        appendDesktopLog(`[updater] automatic check failed: ${error.message}`);
        emit("error", { message: error.message });
      });
    }, 15000);
  } catch (error) {
    appendDesktopLog(`[updater] failed to initialize: ${error.message}`);
    emit("error", { message: error.message });
  }
  return false;
}

async function check() {
  if (!autoUpdater) return false;
  const readyVersion = downloadedUpdateVersion();
  if (readyVersion) {
    emit("ready", { version: readyVersion });
    return true;
  }
  await autoUpdater.checkForUpdates();
  return true;
}

async function download() {
  if (!autoUpdater) return false;
  if (downloadedUpdateVersion()) return true;
  await autoUpdater.downloadUpdate();
  return true;
}

function activityRequestIds(value) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 20) {
    throw new Error("Update activity request acknowledgements must be a bounded array.");
  }
  return [...new Set(value.map((item) => {
    const requestId = typeof item === "string" ? item.trim() : "";
    if (!requestId || requestId.length > 200 || requestId.includes("\0")) {
      throw new Error("Update activity request acknowledgement is invalid.");
    }
    return requestId;
  }))];
}

function settleActivityRequests(requestIds, accepted) {
  for (const requestId of requestIds) {
    const pending = pendingActivityRequests.get(requestId);
    if (!pending) continue;
    pendingActivityRequests.delete(requestId);
    clearTimeout(pending.timer);
    pending.resolve(Boolean(accepted));
  }
}

function invalidateActivity(reason = "activity.invalidated") {
  activityInvalidReason = String(reason || "activity.invalidated");
  activityReceivedAt = 0;
  for (const [requestId, pending] of pendingActivityRequests.entries()) {
    pendingActivityRequests.delete(requestId);
    clearTimeout(pending.timer);
    pending.resolve(false);
  }
}

function updateActivity(value) {
  let requestIds = [];
  try {
    requestIds = activityRequestIds(value?.requestIds);
    const normalized = normalizeUpdateActivitySnapshot(value);
    if (retiredActivityRendererIds.has(normalized.rendererInstanceId)) {
      activityInvalidReason = "activity.retired_renderer";
      settleActivityRequests(requestIds, false);
      return { accepted: false, revision: activitySnapshot?.revision || 0 };
    }
    if (
      activitySnapshot
      && normalized.rendererInstanceId !== activitySnapshot.rendererInstanceId
    ) {
      retiredActivityRendererIds.add(activitySnapshot.rendererInstanceId);
      while (retiredActivityRendererIds.size > 32) {
        retiredActivityRendererIds.delete(retiredActivityRendererIds.values().next().value);
      }
    }
    const result = acceptUpdateActivitySnapshot(activitySnapshot, normalized);
    if (!result.accepted) {
      activityInvalidReason = "activity.stale_revision";
      settleActivityRequests(requestIds, false);
      return { accepted: false, revision: result.snapshot.revision };
    }
    activitySnapshot = result.snapshot;
    activityReceivedAt = Date.now();
    activityInvalidReason = "";
    settleActivityRequests(requestIds, true);
    return { accepted: true, revision: result.snapshot.revision };
  } catch (error) {
    activityInvalidReason = error instanceof Error ? error.message : String(error || "activity.invalid");
    activityReceivedAt = 0;
    settleActivityRequests(requestIds, false);
    return { accepted: false, revision: activitySnapshot?.revision || 0 };
  }
}

async function requestFreshActivitySnapshot() {
  const mainWindow = getMainWindow();
  if (!mainWindow || mainWindow.isDestroyed()) {
    invalidateActivity("activity.renderer_unavailable");
    return false;
  }
  const requestId = crypto.randomUUID();
  const acknowledged = new Promise((resolve) => {
    const timer = setTimeout(() => {
      pendingActivityRequests.delete(requestId);
      activityInvalidReason = "activity.request_timeout";
      activityReceivedAt = 0;
      resolve(false);
    }, UPDATE_ACTIVITY_REQUEST_TIMEOUT_MS);
    pendingActivityRequests.set(requestId, { resolve, timer });
  });
  mainWindow.webContents.send("minicode:update:activity:request", { requestId });
  return acknowledged;
}

function preflightForCurrentState({ ignoreInstallLock = false } = {}) {
  let activePtys = [];
  let ptyError = "";
  try {
    activePtys = getActivePtySessions();
  } catch (error) {
    ptyError = error instanceof Error ? error.message : String(error || "Unknown terminal query error");
  }
  return buildUpdatePreflight({
    activity: activitySnapshot,
    activePtys,
    ptyError,
    updateReady: Boolean(autoUpdater && appRef && downloadedUpdateVersion()),
    readyVersion: downloadedUpdateVersion(),
    installLocked: ignoreInstallLock ? false : installInFlight,
    activityStale: Boolean(
      activityReceivedAt
      && Date.now() - activityReceivedAt > UPDATE_ACTIVITY_STALE_MS
    ),
    activityInvalid: activityInvalidReason,
  });
}

async function preflight() {
  await requestFreshActivitySnapshot();
  return preflightForCurrentState();
}

async function executeInstallTransaction({ fingerprint, readPreflight, createBackup, commitInstall }) {
  const initialPreflight = await readPreflight();
  if (!initialPreflight.allowed || typeof fingerprint !== "string" || fingerprint !== initialPreflight.fingerprint) {
    return { installed: false, reason: "preflight_stale", preflight: initialPreflight };
  }
  const backup = await createBackup();
  const finalPreflight = await readPreflight();
  if (!finalPreflight.allowed || fingerprint !== finalPreflight.fingerprint) {
    return { installed: false, reason: "preflight_changed", preflight: finalPreflight };
  }
  await commitInstall(backup);
  return { installed: true };
}

async function install({ fingerprint } = {}) {
  if (!autoUpdater || !appRef) {
    return { installed: false, reason: "unavailable", preflight: preflightForCurrentState() };
  }
  if (installInFlight) {
    return { installed: false, reason: "install_locked", preflight: preflightForCurrentState() };
  }
  installInFlight = true;
  let installRequested = false;
  try {
    const result = await executeInstallTransaction({
      fingerprint,
      readPreflight: async () => {
        await requestFreshActivitySnapshot();
        return preflightForCurrentState({ ignoreInstallLock: true });
      },
      createBackup: () => createRollbackBackup(appRef),
      commitInstall: async (backup) => {
        updateHealth = {
          ...updateHealth,
          status: "installing",
          previous_version: currentVersion,
          rollback_backup_dir: backup.backupDirectory,
          rollback_executable_relative: backup.executableRelativePath,
          install_started_at: new Date().toISOString(),
        };
        writeHealthState(healthFile, updateHealth);
        autoUpdater.quitAndInstall(false, true);
        installRequested = true;
      },
    });
    return result;
  } catch (error) {
    appendDesktopLog(`[updater] update installation failed: ${error.message}`);
    if (updateHealth.status === "installing") {
      updateHealth = {
        ...updateHealth,
        status: "downloaded",
        install_failed_at: new Date().toISOString(),
        install_error: error.message,
      };
      writeHealthState(healthFile, updateHealth);
    }
    const readyVersion = downloadedUpdateVersion();
    emit(readyVersion ? "ready" : "error", {
      ...(readyVersion ? { version: readyVersion } : {}),
      message: `Update not installed: ${error.message}`,
    });
    return { installed: false, reason: "install_failed", message: error.message };
  } finally {
    if (!installRequested) installInFlight = false;
  }
}

function markHealthy() {
  if (!healthFile || updateHealth.status !== "booting") return false;
  const backupDirectory = String(updateHealth.rollback_backup_dir || "");
  updateHealth = {
    ...updateHealth,
    status: "healthy",
    healthy_at: new Date().toISOString(),
  };
  writeHealthState(healthFile, updateHealth);
  emit("healthy", { version: currentVersion });
  if (appRef && backupDirectory && isPathWithin(rollbackStoreRoot(appRef), backupDirectory)) {
    void fs.promises.rm(backupDirectory, { recursive: true, force: true }).catch((error) => {
      appendDesktopLog(`[updater] failed to clean healthy rollback backup: ${error.message}`);
    });
  }
  return true;
}

module.exports = {
  init,
  check,
  download,
  updateActivity,
  invalidateActivity,
  preflight,
  install,
  markHealthy,
  getStatus,
  validatedFeedUrl,
  readHealthState,
  writeHealthState,
  beginBootHealthCheck,
  expectedRollbackDirectory,
  createRollbackBackup,
  resolveRollbackExecutable,
  attemptAutomaticRollback,
  normalizeUpdateActivitySnapshot,
  acceptUpdateActivitySnapshot,
  normalizeActivePtySessions,
  buildUpdatePreflight,
  executeInstallTransaction,
};

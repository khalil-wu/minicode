"use strict";

const fs = require("node:fs");
const path = require("node:path");
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
let lastStatus = { status: "idle" };

function emit(status, detail = {}) {
  lastStatus = { status, ...detail };
  const mainWindow = getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("minicode:update:status", lastStatus);
  }
}

function getStatus() {
  return { ...lastStatus };
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

async function init({ app, getMainWindow: nextGetMainWindow, logger } = {}) {
  if (initialized) return false;
  initialized = true;
  appRef = app || null;
  getMainWindow = typeof nextGetMainWindow === "function" ? nextGetMainWindow : getMainWindow;
  appendDesktopLog = typeof logger === "function" ? logger : appendDesktopLog;
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
    autoUpdater.autoInstallOnAppQuit = true;
    if (feedUrl) {
      autoUpdater.setFeedURL({ provider: "generic", url: feedUrl });
      appendDesktopLog(`[updater] using validated runtime feed override: ${feedUrl}`);
    } else {
      appendDesktopLog("[updater] using bundled signed release feed configuration");
    }
    autoUpdater.on("checking-for-update", () => emit("checking"));
    autoUpdater.on("update-available", (info) => emit("available", { version: info.version }));
    autoUpdater.on("update-not-available", (info) => emit("current", { version: info.version }));
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
      emit("error", { message: error.message });
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
  await autoUpdater.checkForUpdates();
  return true;
}

async function download() {
  if (!autoUpdater) return false;
  await autoUpdater.downloadUpdate();
  return true;
}

async function install() {
  if (!autoUpdater || !appRef) return false;
  try {
    const backup = await createRollbackBackup(appRef);
    updateHealth = {
      ...updateHealth,
      status: "installing",
      previous_version: currentVersion,
      rollback_backup_dir: backup.backupDirectory,
      rollback_executable_relative: backup.executableRelativePath,
      install_started_at: new Date().toISOString(),
    };
    writeHealthState(healthFile, updateHealth);
  } catch (error) {
    appendDesktopLog(`[updater] refusing to install without a rollback backup: ${error.message}`);
    emit("error", { message: `Update not installed: ${error.message}` });
    return false;
  }
  autoUpdater.quitAndInstall(false, true);
  return true;
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
};

"use strict";

let autoUpdater = null;
let getMainWindow = () => null;
let appendDesktopLog = () => {};
let initialized = false;

function emit(status, detail = {}) {
  const mainWindow = getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("minicode:update:status", { status, ...detail });
  }
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

function init({ app, getMainWindow: nextGetMainWindow, logger } = {}) {
  if (initialized) return;
  initialized = true;
  getMainWindow = typeof nextGetMainWindow === "function" ? nextGetMainWindow : getMainWindow;
  appendDesktopLog = typeof logger === "function" ? logger : appendDesktopLog;
  if (!app?.isPackaged) return;

  let feedUrl;
  try {
    feedUrl = validatedFeedUrl(process.env.MINICODE_UPDATE_FEED_URL);
  } catch (error) {
    appendDesktopLog(`[updater] ${error.message}`);
    return;
  }
  if (!feedUrl) {
    appendDesktopLog("[updater] no update feed configured; updater disabled");
    return;
  }

  try {
    ({ autoUpdater } = require("electron-updater"));
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = true;
    autoUpdater.setFeedURL({ provider: "generic", url: feedUrl });
    autoUpdater.on("checking-for-update", () => emit("checking"));
    autoUpdater.on("update-available", (info) => emit("available", { version: info.version }));
    autoUpdater.on("update-not-available", (info) => emit("current", { version: info.version }));
    autoUpdater.on("download-progress", (progress) => emit("downloading", { percent: progress.percent }));
    autoUpdater.on("update-downloaded", (info) => emit("ready", { version: info.version }));
    autoUpdater.on("error", (error) => {
      appendDesktopLog(`[updater] ${error.message}`);
      emit("error", { message: error.message });
    });
    setTimeout(() => {
      void check().catch((error) => {
        appendDesktopLog(`[updater] automatic check failed: ${error.message}`);
        emit("error", { message: error.message });
      });
    }, 15000);
  } catch (error) {
    appendDesktopLog(`[updater] failed to initialize: ${error.message}`);
  }
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

function install() {
  if (!autoUpdater) return false;
  autoUpdater.quitAndInstall(false, true);
  return true;
}

module.exports = { init, check, download, install, validatedFeedUrl };

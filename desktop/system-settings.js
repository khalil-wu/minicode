"use strict";

const fs = require("node:fs");
const path = require("node:path");

function getStartupStatus(app) {
  if (process.platform !== "win32" || !app.isPackaged) return null;
  return app.getLoginItemSettings().executableWillLaunchAtLogin;
}

function openStartupSettings(shell) {
  if (process.platform !== "win32") throw new Error("启动应用设置仅适用于 Windows。");
  return shell.openExternal("ms-settings:startupapps");
}

function getOpenSourceLicenses(app) {
  return fs.readFileSync(path.join(app.getAppPath(), "THIRD_PARTY_NOTICES.txt"), "utf8");
}

module.exports = { getStartupStatus, openStartupSettings, getOpenSourceLicenses };

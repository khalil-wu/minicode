"use strict";

const os = require("node:os");
const { app, nativeTheme, screen } = require("electron");

const appearances = new Map();

// Source main-C4DYk222 DI/I9/L9/u3e/d3e/l3e. Stock Electron exposes
// setBackgroundMaterial on Windows 11 22H2+, rather than Source's private
// BrowserWindow.isSystemBackdropSupported capability query.
function supportsTranslucency() {
  if (process.platform === "darwin") return true;
  if (process.platform !== "win32") return false;
  return Number(os.release().split(".")[2]) >= 22621
    && app.getGPUFeatureStatus().gpu_compositing === "enabled";
}

function appearanceState(window, options) {
  const supported = supportsTranslucency();
  const dark = options.themeMode === "system" ? nativeTheme.shouldUseDarkColors : options.themeMode === "dark";
  const bounds = window.getBounds();
  const scale = process.platform === "darwin" ? screen.getDisplayMatching(bounds).scaleFactor : 1;
  const largeMacWindow = process.platform === "darwin"
    && Math.max(bounds.width, bounds.height) * scale >= 3840
    && Math.min(bounds.width, bounds.height) * scale >= 2160;
  return {
    supportsTranslucency: supported,
    opaque: options.opaqueWindows || !supported
      || process.platform === "darwin" && (!window.isFocused() || largeMacWindow),
    theme: dark ? "dark" : "light",
  };
}

function apply(window) {
  const record = appearances.get(window);
  const state = appearanceState(window, record.options);
  if (record.state && record.state.opaque === state.opaque
    && record.state.theme === state.theme
    && record.state.supportsTranslucency === state.supportsTranslucency) return state;
  window.setBackgroundColor(state.opaque ? state.theme === "dark" ? "#000000" : "#f9f9f9" : "#00000000");
  if (process.platform === "win32") window.setBackgroundMaterial(state.opaque ? "none" : "mica");
  if (process.platform === "darwin") window.setVibrancy(state.opaque ? null : "menu");
  record.state = state;
  window.webContents.send("minicode:window:appearance", state);
  return state;
}

function set(window, options) {
  if (!["system", "light", "dark"].includes(options.themeMode) || typeof options.opaqueWindows !== "boolean") {
    throw new Error("Invalid window appearance");
  }
  if (!appearances.has(window)) {
    appearances.set(window, { options, state: null });
    const update = () => apply(window);
    window.on("focus", update);
    window.on("blur", update);
    window.on("resize", update);
    nativeTheme.on("updated", update);
    window.once("closed", () => {
      nativeTheme.off("updated", update);
      appearances.delete(window);
    });
  }
  appearances.get(window).options = options;
  // The per-window theme is applied to this window only. Writing
  // nativeTheme.themeSource here is a process-wide override, so one window's
  // explicit dark/light choice repainted every other window's system state.
  return apply(window);
}

function get(window) {
  return appearanceState(window, appearances.get(window)?.options ?? { themeMode: "system", opaqueWindows: false });
}

module.exports = { set, get };

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadAppearance() {
  const sent = [];
  const nativeTheme = {
    themeSource: "system",
    shouldUseDarkColors: false,
    on() {},
    off() {},
  };
  const makeWindow = (id) => ({
    id,
    bounds: { width: 1200, height: 800 },
    isFocused: () => true,
    getBounds() { return this.bounds; },
    on() {},
    once() {},
    setBackgroundColor(value) { this.background = value; },
    setBackgroundMaterial(value) { this.material = value; },
    setVibrancy(value) { this.vibrancy = value; },
    webContents: { send: (channel, state) => sent.push({ id, channel, state }) },
  });
  const moduleBox = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "window-appearance.js"), "utf8"), {
    module: moduleBox,
    require(name) {
      if (name === "node:os") return { release: () => "0.0.0" };
      if (name === "electron") {
        return {
          app: { getGPUFeatureStatus: () => ({ gpu_compositing: "disabled" }) },
          nativeTheme,
          screen: { getDisplayMatching: () => ({ scaleFactor: 1 }) },
        };
      }
      return require(name);
    },
    process: { platform: "linux", env: {} },
    console,
  });
  return { appearance: moduleBox.exports, nativeTheme, sent, makeWindow };
}

test("one window's explicit theme does not repaint another window's system theme", () => {
  const box = loadAppearance();
  const windowA = box.makeWindow("A");
  const windowB = box.makeWindow("B");
  box.appearance.set(windowA, { themeMode: "system", opaqueWindows: false });
  assert.equal(box.appearance.get(windowA).theme, "light");
  box.appearance.set(windowB, { themeMode: "dark", opaqueWindows: true });
  assert.equal(box.appearance.get(windowA).theme, "light");
  assert.equal(box.appearance.get(windowB).theme, "dark");
  // An explicit option must not become a process-wide nativeTheme override.
  assert.equal(box.nativeTheme.themeSource, "system");
  assert.deepEqual(box.sent.map(entry => [entry.id, entry.state.theme]),
    [["A", "light"], ["B", "dark"]]);
});

"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createPopoutWindow } = require("./popout-window");

function fixture() {
  let value = { popoutWindowShortcut: null, popoutWindowProjectlessDefault: false, confettiEnabled: false };
  const callbacks = new Map(), windows = [], registrations = [];
  const source = { isFocused: () => true, isDestroyed: () => false };
  const manager = createPopoutWindow({
    preferences: { getPreferences: () => value, setPreferences: patch => { value = { ...value, ...patch }; return value; } },
    globalShortcut: { register: (shortcut, callback) => { registrations.push(shortcut); if (shortcut === "occupied") return false; callbacks.set(shortcut, callback); return true; }, unregister: shortcut => callbacks.delete(shortcut) },
    getFocusedMainWindow: () => source, getWindowRecord: () => ({ remoteHostId: "ssh-owner", runtime: { apiBaseUrl: "http://remote" } }),
    getWindowMenuState: () => ({ workspaceRoot: "C:/SourceProject", shortcutRecording: false }),
    createNewWindow: async options => {
      const window = new EventEmitter(); window.options = options; window.visible = true; window.focused = true;
      window.draft = "Unsent Popout draft"; window.isDestroyed = () => false; window.isVisible = () => window.visible;
      window.isFocused = () => window.focused; window.hide = () => { window.visible = false; window.focused = false; };
      windows.push(window); return window;
    },
    focusMainWindow: window => { window.visible = true; window.focused = true; }, onError: error => { throw error; },
  });
  return { manager, callbacks, windows, registrations, preferences: () => value };
}

test("the global Popout shortcut is unset by default and failed registration preserves the active shortcut", () => {
  const h = fixture(); h.manager.initialize(); assert.equal(h.callbacks.size, 0);
  h.manager.update({ popoutWindowShortcut: "CommandOrControl+Alt+P" });
  assert.equal(h.callbacks.size, 1);
  assert.throws(() => h.manager.update({ popoutWindowShortcut: "occupied" }), /already in use/);
  assert.equal(h.preferences().popoutWindowShortcut, "CommandOrControl+Alt+P");
  assert.equal(h.callbacks.has("CommandOrControl+Alt+P"), true);
  h.manager.update({ popoutWindowShortcut: null }); assert.equal(h.callbacks.size, 0);
});

test("Popout creates one stable native owner and hiding then waking retains its actual draft and remote owner", async () => {
  const h = fixture();
  await h.manager.toggle();
  assert.equal(h.windows.length, 1);
  assert.deepEqual(h.windows[0].options, { windowOwnerId: "popout", popout: true, initialWorkspaceRoot: "C:/SourceProject",
    restoreLastConversation: true, remoteHostId: "ssh-owner", runtime: { apiBaseUrl: "http://remote" } });
  await h.manager.toggle(); assert.equal(h.windows[0].visible, false);
  await h.manager.toggle(); assert.equal(h.windows[0].visible, true);
  assert.equal(h.windows.length, 1); assert.equal(h.windows[0].draft, "Unsent Popout draft");
  h.manager.update({ popoutWindowShortcut: "Alt+Space", popoutWindowProjectlessDefault: true });
  h.windows[0].emit("closed"); await h.manager.toggle();
  assert.equal(h.windows[1].options.windowOwnerId, "popout"); assert.equal(h.windows[1].options.initialWorkspaceRoot, "");
  h.manager.dispose(); assert.equal(h.callbacks.size, 0);
});

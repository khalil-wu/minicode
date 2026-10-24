"use strict";

function createPopoutWindow({ globalShortcut, preferences, createNewWindow, getFocusedMainWindow,
  getWindowRecord, getWindowMenuState, focusMainWindow, onError }) {
  let window = null;
  let creating = null;
  let registeredShortcut = null;
  let lastWindow = null;
  const toggle = async () => {
    const focused = getFocusedMainWindow();
    const source = focused?.isFocused() ? focused : lastWindow && !lastWindow.isDestroyed() ? lastWindow : focused;
    if (source && getWindowMenuState(source)?.shortcutRecording) return;
    if (window && !window.isDestroyed()) {
      if (window.isVisible() && window.isFocused()) window.hide();
      else focusMainWindow(window);
      return;
    }
    if (creating) return creating;
    const owner = source ? getWindowRecord(source) : null;
    const workspaceRoot = preferences.getPreferences().popoutWindowProjectlessDefault
      ? "" : source && getWindowMenuState(source)?.workspaceRoot || "";
    creating = createNewWindow({ windowOwnerId: "popout", popout: true, initialWorkspaceRoot: workspaceRoot,
      restoreLastConversation: true, ...(owner?.remoteHostId ? { remoteHostId: owner.remoteHostId, runtime: owner.runtime } : {}) })
      .then(value => { window = value; window.once("closed", () => { window = null; }); focusMainWindow(window); return window; })
      .finally(() => { creating = null; });
    return creating;
  };
  const callback = () => { void toggle().catch(onError); };
  return {
    toggle,
    noteFocusedWindow(value) { lastWindow = value; },
    initialize() {
      const shortcut = preferences.getPreferences().popoutWindowShortcut;
      if (shortcut === registeredShortcut) return;
      if (shortcut && !globalShortcut.register(shortcut, callback)) throw new Error(`Popout shortcut is already in use: ${shortcut}`);
      registeredShortcut = shortcut;
    },
    update(patch) {
      const next = patch.popoutWindowShortcut;
      if (next === registeredShortcut) return preferences.setPreferences(patch);
      if (next && !globalShortcut.register(next, callback)) throw new Error(`Popout shortcut is already in use: ${next}`);
      let result;
      try { result = preferences.setPreferences(patch); }
      catch (error) { if (next) globalShortcut.unregister(next); throw error; }
      if (registeredShortcut) globalShortcut.unregister(registeredShortcut);
      registeredShortcut = next;
      return result;
    },
    dispose() { if (registeredShortcut) globalShortcut.unregister(registeredShortcut); registeredShortcut = null; },
  };
}

module.exports = { createPopoutWindow };

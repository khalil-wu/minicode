// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_SHORTCUT_BINDINGS, findShortcutConflict, formatShortcut, isCodeShortcut, nativeMenuAccelerators, nativeMenuBindings, nativeShortcutAction, shortcutFromEvent, toElectronAccelerator } from "./keyboard-shortcuts";
import { initialShortcutBindings, LS } from "../stores/shared-helpers";
import { useAppStore } from "../stores";

afterEach(() => localStorage.removeItem(LS.shortcutBindings));

describe("Source shortcut settings and native accelerator contract", () => {
  it("reserves the Source browser and folder keys without clearing or rerouting the user's draft", () => {
    expect(DEFAULT_SHORTCUT_BINDINGS).toMatchObject({ browserAddress: ["Mod+L"], browserReload: ["Mod+R"], openFolder: ["Mod+O"],
      terminal: ["Mod+Backquote"], leftSidebar: ["Mod+Shift+S", "Mod+B"], promptHistory: [], clearComposer: [], processDetail: [] });
    expect(isCodeShortcut("clearComposer")).toBe(true);
    expect(isCodeShortcut("saveAllFiles")).toBe(true);
    expect(isCodeShortcut("browserReload")).toBe(false);
  });

  it("preserves saved user chords and leaves new conflicting commands unbound", () => {
    localStorage.setItem(LS.shortcutBindings, JSON.stringify({ terminal: "Mod+J", closeEditor: "Mod+W", leftSidebar: "Mod+B", promptHistory: "Alt+H" }));
    expect(initialShortcutBindings()).toMatchObject({ terminal: ["Mod+J"], closeEditor: ["Mod+W"], leftSidebar: ["Mod+B"], promptHistory: ["Alt+H"], bottomPanel: [], closeTab: ["Mod+F4"] });
  });

  it("keeps an explicit unbinding in the complete native map instead of restoring a default", () => {
    const map = nativeMenuAccelerators({ ...DEFAULT_SHORTCUT_BINDINGS, terminal: [], leftSidebar: ["Mod+Shift+Y"] });
    expect(map.terminal).toBeNull();
    expect(map.leftSidebar).toBe("CommandOrControl+Shift+Y");
    expect(map.browserReload).toBe("CommandOrControl+R");
    expect(map.browserAddress).toBe("CommandOrControl+L");
    expect(map.globalSearch).toBe("CommandOrControl+P");
  });

  it("converts recorded physical keys to actual Electron accelerator tokens", () => {
    const chord = shortcutFromEvent(new KeyboardEvent("keydown", { code: "BracketLeft", key: "[", ctrlKey: true, altKey: true }));
    expect(chord).toBe("Mod+Alt+BracketLeft");
    expect(toElectronAccelerator(chord!)).toBe("CommandOrControl+Alt+[");
    expect(toElectronAccelerator("Mod+Backquote")).toBe("CommandOrControl+`");
    expect(toElectronAccelerator("Alt+ArrowRight")).toBe("Alt+Right");
    expect(toElectronAccelerator("Mod+Digit0")).toBe("CommandOrControl+0");
    expect(toElectronAccelerator("Mod+Numpad2")).toBe("CommandOrControl+num2");
    expect(formatShortcut("Mod+BracketLeft")).toBe("Ctrl + [");
    expect(formatShortcut("")).toBe("");
  });

  it("keeps the explicit editor Save All priority while rejecting other duplicate chords", () => {
    expect(findShortcutConflict(DEFAULT_SHORTCUT_BINDINGS, "leftSidebar", "Mod+Shift+S")).toBeNull();
    expect(findShortcutConflict(DEFAULT_SHORTCUT_BINDINGS, "terminal", "Mod+N")?.id).toBe("newConversation");
  });

  it("routes each saved Source binding and respects an explicit removal", () => {
    const sidebar = new KeyboardEvent("keydown", { key: "b", code: "KeyB", ctrlKey: true });
    expect(nativeShortcutAction(sidebar, DEFAULT_SHORTCUT_BINDINGS, false)).toBe("leftSidebar");
    expect(nativeShortcutAction(sidebar, { ...DEFAULT_SHORTCUT_BINDINGS, leftSidebar: [] }, false)).toBeUndefined();
    expect(nativeShortcutAction(sidebar, { ...DEFAULT_SHORTCUT_BINDINGS, leftSidebar: ["Mod+Shift+Y"] }, false)).toBeUndefined();
  });

  it("persists separate bindings through edit, delete, reload and the native menu contract", () => {
    useAppStore.setState({ shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS } });
    useAppStore.getState().setShortcutBinding("newConversation", "Mod+Alt+Y", 1);
    expect(initialShortcutBindings().newConversation).toEqual(["Mod+N", "Mod+Alt+Y"]);
    useAppStore.getState().setShortcutBinding("newConversation", "", 0);
    const restored = initialShortcutBindings();
    expect(restored.newConversation).toEqual(["Mod+Alt+Y"]);
    expect(nativeMenuBindings(restored).newConversation).toEqual(["CommandOrControl+Alt+Y"]);
    expect(nativeMenuAccelerators(restored).newConversation).toBe("CommandOrControl+Alt+Y");
    expect(nativeShortcutAction(new KeyboardEvent("keydown", { key: "y", code: "KeyY", ctrlKey: true, altKey: true }), restored, false)).toBe("newConversation");
    expect(nativeShortcutAction(new KeyboardEvent("keydown", { key: "o", code: "KeyO", ctrlKey: true, shiftKey: true }), restored, false)).toBeUndefined();
  });
});

/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ShortcutsTab } from "./ShortcutsTab";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { useDesktopBehaviorPreferences } from "../hooks/useDesktopBehaviorPreferences";
import { useAppStore } from "../stores";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { pushToast } from "./ToastContainer";

const native = vi.hoisted(() => ({ get: vi.fn(), set: vi.fn(), listeners: new Set<(value: never) => void>() }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => true,
  getDesktopPreferences: native.get, setDesktopPreferences: native.set,
  onDesktopPreferencesChanged: (listener: (value: never) => void) => { native.listeners.add(listener); return () => native.listeners.delete(listener); } }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommand: vi.fn(() => true) }));
const Behavior = () => { useDesktopBehaviorPreferences(); return null; };
const Keyboard = () => { useDesktopBehaviorPreferences(); useKeyboardShortcuts(); return null; };

beforeEach(() => {
  vi.clearAllMocks(); native.listeners.clear();
  let preferences = { primaryNumberShortcutTarget: "tabs", externalBrowserLinkShortcut: "primary", defaultTerminalLocation: "bottom", composerPlainTextMode: false };
  native.get.mockResolvedValue(preferences);
  native.set.mockImplementation(async patch => { preferences = { ...preferences, ...patch }; for (const listener of native.listeners) listener(preferences as never); return preferences; });
  useAppStore.setState({ shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, primaryNumberShortcutTarget: "tabs", appMode: "cowork", conversationId: "keyboard-owner", draft: "Keep keyboard draft",
    pendingConversationSwitchId: null, settingsOpen: false, commandPaletteOpen: false, quickOpenVisible: false, shortcutsHelpOpen: false, liveArtifactsOpen: false, agentEditorOpen: false,
    skillsMarketplaceOpen: false, projectsOpen: false, isStreaming: false });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("persists the two Source preference rows through the real preferences hook and exposes four modifier choices", async () => {
  render(<><Behavior /><ShortcutsTab /></>);
  await waitFor(() => expect(screen.getByRole("button", { name: /^数字快捷键，当前：/ })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: /^数字快捷键，当前：/ }));
  fireEvent.click(screen.getByRole("option", { name: "Ctrl+1–9 选择聊天" }));
  await waitFor(() => expect(useAppStore.getState().primaryNumberShortcutTarget).toBe("sidebar"));
  expect(native.set).toHaveBeenCalledWith({ primaryNumberShortcutTarget: "sidebar" });
  fireEvent.change(screen.getByRole("textbox", { name: "搜索快捷键" }), { target: { value: "浏览器" } });
  fireEvent.click(screen.getByRole("button", { name: /^在默认浏览器中打开网页链接，当前：/ }));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["未分配", "Ctrl+单击", "Alt+单击", "Ctrl+Shift+单击"]);
  fireEvent.click(screen.getByRole("option", { name: "Alt+单击" }));
  await waitFor(() => expect(native.set).toHaveBeenLastCalledWith({ externalBrowserLinkShortcut: "alt" }));
  native.set.mockRejectedValueOnce(new Error("Actual preference write error"));
  fireEvent.click(screen.getByRole("button", { name: /^在默认浏览器中打开网页链接，当前：/ }));
  fireEvent.click(screen.getByRole("option", { name: "Ctrl+Shift+单击" }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Actual preference write error", "error"));
  expect(screen.getByRole("button", { name: /^在默认浏览器中打开网页链接，当前：Alt/ })).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("Keep keyboard draft");
});

it("routes both key groups live and gives custom bindings priority without switching application modes", async () => {
  render(<Keyboard />);
  await waitFor(() => expect(native.get).toHaveBeenCalled());
  const dispatch = vi.spyOn(window, "dispatchEvent");
  fireEvent.keyDown(window, { key: "2", code: "Digit2", ctrlKey: true });
  fireEvent.keyDown(window, { key: "2", code: "Digit2", altKey: true });
  const selected = () => dispatch.mock.calls.map(([event]) => event).filter(event => event.type.startsWith("select-numbered-"));
  expect(selected().map(event => event.type)).toEqual(["select-numbered-tab", "select-numbered-chat"]);
  expect((selected()[1] as CustomEvent).detail).toEqual({ index: 1, conversationId: "keyboard-owner" });
  act(() => useAppStore.setState({ primaryNumberShortcutTarget: "sidebar", shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS, commandPalette: ["Alt+Digit2"] } }));
  fireEvent.keyDown(window, { key: "1", code: "Digit1", ctrlKey: true });
  expect(selected().at(-1)!.type).toBe("select-numbered-chat");
  const before = selected().length;
  fireEvent.keyDown(window, { key: "2", code: "Digit2", altKey: true });
  expect(selected()).toHaveLength(before);
  expect(useAppStore.getState()).toMatchObject({ commandPaletteOpen: true, appMode: "cowork", draft: "Keep keyboard draft" });
});

/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
import { useAppStore } from "../stores";
import { LS } from "../stores/shared-helpers";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { AppearanceTab } from "./AppearanceTab";
import { ShortcutsTab } from "./ShortcutsTab";
import { KeyboardShortcutsHelp } from "./KeyboardShortcutsHelp";

function RuntimeShortcuts() { useKeyboardShortcuts(); return null; }
beforeEach(() => {
  localStorage.clear();
  useAppStore.setState({ themeMode: "dark", textScale: 1, codeTextScale: 1, reducedMotion: false,
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, settingsOpen: true, settingsTab: "shortcuts",
    shortcutsHelpOpen: false, commandPaletteOpen: false, quickOpenVisible: false, skillsMarketplaceOpen: false,
    liveArtifactsOpen: false, agentEditorOpen: false, sendShortcut: "enter" });
});
afterEach(cleanup);

it("uses one radio tab stop and keyboard selection to persist theme and both real text scales", () => {
  render(<AppearanceTab />);
  const theme = screen.getByRole("radiogroup", { name: "应用主题" });
  const dark = within(theme).getByRole("radio", { name: "深色" });
  dark.focus();
  fireEvent.keyDown(dark, { key: "ArrowLeft" });
  expect(useAppStore.getState().themeMode).toBe("light");
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(LS.theme)).toBe("light");
  expect(within(theme).getAllByRole("radio").filter((node) => node.tabIndex === 0)).toHaveLength(1);
  const ui = screen.getByRole("radiogroup", { name: "界面字号" });
  const uiDefault = within(ui).getByRole("radio", { name: "默认" });
  uiDefault.focus(); fireEvent.keyDown(uiDefault, { key: "End" });
  expect(document.documentElement.style.getPropertyValue("--app-text-scale")).toBe("1.12");
  expect(localStorage.getItem(LS.textScale)).toBe("1.12");
  const code = screen.getByRole("radiogroup", { name: "代码字号" });
  const codeDefault = within(code).getByRole("radio", { name: "默认" });
  codeDefault.focus(); fireEvent.keyDown(codeDefault, { key: "Home" });
  expect(document.documentElement.style.getPropertyValue("--code-text-scale")).toBe("0.9");
  expect(localStorage.getItem(LS.codeTextScale)).toBe("0.9");
  fireEvent.click(screen.getByRole("switch", { name: "减少动态效果" }));
  expect(document.documentElement.getAttribute("data-reduced-motion")).toBe("true");
});

it("keeps recording through IME events and distinguishes letters, function keys and modified Delete", () => {
  render(<><ShortcutsTab /><RuntimeShortcuts /></>);
  const edit = screen.getByRole("button", { name: "编辑 命令面板" });
  fireEvent.click(edit);
  fireEvent.keyDown(edit, { key: "K", code: "KeyK", ctrlKey: true, isComposing: true });
  fireEvent.keyDown(edit, { key: "K", code: "KeyK", ctrlKey: true, keyCode: 229 });
  expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Mod+K");
  expect(edit.getAttribute("data-shortcut-recording")).toBe("true");
  fireEvent.keyDown(edit, { key: "F", code: "KeyF" });
  expect(screen.getByRole("alert").textContent).toContain("功能键");
  expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Mod+K");
  fireEvent.keyDown(edit, { key: "F8", code: "F8", shiftKey: true });
  expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Shift+F8");
  fireEvent.click(edit);
  fireEvent.keyDown(edit, { key: "Delete", code: "Delete", ctrlKey: true });
  expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Mod+Delete");
});

it("shows current bindings and send behavior in the actual help dialog", () => {
  useAppStore.setState({ shortcutsHelpOpen: true, sendShortcut: "mod-enter", shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS, commandPalette: "Mod+Shift+K", settings: "" } });
  render(<KeyboardShortcutsHelp />);
  const dialog = screen.getByRole("dialog", { name: "快捷键" });
  expect(within(dialog).getByText("Ctrl/Cmd + Shift + K")).toBeTruthy();
  expect(within(dialog).queryByText("设置")).toBeNull();
  expect(within(dialog).getByText("发送消息").previousElementSibling?.textContent).toBe("Ctrl/Cmd + Enter");
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(useAppStore.getState().shortcutsHelpOpen).toBe(false);
});

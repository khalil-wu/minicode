/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn(() => true) }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
import { useAppStore } from "../stores";
import { LS } from "../stores/shared-helpers";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { AppearanceTab } from "./AppearanceTab";
import { ShortcutsTab } from "./ShortcutsTab";
import { KeyboardShortcutsHelp } from "./KeyboardShortcutsHelp";

function RuntimeShortcuts() { useKeyboardShortcuts(); return null; }
beforeEach(() => {
  localStorage.clear();
  useAppStore.setState({ themeMode: "dark", textScale: 1, codeTextScale: 1, reducedMotion: false,
    workbenchPreferences: { ...defaultWorkbenchPreferences },
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, settingsOpen: true, settingsTab: "shortcuts",
    shortcutsHelpOpen: false, commandPaletteOpen: false, quickOpenVisible: false, skillsMarketplaceOpen: false,
    liveArtifactsOpen: false, agentEditorOpen: false, sendShortcut: "enter" });
});
afterEach(cleanup);

it("persists keyboard-selected theme and exact numeric sizes without font-style controls", () => {
  render(<AppearanceTab />);
  const theme = screen.getByRole("radiogroup", { name: "应用主题" });
  const dark = within(theme).getByRole("radio", { name: "深色" });
  dark.focus();
  fireEvent.keyDown(dark, { key: "ArrowLeft" });
  expect(useAppStore.getState().themeMode).toBe("light");
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(LS.theme)).toBe("light");
  expect(within(theme).getAllByRole("radio").filter((node) => node.tabIndex === 0)).toHaveLength(1);
  expect(screen.getAllByRole("spinbutton")).toHaveLength(3);
  fireEvent.change(screen.getByRole("spinbutton", { name: "界面字号" }), { target: { value: "16" } });
  expect(document.documentElement.style.getPropertyValue("--app-text-scale")).toBe(String(16 / 14));
  expect(localStorage.getItem(LS.textScale)).toBe(String(16 / 14));
  fireEvent.change(screen.getByRole("spinbutton", { name: "正文字号" }), { target: { value: "17" } });
  expect(document.documentElement.style.getPropertyValue("--mc-font-reading")).toBe("17px");
  expect(JSON.parse(localStorage.getItem("minicode.workbench.preferences")!).proseSize).toBe(17);
  fireEvent.change(screen.getByRole("spinbutton", { name: "编辑器字号" }), { target: { value: "18" } });
  expect(document.documentElement.style.getPropertyValue("--code-text-scale")).toBe(String(18 / 14));
  expect(localStorage.getItem(LS.codeTextScale)).toBe(String(18 / 14));
  for (const label of ["界面字体", "正文与 Markdown 字体", "代码字体", "字体连字"]) expect(screen.queryByLabelText(label)).toBeNull();
  expect(screen.queryByRole("radiogroup", { name: "界面字号" })).toBeNull();
  expect(screen.queryByRole("radiogroup", { name: "代码字号" })).toBeNull();
  fireEvent.click(screen.getByRole("switch", { name: "减少动态效果" }));
  expect(document.documentElement.getAttribute("data-reduced-motion")).toBe("true");
});

it("resets sizes separately from editing behavior and retains custom code snippets", () => {
  const snippet = { id: "saved", language: "typescript", prefix: "log", body: "console.log($1)", description: "log" };
  useAppStore.setState({ textScale: 16 / 14, codeTextScale: 18 / 14,
    workbenchPreferences: { ...defaultWorkbenchPreferences, proseSize: 17, tabSize: 2, wordWrap: false, formatOnSave: true,
      aiEnabled: true, aiModel: "saved-model", snippets: [snippet] } });
  render(<AppearanceTab />);

  fireEvent.click(screen.getByRole("button", { name: "恢复默认字号" }));

  expect(useAppStore.getState()).toMatchObject({ textScale: 1, codeTextScale: 1,
    workbenchPreferences: { proseSize: 14, tabSize: 2, wordWrap: false, formatOnSave: true, aiEnabled: true, aiModel: "saved-model", snippets: [snippet] } });
  fireEvent.click(screen.getByText("高级"));
  expect(screen.getByRole("switch", { name: "自动换行" }).getAttribute("aria-checked")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "恢复编辑器默认设置" }));
  expect(useAppStore.getState().workbenchPreferences).toMatchObject({ proseSize: 14, tabSize: 4, wordWrap: true, formatOnSave: false,
    aiEnabled: true, aiModel: "saved-model", snippets: [snippet] });
});

it("retains the unfinished code template when advanced settings are collapsed and reopened", () => {
  render(<AppearanceTab />);
  expect(screen.getByRole("spinbutton", { name: "编辑器字号" })).toBeTruthy();
  const advanced = screen.getByText("高级");
  const disclosure = advanced.closest("details") as HTMLDetailsElement;
  expect(disclosure.open).toBe(false);
  fireEvent.click(advanced);
  expect(disclosure.open).toBe(true);
  expect(screen.getByRole("switch", { name: "AI 行内预测" })).toBeTruthy();
  fireEvent.click(screen.getByText("自定义代码模板 · 0"));
  fireEvent.change(screen.getByRole("textbox", { name: "模板前缀" }), { target: { value: "draft" } });
  fireEvent.change(screen.getByRole("textbox", { name: "模板正文" }), { target: { value: "console.log($1)" } });
  fireEvent.click(advanced);
  expect(disclosure.open).toBe(false);
  fireEvent.click(advanced);
  expect(disclosure.open).toBe(true);
  expect(screen.getByRole("textbox", { name: "模板前缀" })).toHaveProperty("value", "draft");
  expect(screen.getByRole("textbox", { name: "模板正文" })).toHaveProperty("value", "console.log($1)");
  expect(useAppStore.getState().workbenchPreferences.snippets).toHaveLength(0);
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

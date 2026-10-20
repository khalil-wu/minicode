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
import { installAppThemeTokens } from "../../test/app-theme-tokens";

let removeThemeTokens: () => void;

function RuntimeShortcuts() { useKeyboardShortcuts(); return null; }
beforeEach(() => {
  removeThemeTokens = installAppThemeTokens();
  localStorage.clear();
  useAppStore.setState({ themeMode: "dark", resolvedTheme: "dark", textScale: 1, codeTextScale: 1, codeFontBase: 12, reducedMotion: false,
    reducedMotionPreference: "system", pointerCursors: false, diffMarkerStyle: "color",
    workbenchPreferences: { ...defaultWorkbenchPreferences },
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, settingsOpen: true, settingsTab: "shortcuts",
    shortcutsHelpOpen: false, commandPaletteOpen: false, quickOpenVisible: false, skillsMarketplaceOpen: false,
    liveArtifactsOpen: false, agentEditorOpen: false, sendShortcut: "enter" });
  useAppStore.getState().setThemeMode("dark");
});
afterEach(() => { cleanup(); removeThemeTokens(); });

it("persists keyboard-selected theme and Source numeric sizes without modifying saved prose size", () => {
  render(<AppearanceTab />);
  const theme = screen.getByRole("radiogroup", { name: "应用主题" });
  const dark = within(theme).getByRole("radio", { name: "深色" });
  dark.focus();
  fireEvent.keyDown(dark, { key: "ArrowLeft" });
  expect(useAppStore.getState().themeMode).toBe("light");
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(LS.theme)).toBe("light");
  expect(within(theme).getAllByRole("radio").filter((node) => node.tabIndex === 0)).toHaveLength(1);
  expect(screen.getByRole("spinbutton", { name: "代码字号" }).closest("details")).toHaveProperty("open", false);
  fireEvent.click(screen.getByText("高级").closest("summary")!);
  expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
  fireEvent.change(screen.getByRole("spinbutton", { name: "界面字号" }), { target: { value: "16" } });
  expect(document.documentElement.style.getPropertyValue("--app-text-scale")).toBe(String(16 / 14));
  expect(localStorage.getItem(LS.textScale)).toBe(String(16 / 14));
  expect(screen.queryByRole("spinbutton", { name: "正文字号" })).toBeNull();
  expect(useAppStore.getState().workbenchPreferences.proseSize).toBe(defaultWorkbenchPreferences.proseSize);
  fireEvent.change(screen.getByRole("spinbutton", { name: "代码字号" }), { target: { value: "18" } });
  expect(document.documentElement.style.getPropertyValue("--code-text-scale")).toBe(String(18 / 12));
  expect(localStorage.getItem(LS.codeTextScale)).toBe(String(18 / 12));
  expect(screen.getByRole("button", { name: /^浅色界面字体样式/ })).toHaveProperty("disabled", true);
  expect(screen.queryByLabelText("字体连字")).toBeNull();
  expect(screen.queryByRole("radiogroup", { name: "界面字号" })).toBeNull();
  expect(screen.queryByRole("radiogroup", { name: "代码字号" })).toBeNull();
  fireEvent.click(within(screen.getByRole("radiogroup", { name: "减少动态效果" })).getByRole("radio", { name: "开" }));
  expect(document.documentElement.getAttribute("data-reduced-motion")).toBe("true");
});

it("resets sizes separately from editing behavior and retains custom code snippets", () => {
  const snippet = { id: "saved", language: "typescript", prefix: "log", body: "console.log($1)", description: "log" };
  useAppStore.setState({ textScale: 16 / 14, codeTextScale: 18 / 14,
    workbenchPreferences: { ...defaultWorkbenchPreferences, proseSize: 17, tabSize: 2, wordWrap: false, formatOnSave: true,
      aiEnabled: true, aiModel: "saved-model", snippets: [snippet] } });
  render(<AppearanceTab />);

  fireEvent.click(screen.getByText("高级").closest("summary")!);

  fireEvent.click(screen.getByRole("button", { name: "恢复默认", exact: true }));

  expect(useAppStore.getState()).toMatchObject({ textScale: 1, codeTextScale: 1,
    workbenchPreferences: { proseSize: 17, tabSize: 2, wordWrap: false, formatOnSave: true, aiEnabled: true, aiModel: "saved-model", snippets: [snippet] } });
  expect(screen.getByRole("switch", { name: "自动换行" }).getAttribute("aria-checked")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "恢复编辑器默认设置" }));
  expect(useAppStore.getState().workbenchPreferences).toMatchObject({ proseSize: 17, tabSize: 4, wordWrap: true, formatOnSave: false,
    aiEnabled: true, aiModel: "saved-model", snippets: [snippet] });
});

it("retains the unfinished code template when advanced settings are collapsed and reopened", () => {
  render(<AppearanceTab />);
  expect(screen.getByRole("spinbutton", { name: "代码字号" }).closest("details")).toHaveProperty("open", false);
  const advanced = screen.getByText("高级").closest("summary")!;
  const disclosure = advanced.closest("details") as HTMLDetailsElement;
  expect(disclosure.open).toBe(false);
  fireEvent.click(advanced);
  expect(disclosure.open).toBe(true);
  expect(screen.getByRole("spinbutton", { name: "代码字号" })).toBeTruthy();
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
  fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
  const recorder = screen.getByRole("textbox", { name: "编辑 命令面板" });
  fireEvent.keyDown(recorder, { key: "K", code: "KeyK", ctrlKey: true, isComposing: true });
  fireEvent.keyDown(recorder, { key: "K", code: "KeyK", ctrlKey: true, keyCode: 229 });
  expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+K"]);
  expect(recorder.getAttribute("data-shortcut-recording")).toBe("true");
  fireEvent.keyDown(recorder, { key: "F", code: "KeyF" });
  expect(screen.getByRole("alert").textContent).toContain("功能键");
  fireEvent.keyDown(recorder, { key: "F8", code: "F8", shiftKey: true });
  expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Shift+F8"]);
  fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
  fireEvent.keyDown(screen.getByRole("textbox", { name: "编辑 命令面板" }), { key: "Delete", code: "Delete", ctrlKey: true });
  expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+Delete"]);
});

it("captures a search chord without starting a chat, and edits or cancels each binding independently", () => {
  useAppStore.setState({ conversationId: "preserved-owner", draft: "preserved draft", appMode: "cowork" });
  render(<><ShortcutsTab /><RuntimeShortcuts /></>);
  expect(screen.queryByRole("button", { name: "恢复默认" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "按键搜索" }));
  const search = screen.getByRole("textbox", { name: "请按快捷键搜索" });
  fireEvent.keyDown(search, { key: "n", code: "KeyN", ctrlKey: true });
  expect(search).toHaveProperty("value", "Ctrl + N");
  expect(screen.queryByText("命令面板")).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "preserved-owner", draft: "preserved draft" });
  fireEvent.click(screen.getByRole("button", { name: "按键搜索" }));
  expect(screen.getByRole("textbox", { name: "搜索快捷键" })).toHaveProperty("value", "");
  fireEvent.click(screen.getAllByRole("button", { name: "编辑 新建任务" })[1]);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(useAppStore.getState().shortcutBindings.newConversation).toEqual(["Mod+N", "Mod+Shift+O"]);
  fireEvent.click(screen.getAllByRole("button", { name: "编辑 新建任务" })[1]);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "编辑 新建任务" }), { key: "y", code: "KeyY", ctrlKey: true, shiftKey: true });
  expect(useAppStore.getState().shortcutBindings.newConversation).toEqual(["Mod+N", "Mod+Shift+Y"]);
  fireEvent.click(screen.getAllByRole("button", { name: "删除 新建任务 快捷键" })[0]);
  expect(useAppStore.getState().shortcutBindings.newConversation).toEqual(["Mod+Shift+Y"]);
  expect(JSON.parse(localStorage.getItem(LS.shortcutBindings)!)).toMatchObject({ newConversation: ["Mod+Shift+Y"] });
});

it("shows current bindings and send behavior in the actual help dialog", () => {
  useAppStore.setState({ shortcutsHelpOpen: true, sendShortcut: "mod-enter", shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS, commandPalette: ["Mod+Shift+K"], settings: [] } });
  render(<KeyboardShortcutsHelp />);
  const dialog = screen.getByRole("dialog", { name: "快捷键" });
  expect(within(dialog).getByText("Ctrl + Shift + K")).toBeTruthy();
  expect(within(dialog).queryByText("设置")).toBeNull();
  expect(within(dialog).getByText("发送消息").nextElementSibling?.textContent).toBe("Ctrl/Cmd + Enter");
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(useAppStore.getState().shortcutsHelpOpen).toBe(false);
});

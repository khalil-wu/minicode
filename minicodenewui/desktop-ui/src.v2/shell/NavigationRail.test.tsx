/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NavigationRail } from "./NavigationRail";
import { useAppStore } from "../stores";
import type { DesktopPetState } from "../desktop/runtime";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { closeAutomations, getAutomationsPageState } from "../lib/automations-navigation";

const fixture = vi.hoisted(() => {
  const api = { getState: vi.fn(), update: vi.fn(), onStateChanged: vi.fn() };
  return { available: true, api, getState: api.getState, update: api.update, changed: api.onStateChanged, openLink: vi.fn(), toast: vi.fn() };
});
vi.mock("../desktop/runtime", async original => ({ ...await original<typeof import("../desktop/runtime")>(), desktop: () => fixture.available ? { pets: fixture.api } : null }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null, error: "", update: vi.fn() }) }));
vi.mock("../lib/open-preferred-target", () => ({ openPreferredWebLink: fixture.openLink }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: fixture.toast }));
const listeners = new Set<(state: DesktopPetState) => void>();
let state: DesktopPetState;
const publish = (next: DesktopPetState) => { state = next; for (const listener of listeners) listener(next); };
const openProfile = () => fireEvent.click(screen.getByRole("button", { name: "打开个人资料菜单" }));

beforeEach(() => {
  closeAutomations();
  vi.clearAllMocks(); fixture.available = true; listeners.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  state = { pets: [], selectedPetId: "codex", size: 112, petVisible: true, miniOpen: false, petDirectory: "C:/fixture/pets", reducedMotion: false,
    mutedConversationIds: [], activity: { running: false, waiting: false, failed: false, review: false, tasks: [] }, issues: [] };
  fixture.getState.mockReset().mockImplementation(async () => state);
  fixture.changed.mockImplementation(listener => { listeners.add(listener); return () => listeners.delete(listener); });
  fixture.update.mockReset().mockImplementation(async patch => { publish({ ...state, ...patch, miniOpen: patch.petVisible }); return state; });
  useAppStore.setState({ conversationId: "original-owner", pendingConversationSwitchId: null, settingsOpen: false, settingsTab: "general", appMode: "code",
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
    skillsMarketplaceOpen: false, skillsMarketplaceTab: "skills", skillsMarketplaceReturnTarget: "app", projectsOpen: false,
    leftSidebarWidth: 0, leftSidebarExpandedWidth: 336, draft: "Original chat draft", workingDirectory: "C:/original-workspace",
    messages: [{ id: "original-message", role: "user", content: "Original message", timestamp: 1 }], conversationWorkbenchStates: {},
    editorTabs: [{ id: "edited-file", path: "src/edited.ts", content: "unsaved source", original: "disk source", loading: false }] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("routes every global rail destination without borrowing the owner, expanding the chat sidebar or clearing unsaved content", () => {
  fixture.available = false;
  render(<NavigationRail />);
  const original = useAppStore.getState();
  const rail = screen.getByRole("navigation", { name: "应用导航" });
  expect(within(rail).getAllByRole("button")).toHaveLength(5);
  expect(screen.queryByRole("button", { name: "代码", exact: true })).toBeNull();
  act(() => useAppStore.setState({ settingsOpen: true }));
  fireEvent.click(within(rail).getByRole("button", { name: "首页" }));
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, appMode: "cowork", leftSidebarWidth: 0 });
  fireEvent.click(within(rail).getByRole("button", { name: "定时任务" }));
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, settingsTab: "general" });
  expect(getAutomationsPageState().open).toBe(true);
  expect(within(rail).getByRole("button", { name: "定时任务" }).getAttribute("aria-current")).toBe("page");
  expect(within(rail).getByRole("button", { name: "首页" }).getAttribute("aria-current")).toBeNull();
  fireEvent.click(within(rail).getByRole("button", { name: "插件" }));
  expect(getAutomationsPageState().open).toBe(false);
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins" });
  act(() => useAppStore.setState({ skillsMarketplaceTab: "skills", skillsMarketplaceReturnTarget: "settings" }));
  fireEvent.click(within(rail).getByRole("button", { name: "插件" }));
  expect(useAppStore.getState()).toMatchObject({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins", skillsMarketplaceReturnTarget: "app" });
  fireEvent.click(within(rail).getByRole("button", { name: "探索" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "项目", exact: true }));
  expect(useAppStore.getState()).toMatchObject({ projectsOpen: true, skillsMarketplaceOpen: false, leftSidebarWidth: 0 });
  const current = useAppStore.getState();
  expect(current.conversationId).toBe(original.conversationId); expect(current.workingDirectory).toBe(original.workingDirectory);
  expect(current.draft).toBe(original.draft); expect(current.messages).toBe(original.messages);
  expect(current.editorTabs).toBe(original.editorTabs); expect(current.conversationWorkbenchStates).toBe(original.conversationWorkbenchStates);
});

it("keeps profile settings and the nested Help menu on the real source destinations", () => {
  fixture.available = false;
  render(<NavigationRail />); openProfile();
  fireEvent.click(screen.getByRole("menuitem", { name: /^设置/ }));
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "general" });
  openProfile();
  const help = screen.getByRole("menuitem", { name: "帮助", exact: true });
  expect(help.querySelector('[data-menu-source-icon="help"]')).toBeTruthy();
  expect(help.querySelector('[data-menu-source-icon="submenu"]')).toBeTruthy();
  help.focus(); fireEvent.keyDown(help, { key: "ArrowRight" });
  expect(screen.getByRole("menuitem", { name: "新增内容" })).toBe(document.activeElement);
  expect(screen.getByRole("menuitem", { name: "新增内容" }).querySelector('[data-menu-source-icon="whats-new"]')).toBeTruthy();
  expect(screen.getByRole("menuitem", { name: "键盘快捷键" }).querySelector('[data-menu-source-icon="shortcuts"]')).toBeTruthy();
  expect(screen.getByRole("menuitem", { name: "使用指南" }).querySelector('[data-menu-source-icon="guide"]')).toBeTruthy();
  fireEvent.click(screen.getByRole("menuitem", { name: "使用指南" }));
  expect(fixture.openLink).toHaveBeenCalledExactlyOnceWith("https://learn.chatgpt.com/docs/quickstart");
  expect(useAppStore.getState().draft).toBe("Original chat draft");
});

it("shows the configured Settings shortcut and updates it while the profile is open", () => {
  fixture.available = false;
  useAppStore.setState({ shortcutBindings: { ...useAppStore.getState().shortcutBindings, settings: ["Ctrl+Alt+S"] } });
  render(<NavigationRail />); openProfile();
  const settings = screen.getByRole("menuitem", { name: /^设置/ });
  expect(settings.textContent).toBe("设置Ctrl + Alt + S");
  expect(settings.querySelector('[data-menu-source-icon="settings"]')).toBeTruthy();
  act(() => useAppStore.setState({ shortcutBindings: { ...useAppStore.getState().shortcutBindings, settings: [] } }));
  expect(settings.textContent).toBe("设置");
});

it("shows and hides the pet from actual native window state rather than the saved visible preference alone", async () => {
  render(<NavigationRail />); openProfile();
  fireEvent.click(await screen.findByRole("menuitem", { name: "显示虚拟宠物" }));
  expect(fixture.update).toHaveBeenCalledExactlyOnceWith({ petVisible: true });
  openProfile();
  fireEvent.click(await screen.findByRole("menuitem", { name: "隐藏虚拟宠物" }));
  expect(fixture.update).toHaveBeenNthCalledWith(2, { petVisible: false });
  openProfile(); expect(await screen.findByRole("menuitem", { name: "显示虚拟宠物" })).toBeTruthy();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "original-owner", draft: "Original chat draft", leftSidebarWidth: 0 });
});

it("lets actual pet state events win over a late initial read and keeps updates subscribed while the menu is closed", async () => {
  let finish!: (next: DesktopPetState) => void;
  fixture.getState.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const old = state;
  const view = render(<NavigationRail />);
  act(() => publish({ ...state, miniOpen: true }));
  await act(async () => finish(old));
  openProfile(); expect(screen.getByRole("menuitem", { name: "隐藏虚拟宠物" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("menuitem", { name: "隐藏虚拟宠物" }), { key: "Escape" });
  act(() => publish({ ...state, miniOpen: false, petVisible: false }));
  openProfile(); expect(screen.getByRole("menuitem", { name: "显示虚拟宠物" })).toBeTruthy();
  view.unmount(); expect(listeners.size).toBe(0);
});

it("reports a native pet update error without claiming the pet became visible", async () => {
  fixture.update.mockRejectedValueOnce(new Error("Native pet fixture could not show"));
  render(<NavigationRail />); openProfile();
  fireEvent.click(await screen.findByRole("menuitem", { name: "显示虚拟宠物" }));
  await waitFor(() => expect(fixture.toast).toHaveBeenCalledWith("Error: Native pet fixture could not show", "error"));
  openProfile(); expect(screen.getByRole("menuitem", { name: "显示虚拟宠物" })).toBeTruthy();
  expect(screen.queryByRole("menuitem", { name: "隐藏虚拟宠物" })).toBeNull();
});

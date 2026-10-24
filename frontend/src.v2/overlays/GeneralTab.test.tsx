/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GeneralTab } from "./GeneralTab";
import { useAppStore } from "../stores";

const fixture = vi.hoisted(() => ({ update: vi.fn(), updates: vi.fn(), preferences: {
  defaultTaskDirectory: "C:/AuthoredTasks", taskDirectoryOverride: "C:/AuthoredTasks",
  fileOpenTarget: "minicode", fileOpenTargets: [{ id: "minicode", label: "MiniCode" }], terminalShell: "system", terminalShellTargets: [],
  localeOverride: "zh-CN", confirmBeforeClose: "keyboardOnly", showFullAccess: true, showBottomPanel: true,
  autoArchiveAfterDays: 0, localHistoryCompression: false, notificationSound: "none", turnCompletionNotifications: "never",
  permissionNotifications: false, questionNotifications: false, showContextWindowUsage: false,
  defaultWorkspaceFullView: false, popoutWindowShortcut: null as string | null, popoutWindowProjectlessDefault: false, confettiEnabled: false,
} }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: fixture.preferences, error: "", update: fixture.update }) }));
vi.mock("../hooks/usePluginsFeature", () => ({ usePluginsFeature: () => ({ enabled: false, locked: false, error: "" }), setPluginsFeatureEnabled: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => true, desktop: () => ({ platformInfo: { platform: "linux" }, updates: { getStatus: fixture.updates } }) }));
beforeEach(() => {
  vi.clearAllMocks(); fixture.update.mockResolvedValue(undefined);
  fixture.preferences.popoutWindowShortcut = null;
  fixture.preferences.defaultWorkspaceFullView = false; fixture.preferences.popoutWindowProjectlessDefault = false; fixture.preferences.confettiEnabled = false;
  useAppStore.setState({ draft: "Original unsent draft", attachments: [], composerPlainTextMode: false, sendShortcut: "enter", followUpBehavior: "queue" });
});
afterEach(cleanup);

it("keeps Source's conditional directory Reset while removing the three Source-absent General entry types", () => {
  render(<GeneralTab />);
  expect(screen.getByRole("button", { name: "重置" })).toBeTruthy();
  expect(screen.queryByRole("radiogroup", { name: "默认终端位置" })).toBeNull();
  expect(screen.queryByLabelText("新任务默认权限")).toBeNull();
  expect(screen.queryByText("高级")).toBeNull();
  expect(screen.queryByRole("button", { name: "检查更新" })).toBeNull();
  expect(screen.queryByText("从其他 AI 应用导入")).toBeNull();
  expect(fixture.updates).not.toHaveBeenCalled();
  expect(fixture.preferences.taskDirectoryOverride).toBe("C:/AuthoredTasks");
  expect(useAppStore.getState().draft).toBe("Original unsent draft");
});

it("writes the actual full-view, standalone Popout and Toys preference fields without firing a celebration", () => {
  render(<GeneralTab />);
  fireEvent.click(screen.getByRole("switch", { name: "默认使用完整视图" }));
  fireEvent.click(screen.getByRole("switch", { name: "默认将弹出窗口设为独立聊天" }));
  fireEvent.click(screen.getByRole("switch", { name: "彩纸礼炮" }));
  expect(fixture.update.mock.calls).toEqual([[{ defaultWorkspaceFullView: true }], [{ popoutWindowProjectlessDefault: true }], [{ confettiEnabled: true }]]);
  expect(document.querySelector("canvas")).toBeNull();
});

it("records the native global accelerator, supports clear and withdraws recording when its page hides", async () => {
  const view = render(<GeneralTab />);
  fireEvent.click(screen.getByRole("button", { name: "为“弹出窗口快捷键”设置快捷键" }));
  await act(async () => fireEvent.keyDown(window, { key: "p", code: "KeyP", ctrlKey: true, altKey: true }));
  expect(fixture.update).toHaveBeenCalledExactlyOnceWith({ popoutWindowShortcut: "CommandOrControl+Alt+P" });
  fixture.preferences.popoutWindowShortcut = "CommandOrControl+Alt+P";
  view.rerender(<GeneralTab />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "清除弹出窗口快捷键" })));
  expect(fixture.update).toHaveBeenLastCalledWith({ popoutWindowShortcut: null });
  fireEvent.click(screen.getByRole("button", { name: "为“弹出窗口快捷键”设置快捷键" }));
  view.rerender(<GeneralTab active={false} />);
  fireEvent.keyDown(window, { key: "x", code: "KeyX", ctrlKey: true });
  expect(fixture.update).toHaveBeenCalledTimes(2);
});

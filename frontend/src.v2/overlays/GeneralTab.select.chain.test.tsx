/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GeneralTab } from "./GeneralTab";
import { initialSendShortcut } from "../stores/shared-helpers";
import { useAppStore } from "../stores";
import type { DesktopPreferences } from "../desktop/runtime";

const native = vi.hoisted(() => ({ preferences: null as DesktopPreferences | null, update: vi.fn(), openExternal: vi.fn() }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ ...native, error: "" }) }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => true, desktop: () => ({ openExternal: native.openExternal, updates: {
  getStatus: () => Promise.resolve({ status: "unavailable" }), onStatus: () => () => {},
} }) }));

beforeEach(() => {
  native.preferences = {
    fileOpenTarget: "vscode", fileOpenTargetPerPath: { "C:/original-owner": "cursor" },
    fileOpenTargets: [
      { id: "vscode", label: "VS Code", kind: "editor", icon: "apps/vscode.svg" },
      { id: "visualStudio", label: "Visual Studio", kind: "editor", icon: "apps/visual-studio.png" },
      { id: "system", label: "Default app", kind: "systemDefault", icon: "apps/file-explorer.png", hidden: true },
    ],
    terminalShell: "powershell", terminalShellTargets: [{ id: "powershell", label: "PowerShell" }, { id: "cmd", label: "Command Prompt" }],
    defaultTaskDirectory: "C:/QA/long-real-original-task-directory", defaultPermissionMode: "confirm", showFullAccess: true,
    localeOverride: "zh-CN", systemLocale: "en-US", confirmBeforeClose: "keyboardOnly", turnCompletionNotifications: "onlyWhenUnfocused",
    permissionNotifications: true, questionNotifications: true, urlOpenTarget: "external-browser", localUrlOpenTarget: "in-app-browser",
    showFullUrl: false, annotationScreenshotsMode: "always",
  };
  native.update.mockReset();
  native.openExternal.mockReset();
  useAppStore.setState({ draft: "ORIGINAL UNSENT DRAFT", allowedRemoteImageDomains: [], permissionMode: "confirm", sendShortcut: "enter", followUpBehavior: "queue" });
});
afterEach(cleanup);

it("renders the real discovered app icon and full label and changes only the global preference", () => {
  const view = render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "默认文件打开位置，当前：VS Code" });
  expect(trigger.closest(".mc-select-menu")?.classList.contains("settings-select")).toBe(true);
  const selectedIcon = trigger.querySelector("img")!.getAttribute("src");
  fireEvent.click(trigger);
  const listbox = screen.getByRole("listbox", { name: "默认文件打开位置" });
  expect([...listbox.querySelectorAll('[role="option"]')].map(option => option.textContent)).toEqual(["VS Code", "Visual Studio", "Default app"]);
  expect(screen.getByRole("option", { name: "VS Code" }).querySelector("img")!.getAttribute("src")).toBe(selectedIcon);
  fireEvent.click(screen.getByRole("option", { name: "Visual Studio" }));
  expect(native.update).toHaveBeenCalledExactlyOnceWith({ fileOpenTarget: "visualStudio" });
  expect(native.preferences!.fileOpenTargetPerPath).toEqual({ "C:/original-owner": "cursor" });
  native.preferences = { ...native.preferences!, fileOpenTarget: "visualStudio" };
  view.rerender(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  expect(screen.getByRole("button", { name: "默认文件打开位置，当前：Visual Studio" }).querySelector("img")).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("ORIGINAL UNSENT DRAFT");
});

it("publishes the actual available shell id and retains complete preference labels", () => {
  render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "集成终端 Shell，当前：PowerShell" }));
  fireEvent.click(screen.getByRole("option", { name: "Command Prompt" }));
  expect(native.update).toHaveBeenCalledExactlyOnceWith({ terminalShell: "cmd" });
  const completion = screen.getByRole("button", { name: "任务完成通知，当前：仅在应用未聚焦时" });
  expect(completion.closest(".mc-select-menu")?.classList.contains("settings-select")).toBe(true);
  fireEvent.click(completion);
  fireEvent.click(screen.getByRole("option", { name: "始终" }));
  expect(native.update).toHaveBeenLastCalledWith({ turnCompletionNotifications: "always" });
  expect(useAppStore.getState().draft).toBe("ORIGINAL UNSENT DRAFT");
});

it("keeps every General preference on the shared menu and preserves the real task path title", () => {
  const view = render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  expect(view.container.querySelectorAll(".settings-row-control > .mc-select-menu.settings-select")).toHaveLength(9);
  expect(view.container.querySelectorAll(".settings-row-control > select:not(.mc-select-native-proxy)")).toHaveLength(0);
  const taskPath = view.container.querySelector(".settings-general-task-path")!;
  expect(taskPath.textContent).toBe(native.preferences!.defaultTaskDirectory);
  expect(taskPath.getAttribute("title")).toBe(native.preferences!.defaultTaskDirectory);
  expect(screen.queryByRole("button", { name: /智能体环境/ })).toBeNull();
});

it("changes the actual composer and content choices through the same menu without clearing the draft", () => {
  const remotePolicy = vi.fn();
  render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={remotePolicy} />);
  fireEvent.click(screen.getByRole("button", { name: "发送快捷键，当前：按 Enter 键" }));
  fireEvent.click(screen.getByRole("option", { name: "总是使用 ^ + Enter" }));
  expect(useAppStore.getState().sendShortcut).toBe("mod-enter");
  fireEvent.click(screen.getByRole("button", { name: "跟进行为，当前：排队" }));
  fireEvent.click(screen.getByRole("option", { name: "引导" }));
  expect(useAppStore.getState().followUpBehavior).toBe("steer");
  fireEvent.click(screen.getByText("高级", { selector: "summary" }));
  fireEvent.click(screen.getByRole("button", { name: "远程 Markdown 图片，当前：询问" }));
  fireEvent.click(screen.getByRole("option", { name: "阻止" }));
  expect(remotePolicy).toHaveBeenCalledExactlyOnceWith("block");
  expect(useAppStore.getState().draft).toBe("ORIGINAL UNSENT DRAFT");
});

it("selects Source's multiline Enter mode and restores that exact preference without changing the current draft", () => {
  render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "发送快捷键，当前：按 Enter 键" }));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual([
    "按 Enter 键", "^ + Enter 用于多行提示", "总是使用 ^ + Enter",
  ]);
  fireEvent.click(screen.getByRole("option", { name: "^ + Enter 用于多行提示" }));
  expect(screen.getByRole("button", { name: "发送快捷键，当前：^ + Enter 用于多行提示" })).toBeTruthy();
  expect(initialSendShortcut()).toBe("cmdIfMultiline");
  expect(useAppStore.getState()).toMatchObject({ draft: "ORIGINAL UNSENT DRAFT", followUpBehavior: "queue", permissionMode: "confirm" });
});

it("confirms only Full access visibility and leaves cancellation, the active permission mode, and drafts intact", () => {
  native.preferences = { ...native.preferences!, showFullAccess: false, urlOpenTarget: "in-app-browser" };
  render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  const toggle = screen.getByRole("switch", { name: "完整访问权限" });
  fireEvent.click(toggle);
  let dialog = screen.getByRole("dialog", { name: "要启用完整访问权限吗？" });
  expect(native.update).not.toHaveBeenCalled();
  expect(toggle.getAttribute("aria-checked")).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(native.update).not.toHaveBeenCalled();
  fireEvent.click(toggle);
  dialog = screen.getByRole("dialog", { name: "要启用完整访问权限吗？" });
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(native.update).not.toHaveBeenCalled();
  fireEvent.click(toggle);
  dialog = screen.getByRole("dialog", { name: "要启用完整访问权限吗？" });
  fireEvent.click(within(dialog).getByRole("link", { name: "了解更多" }));
  expect(native.openExternal).toHaveBeenCalledExactlyOnceWith("https://developers.openai.com/codex/concepts/sandboxing#how-you-control-it");
  const confirm = screen.getByRole("button", { name: "确认" });
  expect(confirm.getAttribute("type")).toBe("submit");
  expect(confirm.querySelector("svg")).toBeTruthy();
  fireEvent.click(confirm);
  expect(native.update).toHaveBeenCalledExactlyOnceWith({ showFullAccess: true });
  expect(useAppStore.getState().permissionMode).toBe("confirm");
  expect(useAppStore.getState().draft).toBe("ORIGINAL UNSENT DRAFT");
});

it("hides Full access immediately without a confirmation or changing the mandatory default option", () => {
  render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);
  const mandatory = screen.getByRole("switch", { name: "始终显示默认权限" }) as HTMLButtonElement;
  expect(mandatory.disabled).toBe(true);
  expect(mandatory.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("switch", { name: "完整访问权限" }));
  expect(native.update).toHaveBeenCalledExactlyOnceWith({ showFullAccess: false });
  expect(screen.queryByRole("dialog")).toBeNull();
});

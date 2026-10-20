/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GeneralTab } from "./GeneralTab";
import { useAppStore } from "../stores";
import type { DesktopPreferences, DesktopPreferencesPatch } from "../desktop/runtime";

const mocks = vi.hoisted(() => ({ toast: vi.fn(), command: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../hooks/usePluginsFeature", () => ({ usePluginsFeature: () => ({ enabled: false, locked: false, error: "" }), setPluginsFeatureEnabled: vi.fn() }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommandAwaitResult: mocks.command }));

const runtimeDefault = "C:/RuntimeDocuments/MiniCode";
const selectedFolder = "C:/Selected task data";
function bindNative(initialOverride: string | null = null) {
  let current: DesktopPreferences = {
    defaultTaskDirectory: initialOverride ?? runtimeDefault, taskDirectoryOverride: initialOverride,
    fileOpenTarget: "minicode", fileOpenTargetPerPath: {}, fileOpenTargets: [{ id: "minicode", label: "MiniCode", kind: "editor", icon: "apps/minicode.svg" }],
    terminalShell: "powershell", terminalShellTargets: [{ id: "powershell", label: "PowerShell" }],
    defaultPermissionMode: "confirm", showFullAccess: true, showContextWindowUsage: false, showBottomPanel: true,
    defaultTerminalLocation: "bottom", composerPlainTextMode: false, localeOverride: "zh-CN", systemLocale: "zh-CN",
    confirmBeforeClose: "keyboardOnly", turnCompletionNotifications: "onlyWhenUnfocused", permissionNotifications: true, questionNotifications: true,
    autoArchiveAfterDays: 0, localHistoryCompression: false, notificationSound: "default", urlOpenTarget: "external-browser", localUrlOpenTarget: "in-app-browser",
    showFullUrl: false, annotationScreenshotsMode: "always",
  };
  const listeners = new Set<(preferences: DesktopPreferences) => void>();
  const publish = (override: string | null) => {
    current = { ...current, defaultTaskDirectory: override ?? runtimeDefault, taskDirectoryOverride: override };
    for (const listener of listeners) listener(current);
    return current;
  };
  const get = vi.fn(async () => current);
  const set = vi.fn(async (patch: DesktopPreferencesPatch) => {
    current = { ...current, ...patch, defaultTaskDirectory: current.defaultTaskDirectory };
    return publish("defaultTaskDirectory" in patch ? patch.defaultTaskDirectory! : current.taskDirectoryOverride);
  });
  const chooseTaskDirectory = vi.fn<() => Promise<DesktopPreferences | null>>(async () => null);
  window.__MINICODE_RUNTIME__ = { desktop: {
    platformInfo: { isDesktop: true, platform: "win32", arch: "x64" },
    preferences: { get, set, chooseTaskDirectory, getStartupStatus: async () => false,
      onChanged: (listener: (preferences: DesktopPreferences) => void) => { listeners.add(listener); return () => listeners.delete(listener); } },
    updates: { getStatus: async () => ({ status: "unavailable" }), onStatus: () => () => {} },
  } } as never;
  return { get, set, chooseTaskDirectory, publish };
}
const taskRow = () => within(screen.getByText("无项目任务文件夹").closest<HTMLElement>(".settings-row")!);
const renderGeneral = () => render(<GeneralTab remoteImagePolicy="ask" setRemoteImagePolicy={vi.fn()} />);

beforeEach(() => {
  vi.clearAllMocks();
  mocks.command.mockResolvedValue({ level: "success", command: "conversation.create", data: {} });
  useAppStore.setState({ draft: "保留未发送草稿", attachments: [{ id: "keep-image", name: "draft.png", type: "image/png", size: 12, status: "ready" }],
    workingDirectory: "C:/ExistingWorkspace", workspaceComposerDrafts: {}, conversationId: "existing-chat", conversations: [{ id: "existing-chat", title: "Existing chat", updatedAt: "2026-10-20", workspaceRoot: "C:/ExistingWorkspace" }],
    pendingConversationCreateId: null, pendingConversationSwitchId: null, pendingComposerConversationAdoption: null,
    settingsOpen: true, settingsTab: "general", appMode: "cowork", isConnected: true, isStreaming: false, permissionMode: "confirm", runtimeSession: null });
});
afterEach(() => { cleanup(); delete window.__MINICODE_RUNTIME__; });

it("shows Reset only for an explicit selection and sends new projectless chats to the restored effective default", async () => {
  const native = bindNative();
  native.chooseTaskDirectory.mockImplementation(async () => native.publish(selectedFolder));
  renderGeneral();
  await screen.findByText("无项目任务文件夹");
  expect(taskRow().queryByRole("button", { name: "重置" })).toBeNull();
  expect(taskRow().getByTitle(runtimeDefault).textContent).toBe(runtimeDefault);
  fireEvent.click(taskRow().getByRole("button", { name: "更改" }));
  const reset = await taskRow().findByRole("button", { name: "重置" });
  expect(taskRow().getByTitle(selectedFolder).textContent).toBe(selectedFolder);
  expect(reset.classList.contains("settings-general-task-reset")).toBe(true);
  fireEvent.click(reset);
  await waitFor(() => expect(taskRow().queryByRole("button", { name: "重置" })).toBeNull());
  expect(native.set).toHaveBeenCalledExactlyOnceWith({ defaultTaskDirectory: null });
  expect(taskRow().getByTitle(runtimeDefault).textContent).toBe(runtimeDefault);
  expect(useAppStore.getState()).toMatchObject({ draft: "保留未发送草稿", attachments: [{ id: "keep-image" }], workingDirectory: "C:/ExistingWorkspace", conversationId: "existing-chat" });
  await act(async () => { expect(await useAppStore.getState().createConversation({ bindWorkspace: false })).toBe(true); });
  expect(mocks.command.mock.calls[0][0]).toMatchObject({ type: "conversation.create", projectless_base: runtimeDefault, workspace_root: undefined });
});

it("keeps the original selection and draft when the pending native chooser is canceled", async () => {
  const native = bindNative(selectedFolder);
  let finish!: (value: DesktopPreferences | null) => void;
  native.chooseTaskDirectory.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  renderGeneral();
  await screen.findByText("无项目任务文件夹");
  const change = taskRow().getByRole("button", { name: "更改" });
  fireEvent.click(change);
  expect(change).toHaveProperty("disabled", true);
  expect(change.getAttribute("aria-busy")).toBe("true");
  fireEvent.click(change);
  expect(native.chooseTaskDirectory).toHaveBeenCalledTimes(1);
  await act(async () => finish(null));
  expect(change).toHaveProperty("disabled", false);
  expect(taskRow().getByTitle(selectedFolder).textContent).toBe(selectedFolder);
  expect(taskRow().getByRole("button", { name: "重置" })).toBeTruthy();
  expect(native.set).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("保留未发送草稿");
});

it.each(["重置", "更改"])("retains the displayed path and reports the actual %s error without clearing user input", async (button) => {
  const native = bindNative(selectedFolder);
  native.set.mockRejectedValueOnce(new Error("preference disk failure"));
  native.chooseTaskDirectory.mockRejectedValueOnce(new Error("native chooser failure"));
  renderGeneral();
  await screen.findByText("无项目任务文件夹");
  fireEvent.click(taskRow().getByRole("button", { name: button }));
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledExactlyOnceWith(button === "重置" ? "preference disk failure" : "native chooser failure", "error"));
  expect(taskRow().getByTitle(selectedFolder).textContent).toBe(selectedFolder);
  expect(taskRow().getByRole("button", { name: "重置" })).toBeTruthy();
  expect(taskRow().getByRole("button", { name: "更改" })).toHaveProperty("disabled", false);
  expect(useAppStore.getState()).toMatchObject({ draft: "保留未发送草稿", attachments: [{ id: "keep-image" }], workingDirectory: "C:/ExistingWorkspace", conversationId: "existing-chat" });
});

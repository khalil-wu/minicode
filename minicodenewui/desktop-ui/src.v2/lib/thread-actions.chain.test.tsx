// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { send: vi.fn(), prompt: vi.fn(), confirm: vi.fn(), copy: vi.fn(), toast: vi.fn() };
});
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: mocks.send,
  commandResultSucceeded: (result: { level: string }) => result.level === "success",
}));
vi.mock("../overlays/DialogService", () => ({ showPrompt: mocks.prompt, showConfirm: mocks.confirm }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("./clipboard", () => ({ copyText: mocks.copy }));

import { useAppStore } from "../stores";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { useDesktopEvents } from "../hooks/useDesktopEvents";
import { DEFAULT_SHORTCUT_BINDINGS } from "./keyboard-shortcuts";
import { captureThreadActionOwner } from "./thread-actions";
import { LS } from "../stores/shared-helpers";

const KeyboardHarness = () => { useKeyboardShortcuts(); return null; };
const DesktopHarness = () => { useDesktopEvents(); return null; };
const key = (letter: string, modifier: "alt" | "shift" = "alt", target: EventTarget = window) => {
  const event = new KeyboardEvent("keydown", { key: letter, code: `Key${letter.toUpperCase()}`, ctrlKey: true,
    altKey: modifier === "alt", shiftKey: modifier === "shift", bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
};
const sixKeys = (target: EventTarget = window) => {
  key("r", "alt", target); key("p", "alt", target); key("a", "shift", target);
  key("l", "alt", target); key("c", "shift", target); key("s", "alt", target);
};

beforeEach(() => {
  vi.clearAllMocks();
  window.__MINICODE_RUNTIME__ = undefined;
  mocks.send.mockResolvedValue({ type: "command.result", level: "success", data: {} });
  mocks.prompt.mockResolvedValue("Renamed A");
  mocks.confirm.mockResolvedValue(true);
  mocks.copy.mockResolvedValue(true);
  useAppStore.setState({
    shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS }, appMode: "cowork", conversationId: "A", pendingConversationSwitchId: null,
    conversations: [
      { id: "A", title: "Chat A", workspaceRoot: "C:/project/A", executionCwd: "C:/execution/A", updatedAt: "today" },
      { id: "B", title: "Chat B", workspaceRoot: "C:/project/B", updatedAt: "today" },
    ],
    workingDirectory: "C:/visible", permissionMode: "confirm", isConnected: false, isStreaming: true, conversationStreaming: { A: true },
    messages: [{ id: "a-user", role: "user", content: "Inspect A", timestamp: 1 }], conversationMessages: {}, conversationHistoryPages: {},
    draft: "Retain main draft", sideChats: {}, activeSideChatId: null, sideChatOpen: false,
    rightPanelOpen: false, rightStackTab: "tasks", rightStackTabLocked: false,
    commandPaletteOpen: false, settingsOpen: false, shortcutsHelpOpen: false, quickOpenVisible: false, skillsMarketplaceOpen: false,
    projectsOpen: false, liveArtifactsOpen: false, agentEditorOpen: false,
  });
});
afterEach(() => { cleanup(); window.__MINICODE_RUNTIME__ = undefined; localStorage.removeItem(LS.shortcutBindings); });

const expectSixActions = async () => {
  await waitFor(() => expect(mocks.send).toHaveBeenCalledTimes(3));
  expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.rename", conversation_id: "A", title: "Renamed A" }, "conversation.rename");
  expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.pin.set", conversation_id: "A", pinned: true }, "conversation.pin.set");
  expect(mocks.send).toHaveBeenCalledWith({ type: "conversation.archive", conversation_id: "A", archived: true, archive_navigation: "home" }, "conversation.archive");
  expect(mocks.confirm).toHaveBeenCalledOnce();
  expect(mocks.copy).toHaveBeenCalledWith("minicode://conversation?conversation_id=A", "复制深度链接");
  expect(mocks.copy).toHaveBeenCalledWith("C:/execution/A", "工作区路径");
  const state = useAppStore.getState();
  expect(state.sideChats[state.activeSideChatId!]).toMatchObject({ parentConversationId: "A", workspaceRoot: "C:/execution/A", permissionMode: "confirm" });
  expect(state).toMatchObject({ conversationId: "A", draft: "Retain main draft", isStreaming: true });
  expect(state.conversations[0]).toMatchObject({ title: "Chat A" });
  expect(state.conversations[0].pinned).not.toBe(true);
  expect(state.conversations[0].archived).not.toBe(true);
};

describe("Source chat shortcut execution", () => {
  it.each(["chat", "cowork", "code"] as const)("executes all six configured actions in %s through real commands and captured owners", async appMode => {
    useAppStore.setState({ appMode });
    render(<KeyboardHarness />);
    sixKeys();
    await expectSixActions();
    expect(useAppStore.getState().appMode).toBe(appMode);
  });

  it("keeps the rename prompt attached to A when selection changes before submission", async () => {
    let finish!: (title: string) => void;
    mocks.prompt.mockReturnValueOnce(new Promise<string>(resolve => { finish = resolve; }));
    render(<KeyboardHarness />);
    key("r");
    expect(mocks.prompt).toHaveBeenCalledWith(expect.objectContaining({ defaultValue: "Chat A", allowEmpty: true }));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project/B", draft: "Retain B draft" }));
    await act(async () => finish("Renamed A later"));
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.rename", conversation_id: "A", title: "Renamed A later" }, "conversation.rename");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "Retain B draft" });
  });

  it("confirms the captured running owner and archives A after B becomes active", async () => {
    let finish!: (confirmed: boolean) => void;
    mocks.confirm.mockReturnValueOnce(new Promise<boolean>(resolve => { finish = resolve; }));
    render(<KeyboardHarness />);
    key("a", "shift");
    expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "停止并归档此聊天？", danger: true }));
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project/B", draft: "Retain B draft", isStreaming: false }));
    await act(async () => finish(true));
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.archive", conversation_id: "A", archived: true, archive_navigation: "home" }, "conversation.archive");
    expect(useAppStore.getState()).toMatchObject({ conversationId: "B", draft: "Retain B draft" });
  });

  it("does not send an archive command after the running-owner confirmation is cancelled", async () => {
    mocks.confirm.mockResolvedValueOnce(false);
    render(<KeyboardHarness />);
    key("a", "shift");
    await act(async () => {});
    expect(mocks.send).not.toHaveBeenCalled();
    expect(useAppStore.getState()).toMatchObject({ isStreaming: true, draft: "Retain main draft" });
  });

  it.each([{ pendingConversationSwitchId: "B" }, { conversationId: "missing" }])("does not act without a canonical owner: %j", state => {
    useAppStore.setState(state);
    render(<KeyboardHarness />);
    sixKeys();
    expect(mocks.prompt).not.toHaveBeenCalled();
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(mocks.copy).not.toHaveBeenCalled();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(useAppStore.getState().sideChats).toEqual({});
  });

  it("uses edits and explicit removals immediately without invoking the old chord", () => {
    render(<KeyboardHarness />);
    act(() => useAppStore.getState().setShortcutBinding("copyDeeplink", "Mod+Alt+Y"));
    expect(key("l").defaultPrevented).toBe(false);
    expect(mocks.copy).not.toHaveBeenCalled();
    expect(key("y").defaultPrevented).toBe(true);
    expect(mocks.copy).toHaveBeenCalledExactlyOnceWith("minicode://conversation?conversation_id=A", "复制深度链接");
    act(() => useAppStore.getState().setShortcutBinding("copyDeeplink", ""));
    expect(key("y").defaultPrevented).toBe(false);
    expect(mocks.copy).toHaveBeenCalledOnce();
    expect(useAppStore.getState().draft).toBe("Retain main draft");
  });

  it("creates a new side owner each time and retains the previous side draft and permission", () => {
    render(<KeyboardHarness />);
    key("s");
    const first = useAppStore.getState().activeSideChatId!;
    act(() => {
      useAppStore.getState().setSideChatDraft(first, "Retain side A draft");
      useAppStore.setState({ conversationId: "B", workingDirectory: "C:/project/B", permissionMode: "bypass" });
    });
    key("s");
    const state = useAppStore.getState();
    expect(state.activeSideChatId).not.toBe(first);
    expect(state.sideChats[first]).toMatchObject({ parentConversationId: "A", draft: "Retain side A draft", permissionMode: "confirm" });
    expect(state.sideChats[state.activeSideChatId!]).toMatchObject({ parentConversationId: "B", workspaceRoot: "C:/project/B", permissionMode: "bypass" });
    expect(state.draft).toBe("Retain main draft");
  });

  it("retains archived and child-chat action availability from the existing header", () => {
    useAppStore.setState({ conversations: [{ ...useAppStore.getState().conversations[0], archived: true }] });
    render(<KeyboardHarness />);
    sixKeys();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.prompt).not.toHaveBeenCalled();
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(useAppStore.getState().sideChats).toEqual({});
    expect(mocks.copy).toHaveBeenCalledTimes(2);
    act(() => useAppStore.setState({ conversations: [{ ...useAppStore.getState().conversations[0], archived: false, conversationType: "side_chat" }] }));
    key("p");
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it("reports real command rejection and transport errors without optimistic pin or archive state", async () => {
    mocks.send.mockResolvedValueOnce({ type: "command.result", level: "error", message: "pin denied" });
    render(<KeyboardHarness />);
    key("p");
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("pin denied", "error"));
    mocks.send.mockRejectedValueOnce(new Error("connection lost"));
    key("a", "shift");
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("connection lost", "error"));
    expect(useAppStore.getState().conversations[0].pinned).not.toBe(true);
    expect(useAppStore.getState().conversations[0].archived).not.toBe(true);
    expect(useAppStore.getState().draft).toBe("Retain main draft");
  });

  it("keeps thread mutations out of modal inputs and active shortcut recording", () => {
    useAppStore.setState({ settingsOpen: true, settingsTab: "shortcuts" });
    render(<><button data-shortcut-recording="true">Record</button><KeyboardHarness /></>);
    sixKeys();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.prompt).not.toHaveBeenCalled();
    expect(mocks.copy).not.toHaveBeenCalled();
    expect(useAppStore.getState().sideChats).toEqual({});
  });
});

describe("desktop thread action consumers", () => {
  it("dispatches each actual native command once and uses the edited and removed bindings", () => {
    const execute = vi.fn(async () => true);
    window.__MINICODE_RUNTIME__ = { desktop: { menu: { execute } } } as never;
    render(<KeyboardHarness />);
    sixKeys();
    expect(execute.mock.calls).toEqual([["renameThread"], ["toggleThreadPin"], ["archiveThread"], ["copyDeeplink"], ["copyWorkingDirectory"], ["openSideChat"]]);
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.copy).not.toHaveBeenCalled();
    act(() => useAppStore.getState().setShortcutBinding("copyDeeplink", "Mod+Alt+Y"));
    expect(key("l").defaultPrevented).toBe(false);
    expect(key("y").defaultPrevented).toBe(true);
    expect(execute).toHaveBeenLastCalledWith("copyDeeplink");
    expect(execute).toHaveBeenCalledTimes(7);
    act(() => useAppStore.getState().setShortcutBinding("copyDeeplink", ""));
    expect(key("y").defaultPrevented).toBe(false);
    expect(execute).toHaveBeenCalledTimes(7);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "A", draft: "Retain main draft" });
  });

  it.each(["settings", "dialog"])("does not dispatch native chat keys from an active %s context", context => {
    const execute = vi.fn(async () => true);
    window.__MINICODE_RUNTIME__ = { desktop: { menu: { execute } } } as never;
    if (context === "settings") useAppStore.setState({ settingsOpen: true });
    render(<>{context === "dialog" && <div role="dialog"><input aria-label="Dialog input" /></div>}<KeyboardHarness /></>);
    const target = document.querySelector<HTMLInputElement>("input");
    target?.focus();
    sixKeys(target ?? window);
    expect(execute).not.toHaveBeenCalled();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.prompt).not.toHaveBeenCalled();
    expect(mocks.copy).not.toHaveBeenCalled();
  });

  it("routes all six desktop events through the same owner and actual action chain", async () => {
    render(<DesktopHarness />);
    for (const action of ["rename-thread", "toggle-thread-pin", "archive-thread", "copy-deeplink", "copy-working-directory", "open-side-chat"]) {
      fireEvent(window, new Event(action));
    }
    await expectSixActions();
  });

  it.each(["settings", "dialog"])("keeps all six desktop actions out of an active %s context", context => {
    if (context === "settings") useAppStore.setState({ settingsOpen: true });
    render(<>{context === "dialog" && <div role="dialog"><input aria-label="Dialog input" /></div>}<DesktopHarness /></>);
    document.querySelector<HTMLInputElement>("input")?.focus();
    for (const action of ["rename-thread", "toggle-thread-pin", "archive-thread", "copy-deeplink", "copy-working-directory", "open-side-chat"]) {
      fireEvent(window, new Event(action));
    }
    expect(mocks.send).not.toHaveBeenCalled();
    expect(mocks.prompt).not.toHaveBeenCalled();
    expect(mocks.confirm).not.toHaveBeenCalled();
    expect(mocks.copy).not.toHaveBeenCalled();
    expect(useAppStore.getState().sideChats).toEqual({});
  });

  it("ignores pending owners and removes the thread action listeners on unmount", () => {
    const { unmount } = render(<DesktopHarness />);
    act(() => useAppStore.setState({ pendingConversationSwitchId: "B" }));
    fireEvent(window, new Event("copy-deeplink"));
    fireEvent(window, new Event("open-side-chat"));
    expect(mocks.copy).not.toHaveBeenCalled();
    expect(useAppStore.getState().sideChats).toEqual({});
    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    unmount();
    fireEvent(window, new Event("copy-deeplink"));
    expect(mocks.copy).not.toHaveBeenCalled();
  });

  it("captures persisted running metadata even after renderer streaming has stopped", () => {
    useAppStore.setState({ isStreaming: false, conversationStreaming: {},
      conversations: [{ ...useAppStore.getState().conversations[0], sessionStatus: "waiting" }] });
    expect(captureThreadActionOwner()).toMatchObject({ id: "A", workspaceRoot: "C:/execution/A", isRunning: true, permissionMode: "confirm" });
  });
});

/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { bridge, popup, send, awaitResult, openFolder, windowControls, windowStateListeners } = vi.hoisted(() => {
  const windowStateListeners = new Set<(state: { isMaximized: boolean }) => void>();
  return {
    bridge: { enabled: false, showBottomPanel: true },
    popup: vi.fn(async (_key: string) => undefined),
    send: vi.fn(() => true),
    awaitResult: vi.fn(async (_command: unknown, command: string) => ({
      type: "command.result", command, level: "success", message: "", data: {},
    })),
    openFolder: vi.fn(async () => true),
    windowStateListeners,
    windowControls: {
      getState: vi.fn(async () => ({ isMaximized: false })),
      onStateChanged: vi.fn((callback: (state: { isMaximized: boolean }) => void) => {
        windowStateListeners.add(callback);
        return vi.fn(() => windowStateListeners.delete(callback));
      }),
      minimize: vi.fn(), maximize: vi.fn(), close: vi.fn(),
    },
  };
});

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }) });
});
vi.mock("../desktop/runtime", async (original) => ({
  ...await original<typeof import("../desktop/runtime")>(),
  desktop: () => bridge.enabled ? { menu: { popup }, windowControls } : null,
  isDesktop: () => bridge.enabled,
  runtime: () => ({ runtimeToken: "fixture-runtime-token" }),
}));
vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: () => "fixture-command", sendClientCommand: send, sendClientCommandAwaitResult: awaitResult,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error",
  sendConversationDeleteCommand: vi.fn(async () => true),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: { showBottomPanel: bridge.showBottomPanel } }) }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: openFolder }));
vi.mock("../composer/useComposerPlugins", () => ({ useComposerPlugins: () => ({ plugins: [], loading: false, error: "", refresh: vi.fn() }) }));

import { HeaderBar } from "./HeaderBar";
import { SidebarLeft } from "./SidebarLeft";
import { useAppStore } from "../stores";
import { closeAutomations, getAutomationsPageState } from "../lib/automations-navigation";

const header = (overrides = {}) => <HeaderBar
  leftPanelAvailable leftPanelOpen rightPanelAvailable rightPanelOpen
  onToggleLeftPanel={vi.fn()} onToggleRightPanel={vi.fn()} {...overrides}
/>;
const chooseMode = (name: "代码" | "协作") => {
  fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
  fireEvent.click(screen.getByRole("menuitem", { name, exact: true }));
};
const previousExecCommand = Object.getOwnPropertyDescriptor(document, "execCommand");

describe("desktop chrome uses the existing workbench command and owner chain", () => {
  it("opens the settings-and-help menu before navigating and keeps the conversation draft",()=>{
    useAppStore.setState({settingsOpen:false,draft:"Keep gear-menu draft",conversationId:"A"});
    render(<SidebarLeft embedded />);
    fireEvent.click(screen.getByRole("button",{name:"打开个人资料菜单"}));
    expect(useAppStore.getState().settingsOpen).toBe(false);
    fireEvent.click(screen.getByRole("menuitem",{name:"帮助"}));
    fireEvent.click(screen.getByRole("menuitem",{name:"键盘快捷键"}));
    expect(useAppStore.getState()).toMatchObject({settingsOpen:false,shortcutsHelpOpen:true,draft:"Keep gear-menu draft",conversationId:"A"});
  });
  beforeEach(() => {
    closeAutomations();
    vi.clearAllMocks();
    windowControls.getState.mockResolvedValue({ isMaximized: false });
    windowStateListeners.clear();
    bridge.enabled = false;
    bridge.showBottomPanel = true;
    localStorage.clear();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    useAppStore.setState({
      appMode: "cowork", isConnected: true, connectionPhase: "connected", workingDirectory: "C:\\workspace",
      conversationId: "A", pendingConversationSwitchId: null,
      conversations: ["A", "B", "C", "D"].map(id => ({ id, title: id, updatedAt: "2026-10-05" })),
      messages: [{ id: "message-A", role: "user", content: "Keep my draft context", timestamp: 1, artifacts: [] }],
      draft: "unfinished", settingsOpen: false, settingsTab: "general", skillsMarketplaceOpen: false,
      projectsOpen: false, shortcutsHelpOpen: false, recentWorkspaces: [], availableSkills: [],
      leftSidebarWidth: 0, dockCollapsed: true, activeBottomTab: "git", defaultTerminalLocation: "bottom", rightPanelOpen: true, rightPanelExpanded: false,
      rightStackTab: "tasks", panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: true }],
    });
  });
  afterEach(() => {
    cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
    if (previousExecCommand) Object.defineProperty(document, "execCommand", previousExecCommand);
    else Reflect.deleteProperty(document, "execCommand");
  });

  it("opens the native menu without stealing the active editor selection or draft", () => {
    bridge.enabled = true;
    render(<><textarea aria-label="draft input" defaultValue="unfinished" />{header()}</>);
    const input = screen.getByRole("textbox", { name: "draft input" }) as HTMLTextAreaElement;
    input.focus();
    input.setSelectionRange(2, 6);
    const menu = screen.getByRole("button", { name: "编辑", exact: true });
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    menu.dispatchEvent(down);
    expect(down.defaultPrevented).toBe(true);
    fireEvent.click(menu);
    expect(popup).toHaveBeenCalledExactlyOnceWith("edit");
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd]).toEqual([2, 6]);
    expect(input.value).toBe("unfinished");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it("hides only the bottom-panel header entry when its persisted visibility is off", () => {
    useAppStore.setState({ appMode: "code", dockCollapsed: false, activeBottomTab: "terminal" });
    const view = render(header());
    expect(screen.getByRole("button", { name: "关闭终端", exact: true })).toBeTruthy();
    bridge.showBottomPanel = false;
    view.rerender(header());
    expect(screen.queryByRole("button", { name: "关闭终端", exact: true })).toBeNull();
    expect(screen.getByRole("button", { name: "关闭右侧栏", exact: true })).toBeTruthy();
    expect(useAppStore.getState()).toMatchObject({ dockCollapsed: false, activeBottomTab: "terminal", draft: "unfinished" });
  });

  it("shows restore for a native window already maximized before the header subscribes", async () => {
    bridge.enabled = true;
    windowControls.getState.mockResolvedValue({ isMaximized: true });
    const view = render(header());
    const restore = await screen.findByRole("button", { name: "还原窗口", exact: true });
    expect(restore.querySelector(".lucide-copy")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "最大化", exact: true })).toBeNull();
    expect(windowControls.getState).toHaveBeenCalledOnce();
    expect(windowStateListeners.size).toBe(1);
    expect(useAppStore.getState().draft).toBe("unfinished");
    view.unmount();
    expect(windowStateListeners.size).toBe(0);
    expect(windowControls.onStateChanged.mock.results[0].value).toHaveBeenCalledOnce();
  });

  it("updates the native maximize button from native events and delegates restore without guessing its result", async () => {
    bridge.enabled = true;
    const view = render(header());
    await waitFor(() => expect(windowControls.getState).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "最大化", exact: true }).querySelector(".lucide-square")).toBeTruthy();
    act(() => { for (const callback of windowStateListeners) callback({ isMaximized: true }); });
    fireEvent.click(screen.getByRole("button", { name: "还原窗口", exact: true }));
    expect(windowControls.maximize).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "还原窗口", exact: true })).toBeTruthy();
    act(() => { for (const callback of windowStateListeners) callback({ isMaximized: false }); });
    expect(screen.getByRole("button", { name: "最大化", exact: true }).querySelector(".lucide-square")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "还原窗口", exact: true })).toBeNull();
    expect(useAppStore.getState().draft).toBe("unfinished");
    expect(send).not.toHaveBeenCalled();
    view.unmount();
    expect(windowStateListeners.size).toBe(0);
  });

  it("routes browser menus to the real folder, settings, terminal, and conversation actions", async () => {
    const left = vi.fn();
    render(header({ onToggleLeftPanel: left }));
    fireEvent.click(screen.getByRole("button", { name: "文件", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "打开文件夹…" }));
    expect(openFolder).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "文件", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置…" }));
    expect(useAppStore.getState().settingsOpen).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "视图", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "收起左侧栏" }));
    expect(left).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "视图", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "打开终端" }));
    expect(useAppStore.getState()).toMatchObject({ activeBottomTab: "terminal", dockCollapsed: false, rightStackTab: "tasks", rightPanelOpen: true });
    act(() => useAppStore.setState({ appMode: "code" }));
    fireEvent.click(screen.getByRole("button", { name: "文件", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "新聊天" }));
    await waitFor(() => expect(useAppStore.getState().appMode).toBe("cowork"));
    const state = useAppStore.getState();
    expect(state.conversationDrafts[state.activeConversationDraftId!]).toMatchObject({ bindWorkspace: true, workspaceRoot: "C:\\workspace" });
    expect(state.conversationId).toBeNull();
    expect(popup).not.toHaveBeenCalled();
  });

  it("restores the browser edit target before applying the selected edit action", () => {
    const execute = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { configurable: true, value: execute });
    render(<><textarea aria-label="draft input" defaultValue="unfinished" />{header()}</>);
    const input = screen.getByRole("textbox", { name: "draft input" });
    input.focus();
    fireEvent.click(screen.getByRole("button", { name: "编辑", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "全选" }));
    expect(execute).toHaveBeenCalledExactlyOnceWith("selectAll");
    expect(document.activeElement).toBe(input);
    expect(send).not.toHaveBeenCalled();
  });

  it.each([false, true])("switches through the real sidebar brand menu while retaining owner/editor/draft (native=%s)", (native) => {
    bridge.enabled = native;
    const editorTabs = [{ id: "retained-editor", path: "README.md", content: "draft code", original: "", loading: false, error: null }];
    useAppStore.setState({ editorTabs, activeTabPath: "README.md", activeEditorPath: "README.md", skillsMarketplaceOpen: false,
      panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: false }, { id: "editor", kind: "editor", label: "File", focused: true }],
    });
    act(() => useAppStore.getState().focusPanel("chat"));
    const before = useAppStore.getState();
    const { container } = render(<><SidebarLeft embedded />{header()}</>);
    send.mockClear();
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(container.querySelector(".mc-header-mode-switch")).toBeNull();
    if (native) expect(container.querySelector(".mc-header-end .mc-window-controls")).toBeTruthy();
    chooseMode("代码");
    expect(useAppStore.getState().appMode).toBe("code");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    chooseMode("协作");
    expect(useAppStore.getState().appMode).toBe("cowork");
    chooseMode("代码");
    expect(useAppStore.getState().appMode).toBe("code");
    chooseMode("协作");
    expect(useAppStore.getState().appMode).toBe("cowork");
    const after = useAppStore.getState();
    expect(after.skillsMarketplaceOpen).toBe(false);
    expect(after.conversationId).toBe(before.conversationId);
    expect(after.messages).toBe(before.messages);
    expect(after.editorTabs).toBe(editorTabs);
    expect(after.activeTabPath).toBe("README.md");
    expect(after.activeEditorPath).toBe("README.md");
    expect(after.draft).toBe("unfinished");
    expect(after.currentModel).toBe(before.currentModel);
    expect(after.currentProvider).toBe(before.currentProvider);
    expect(after.effortLevel).toBe(before.effortLevel);
    expect(after.mcpServers).toBe(before.mcpServers);
    expect(screen.queryByRole("navigation", { name: "应用导航" })).toBeNull();
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });

  it("reflects the effective terminal visibility when a maximized editor is retained across chat/code", () => {
    useAppStore.setState({ appMode: "code", dockCollapsed: false, activeBottomTab: "terminal",
      panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: false }, { id: "editor", kind: "editor", label: "File", focused: true, maximized: true }],
      editorTabs: [{ id: "max-editor", path: "README.md", content: "draft", original: "", loading: false, error: null }],
    });
    render(<><SidebarLeft embedded />{header()}</>);
    chooseMode("协作");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.maximized).toBe(true);
    expect(screen.queryByRole("button", { name: "关闭终端" })).toBeNull();
    expect(screen.queryByRole("button", { name: "打开终端" })).toBeNull();
    chooseMode("代码");
    expect(screen.getByRole("button", { name: "关闭终端" })).toBeTruthy();
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.maximized).toBe(false);
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    act(() => useAppStore.getState().togglePanelMaximized("editor"));
    expect(screen.getByRole("button", { name: "打开终端" })).toBeTruthy();
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.maximized).toBe(true);
    expect(useAppStore.getState().conversationId).toBe("A");
    expect(useAppStore.getState().draft).toBe("unfinished");
  });

  it("initializes a missing editor slot through Code without creating or switching a conversation", () => {
    useAppStore.setState({ settingsOpen: false, editorTabs: [], activeTabPath: null, activeEditorPath: null });
    render(<SidebarLeft embedded />);
    send.mockClear();
    chooseMode("代码");
    const state = useAppStore.getState();
    expect(state).toMatchObject({ appMode: "code", settingsOpen: false, conversationId: "A", draft: "unfinished" });
    expect(state.panelSlots.map((slot) => slot.kind)).toEqual(["chat", "editor"]);
    expect(state.panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    expect(state.messages[0].id).toBe("message-A");
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });

  it("records only canonical conversations and blocks history navigation during a pending switch", () => {
    render(header());
    const back = screen.getByRole("button", { name: "上一条访问的对话" }) as HTMLButtonElement;
    const forward = screen.getByRole("button", { name: "下一条访问的对话" }) as HTMLButtonElement;
    expect(back.disabled).toBe(true);
    act(() => useAppStore.setState({ pendingConversationSwitchId: "B" }));
    expect(back.disabled).toBe(true);
    expect(useAppStore.getState().conversationId).toBe("A");
    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    expect(back.disabled).toBe(true);
    act(() => useAppStore.setState({ conversationId: "B" }));
    expect(back.disabled).toBe(false);
    fireEvent.click(back);
    expect(send).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "A" }));
    expect(useAppStore.getState().conversationId).toBe("B");
    expect(back.disabled).toBe(true);
    expect(forward.disabled).toBe(true);
    act(() => useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null }));
    expect(forward.disabled).toBe(false);
  });

  it("truncates forward history when a new canonical branch opens after navigating back", () => {
    render(header());
    const back = screen.getByRole("button", { name: "上一条访问的对话" });
    const forward = screen.getByRole("button", { name: "下一条访问的对话" }) as HTMLButtonElement;
    act(() => useAppStore.setState({ conversationId: "B" }));
    act(() => useAppStore.setState({ conversationId: "C" }));
    fireEvent.click(back);
    act(() => useAppStore.setState({ conversationId: "B", pendingConversationSwitchId: null }));
    expect(forward.disabled).toBe(false);
    act(() => useAppStore.setState({ conversationId: "D" }));
    expect(forward.disabled).toBe(true);
    fireEvent.click(back);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "B", client_command_id: expect.any(String) }));
    act(() => useAppStore.setState({ conversationId: "B", pendingConversationSwitchId: null }));
    fireEvent.click(forward);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "D", client_command_id: expect.any(String) }));
  });

  it("uses the same canonical back/forward history for native menu events", () => {
    const view = render(header());
    act(() => useAppStore.setState({ conversationId: "B" }));
    fireEvent(window, new Event("navigate-back"));
    expect(send).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "A" }));
    fireEvent(window, new Event("navigate-forward"));
    expect(send).toHaveBeenCalledOnce();
    act(() => useAppStore.setState({ conversationId: "A", pendingConversationSwitchId: null }));
    fireEvent(window, new Event("navigate-forward"));
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "B" }));
    expect(useAppStore.getState().draft).toBe("unfinished");
    view.unmount(); send.mockClear();
    fireEvent(window, new Event("navigate-back"));
    expect(send).not.toHaveBeenCalled();
  });

  it("uses real sidebar scheduled and plugin navigation while retaining the active conversation and draft", () => {
    render(<SidebarLeft embedded />);
    send.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "已安排" }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, settingsTab: "general" });
    expect(getAutomationsPageState().open).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "插件", exact: true }));
    expect(getAutomationsPageState().open).toBe(false);
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins" });
    expect(useAppStore.getState()).toMatchObject({ conversationId: "A", draft: "unfinished" });
    expect(useAppStore.getState().messages[0].id).toBe("message-A");
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });

  it("keeps Code and unsaved content when the real sidebar opens project creation", () => {
    useAppStore.setState({ appMode: "code", settingsOpen: false, projectEditTarget: null,
      editorTabs: [{ id: "project-editor", path: "src/main.ts", content: "unsaved code", original: "disk", loading: false }],
    });
    render(<SidebarLeft embedded />);
    send.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "添加项目" }));
    expect(useAppStore.getState()).toMatchObject({ appMode: "code", settingsOpen: false, projectEditTarget: "create", conversationId: "A", draft: "unfinished" });
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved code");
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });
});

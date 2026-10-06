/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { bridge, popup, send, awaitResult, openFolder } = vi.hoisted(() => ({
  bridge: { enabled: false },
  popup: vi.fn(async (_key: string) => undefined),
  send: vi.fn(() => true),
  awaitResult: vi.fn(async (_command: unknown, command: string) => ({
    type: "command.result", command, level: "success", message: "", data: {},
  })),
  openFolder: vi.fn(async () => true),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }) });
});
vi.mock("../desktop/runtime", async (original) => ({
  ...await original<typeof import("../desktop/runtime")>(),
  desktop: () => bridge.enabled ? { menu: { popup }, windowControls: { minimize: vi.fn(), maximize: vi.fn(), close: vi.fn() } } : null,
  isDesktop: () => bridge.enabled,
  runtime: () => ({ runtimeToken: "fixture-runtime-token" }),
}));
vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: () => "fixture-command", sendClientCommand: send, sendClientCommandAwaitResult: awaitResult,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error",
  sendConversationDeleteCommand: vi.fn(async () => true),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: openFolder }));

import { HeaderBar } from "./HeaderBar";
import { NavigationRail } from "./NavigationRail";
import { useAppStore } from "../stores";

const header = (overrides = {}) => <HeaderBar
  leftPanelAvailable leftPanelOpen rightPanelAvailable rightPanelOpen
  onToggleLeftPanel={vi.fn()} onToggleRightPanel={vi.fn()} {...overrides}
/>;
const previousExecCommand = Object.getOwnPropertyDescriptor(document, "execCommand");

describe("desktop chrome uses the existing workbench command and owner chain", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.enabled = false;
    localStorage.clear();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    useAppStore.setState({
      appMode: "cowork", isConnected: true, connectionPhase: "connected", workingDirectory: "C:\\workspace",
      conversationId: "A", pendingConversationSwitchId: null,
      conversations: ["A", "B", "C", "D"].map(id => ({ id, title: id, updatedAt: "2026-10-05" })),
      messages: [{ id: "message-A", role: "user", content: "Keep my draft context", timestamp: 1, artifacts: [] }],
      draft: "unfinished", settingsOpen: false, settingsTab: "general", skillsMarketplaceOpen: false,
      leftSidebarWidth: 0, dockCollapsed: true, rightPanelOpen: true, rightPanelExpanded: false,
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
    expect(useAppStore.getState()).toMatchObject({ activeBottomTab: "terminal", dockCollapsed: false });
    act(() => useAppStore.setState({ appMode: "code" }));
    fireEvent.click(screen.getByRole("button", { name: "文件", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "新聊天" }));
    await waitFor(() => expect(awaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "conversation.create", conversation_type: "main", title: "New chat",
      workspace_root: "C:\\workspace",
    }), "conversation.create"));
    await waitFor(() => expect(useAppStore.getState().appMode).toBe("cowork"));
    expect(useAppStore.getState().conversationId).toBe("A");
    expect(useAppStore.getState().messages[0].id).toBe("message-A");
    expect(useAppStore.getState().draft).toBe("unfinished");
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

  it.each([false, true])("switches through the chat home and Code rail entries while retaining owner/editor/draft (native=%s)", (native) => {
    bridge.enabled = native;
    const editorTabs = [{ id: "retained-editor", path: "README.md", content: "draft code", original: "", loading: false, error: null }];
    useAppStore.setState({ editorTabs, activeTabPath: "README.md", activeEditorPath: "README.md", skillsMarketplaceOpen: true,
      panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: false }, { id: "editor", kind: "editor", label: "File", focused: true }],
    });
    act(() => useAppStore.getState().focusPanel("chat"));
    const before = useAppStore.getState();
    const { container } = render(<><NavigationRail />{header()}</>);
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    const code = within(rail).getByRole("button", { name: "Code", exact: true });
    const chat = within(rail).getByRole("button", { name: "聊天首页", exact: true });
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(container.querySelector(".mc-header-mode-switch")).toBeNull();
    if (native) expect(container.querySelector(".mc-header-end .mc-window-controls")).toBeTruthy();
    expect(chat.getAttribute("aria-current")).toBeNull();
    fireEvent.click(code);
    expect(useAppStore.getState().appMode).toBe("code");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    expect(code.getAttribute("aria-current")).toBe("page");
    expect(chat.getAttribute("aria-current")).toBeNull();
    fireEvent.click(chat);
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(chat.getAttribute("aria-current")).toBe("page");
    expect(code.getAttribute("aria-current")).toBeNull();
    fireEvent.click(code);
    expect(useAppStore.getState().appMode).toBe("code");
    fireEvent.click(chat);
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
    expect(after.leftSidebarWidth).toBeGreaterThan(0);
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });

  it("reflects the effective terminal visibility when a maximized editor is retained across chat/code", () => {
    useAppStore.setState({ appMode: "code", dockCollapsed: false, activeBottomTab: "terminal",
      panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: false }, { id: "editor", kind: "editor", label: "File", focused: true, maximized: true }],
      editorTabs: [{ id: "max-editor", path: "README.md", content: "draft", original: "", loading: false, error: null }],
    });
    render(<><NavigationRail />{header()}</>);
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    fireEvent.click(within(rail).getByRole("button", { name: "聊天首页" }));
    expect(useAppStore.getState().panelSlots.find((slot) => slot.kind === "editor")?.maximized).toBe(true);
    expect(screen.getByRole("button", { name: "关闭终端" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开终端" })).toBeNull();
    fireEvent.click(within(rail).getByRole("button", { name: "Code" }));
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
    useAppStore.setState({ settingsOpen: true, editorTabs: [], activeTabPath: null, activeEditorPath: null });
    render(<NavigationRail />);
    fireEvent.click(screen.getByRole("button", { name: "Code", exact: true }));
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
    expect(send).toHaveBeenCalledExactlyOnceWith({ type: "conversation.switch", conversation_id: "A" });
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
    expect(send).toHaveBeenLastCalledWith({ type: "conversation.switch", conversation_id: "B" });
    act(() => useAppStore.setState({ conversationId: "B", pendingConversationSwitchId: null }));
    fireEvent.click(forward);
    expect(send).toHaveBeenLastCalledWith({ type: "conversation.switch", conversation_id: "D" });
  });

  it("uses existing rail navigation while retaining the active conversation and draft", () => {
    render(<NavigationRail />);
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    fireEvent.click(within(rail).getByRole("button", { name: "已安排" }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "scheduler" });
    fireEvent.click(within(rail).getByRole("button", { name: "Git 与工作树" }));
    expect(useAppStore.getState().settingsTab).toBe("workspaceGit");
    fireEvent.click(within(rail).getByRole("button", { name: "插件与技能" }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins" });
    fireEvent.click(within(rail).getByRole("button", { name: "更多工作区视图" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "子智能体" }));
    expect(useAppStore.getState()).toMatchObject({ appMode: "cowork", rightStackTab: "subagents", skillsMarketplaceOpen: false });
    fireEvent.click(within(rail).getByRole("button", { name: "聊天首页", exact: true }));
    expect(useAppStore.getState().leftSidebarWidth).toBeGreaterThan(0);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "A", draft: "unfinished" });
    expect(useAppStore.getState().messages[0].id).toBe("message-A");
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });

  it("keeps Code active when a secondary workspace view is opened from the more menu", () => {
    useAppStore.setState({ appMode: "code", settingsOpen: true });
    render(<NavigationRail />);
    fireEvent.click(screen.getByRole("button", { name: "更多工作区视图" }));
    expect(screen.queryByRole("menuitem", { name: "代码与文件" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "浏览器" }));
    expect(useAppStore.getState()).toMatchObject({ appMode: "code", settingsOpen: false, rightStackTab: "browser", conversationId: "A", draft: "unfinished" });
    expect(screen.getByRole("button", { name: "Code", exact: true }).getAttribute("aria-current")).toBe("page");
    fireEvent.click(screen.getByRole("button", { name: "聊天首页", exact: true }));
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(send).not.toHaveBeenCalled();
    expect(awaitResult).not.toHaveBeenCalled();
  });
});

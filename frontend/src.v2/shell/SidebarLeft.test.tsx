/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

import { SidebarLeft } from "./SidebarLeft";
import { HeaderBar } from "./HeaderBar";
import { NavigationRail } from "./NavigationRail";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import type { ChatMessage } from "../stores/types";
import { LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";

const renderWithNavigation = (props: { embedded?: boolean; onNavigate?: () => void } = {}) => render(<>
  <NavigationRail />
  <HeaderBar leftPanelAvailable={false} leftPanelOpen={false} rightPanelAvailable={false} rightPanelOpen={false}
    onToggleLeftPanel={vi.fn()} onToggleRightPanel={vi.fn()} />
  <SidebarLeft withGlobalRail {...props} />
</>);
const chooseMode = (name: "代码" | "聊天") => {
  const choice = within(screen.getByRole("navigation", { name: "应用导航" })).getByRole("button", { name: name === "代码" ? "Code" : "聊天首页", exact: true });
  fireEvent.click(choice);
  return choice;
};

vi.mock("../protocol/ws-outbox", () => ({
  createClientCommandId: vi.fn(() => "test-client-command-id"),
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (_command, expectedCommand) => ({
    type: "command.result",
    command: expectedCommand,
    level: "success",
    message: "",
    data: {},
  })),
  commandResultSucceeded: (event: { level?: string }) => !["error", "failed"].includes(String(event.level || "")),
  sendConversationDeleteCommand: vi.fn(() => Promise.resolve(true)),
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => null,
  runtime: () => null,
  isDesktop: () => false,
  revealPath: vi.fn(),
}));

vi.mock("../workspace/openWorkspaceFolder", () => ({
  openWorkspaceFolder: vi.fn(),
}));

describe("SidebarLeft session status", () => {
  beforeEach(() => {
    localStorage.removeItem("minicode.sidebar.conversations.state");
    vi.mocked(sendClientCommand).mockClear();
    vi.mocked(sendClientCommandAwaitResult).mockClear();
    vi.mocked(openWorkspaceFolder).mockClear();
    useAppStore.setState({
      skillsMarketplaceOpen: false,
      settingsOpen: false,
      isConnected: false,
      pendingConversationSwitchId: null,
      appMode: "cowork",
      themeMode: "dark",
      leftSidebarWidth: 280,
      conversationId: "conv-restored",
      conversations: [
        {
          id: "conv-restored",
          title: "Restored pending prompt",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
      ],
      conversationMessages: {},
      recentWorkspaces: [],
      conversationStreaming: {},
      messages: [],
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      pendingAskUser: null,
      askUserQueue: [],
      runtimeSession: null,
      workingDirectory: "C:\\Desktop\\MiniCode",
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps an already open extensions page open when its navigation item is clicked", () => {
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills" });
    render(<SidebarLeft />);
    fireEvent.click(screen.getByRole("button", { name: "插件", exact: true }));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");
  });

  it("hides empty recent-session chrome", () => {
    useAppStore.setState({ conversations: [], conversationId: null });

    render(<SidebarLeft />);

    expect(screen.queryByText("最近会话")).toBeNull();
    expect(screen.queryByPlaceholderText("搜索会话")).toBeNull();
    expect(screen.queryByText("No sessions yet.")).toBeNull();
  });

  it("resizes the actual sidebar with keys, clamps it, and remembers its open width", () => {
    useAppStore.setState({ leftSidebarWidth: 320 });

    const { container } = render(<SidebarLeft />);

    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    const aside = container.querySelector<HTMLElement>(".mc-sidebar-left");
    expect(aside).not.toBeNull();
    expect(aside!.style.width).toBe("320px");
    fireEvent.keyDown(handle, { key: "ArrowRight", shiftKey: true });
    expect(aside!.style.width).toBe("360px");
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(360);
    fireEvent.keyDown(handle, { key: "Home" });
    expect(aside!.style.width).toBe(`${LEFT_SIDEBAR_MIN_WIDTH}px`);
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(useAppStore.getState().leftSidebarWidth).toBe(LEFT_SIDEBAR_MIN_WIDTH);
    fireEvent.doubleClick(handle);
    expect(useAppStore.getState().leftSidebarWidth).toBe(LEFT_SIDEBAR_DEFAULT_WIDTH);
  });

  it("drags without transition lag and retains folder content when collapsed", () => {
    useAppStore.setState({ leftSidebarWidth: 300, leftSidebarExpandedWidth: 300 });
    const { container } = render(<SidebarLeft />);
    const content = container.querySelector(".mc-sidebar-mode-content");
    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    handle.setPointerCapture = vi.fn();
    fireEvent(handle, new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 300 }));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 348 }));
    expect(useAppStore.getState().leftSidebarWidth).toBe(348);
    expect(document.body.classList.contains("layout-dragging")).toBe(true);
    fireEvent.pointerUp(handle);
    expect(document.body.classList.contains("layout-dragging")).toBe(false);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(358);
    useAppStore.getState().setLeftSidebarWidth(0);
    expect(content!.isConnected).toBe(true);
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(358);
  });

  it("keeps the sidebar brand a title and search in the heading", () => {
    render(<SidebarLeft />);
    const navigation = screen.getByRole("navigation", { name: "工作区导航" });
    expect(within(navigation).getByRole("button", { name: "新聊天" })).toBeTruthy();
    expect(within(navigation).queryByRole("button", { name: "搜索" })).toBeNull();
    expect(screen.getByRole("button", { name: "搜索" }).closest(".mc-sidebar-heading")).toBeTruthy();
    expect(screen.getByRole("button", { name: "添加项目" }).closest(".mc-sidebar-project-heading")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "切换项目" })).toBeNull();
    expect(screen.getByText("MiniCode").closest("button")).toBeNull();
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(screen.queryByLabelText("MiniCode · 切换工作模式")).toBeNull();
  });

  it("switches the bell to real activity groups while retaining main task, models, MCP, and draft", () => {
    const now = new Date();
    useAppStore.setState({ rightStackTab: "tasks", draft: "unfinished activity draft", currentModel: "selected-model",
      currentProvider: "selected-provider", effortLevel: "high",
      runtimeSession: {
        active_conversation_id: "conv-restored",
        pending_approval_count: 1,
        pending_approvals: [{ request_id: "activity-input", type: "control_request", subtype: "elicitation", conversation_id: "conv-waiting" }],
      },
      recentWorkspaces: [{ path: "C:\\Desktop\\MiniCode", name: "MiniCode", projectType: "node", lastOpened: 1 }],
      conversations: [
        { id: "conv-restored", title: "Main task", updatedAt: now.toISOString(), workspaceRoot: "C:\\Desktop\\MiniCode", summary: "Recorded task summary" },
        { id: "conv-waiting", title: "Waiting task", updatedAt: "2026-01-01T00:00:00Z" },
      ],
    });
    const before = useAppStore.getState();
    const onNavigate = vi.fn();
    render(<SidebarLeft embedded onNavigate={onNavigate} />);
    expect(screen.getByRole("region", { name: "工作区 MiniCode" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看任务状态" }));
    expect(within(screen.getByRole("region", { name: "优先级" })).getByText("Waiting task")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "优先级" })).getByLabelText("等待回复")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "今天" })).getByText("Main task")).toBeTruthy();
    expect(screen.getByText("Recorded task summary")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "工作区 MiniCode" })).toBeNull();
    const active = screen.getByRole("button", { name: "返回项目与最近" });
    expect(active.getAttribute("aria-pressed")).toBe("true");
    const after = useAppStore.getState();
    expect(after.rightStackTab).toBe("tasks");
    expect(after.conversationId).toBe(before.conversationId);
    expect(after.messages).toBe(before.messages);
    expect(after.appMode).toBe(before.appMode);
    expect(after.draft).toBe(before.draft);
    expect(after.currentModel).toBe(before.currentModel);
    expect(after.currentProvider).toBe(before.currentProvider);
    expect(after.effortLevel).toBe(before.effortLevel);
    expect(after.mcpServers).toBe(before.mcpServers);
    expect(onNavigate).not.toHaveBeenCalled();
    fireEvent.click(active);
    expect(screen.getByRole("region", { name: "工作区 MiniCode" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "最近" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "优先级" })).toBeNull();
  });

  it("marks the active session as waiting from restored runtime pending state", () => {
    useAppStore.setState({
      runtimeSession: {
        pending_approval_count: 1,
        pending_approvals: [{ request_id: "ask-1", type: "control_request", subtype: "elicitation" }],
      },
    });

    render(<SidebarLeft />);

    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
    expect(screen.getByText("等待回复")).toBeTruthy();
    expect(screen.queryByText("当前筛选下暂无会话")).toBeNull();
  });

  it("marks the owning inactive session as waiting from restored runtime pending state", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        {
          id: "conv-active",
          title: "Active session",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
        {
          id: "conv-waiting",
          title: "Waiting inactive session",
          updatedAt: "2026-05-25T00:00:00.000Z",
        },
      ],
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-inactive",
          type: "control_request",
          subtype: "elicitation",
          conversation_id: "conv-waiting",
        }],
      },
    });

    render(<SidebarLeft />);

    expect(screen.getByText("Waiting inactive session")).toBeTruthy();
    expect(screen.getByText("等待回复")).toBeTruthy();
    expect(screen.getByText("Active session")).toBeTruthy();
    expect(screen.queryByText("当前筛选下暂无会话")).toBeNull();
  });

  it("marks conversations waiting when their local prompt is queued behind another conversation", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        {
          id: "conv-active",
          title: "Active question",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
        {
          id: "conv-waiting",
          title: "Queued review",
          updatedAt: "2026-05-25T00:00:00.000Z",
        },
      ],
      pendingAskUser: {
        requestId: "ask-active",
        conversationId: "conv-active",
        question: "Continue?",
      },
      diffReviewQueue: [{
        requestId: "diff-waiting",
        conversationId: "conv-waiting",
        diff: "+queued",
      }],
    });

    render(<SidebarLeft />);

    expect(screen.getByText("Active question")).toBeTruthy();
    expect(screen.getByText("Queued review")).toBeTruthy();
    expect(screen.getByText("等待回复")).toBeTruthy();
    expect(screen.getByText("等待审阅")).toBeTruthy();
  });

  it("does not expose session deletion from the sidebar menu", () => {
    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("button", { name: "会话操作" }));
    expect(screen.queryByRole("menuitem", { name: "删除" })).toBeNull();
    expect(sendConversationDeleteCommand).not.toHaveBeenCalled();
    expect(useAppStore.getState().conversations.map((c) => c.id)).toContain("conv-restored");
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("keeps project navigation in place when Code opens from the rail", () => {
    useAppStore.setState({
      appMode: "cowork",
      workingDirectory: "",
    });

    renderWithNavigation();

    chooseMode("代码");

    expect(openWorkspaceFolder).not.toHaveBeenCalled();
    expect(useAppStore.getState().appMode).toBe("code");
    expect(document.querySelector('.mc-sidebar-mode-content')?.getAttribute("data-mode")).toBe("code");
    expect(screen.queryByRole("button", { name: "项目文件" })).toBeNull();
    expect(screen.queryByRole("button", { name: "返回会话" })).toBeNull();
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("preserves project folders, expansion and the same list through code/chat switches", () => {
    useAppStore.setState({
      recentWorkspaces: [{ path: "C:\\Desktop\\MiniCode", name: "MiniCode", projectType: "node", lastOpened: 1 }],
      conversations: [{ id: "conv-restored", title: "Project chat", workspaceRoot: "C:\\Desktop\\MiniCode", updatedAt: "2026-10-05T00:00:00Z" }],
      draft: "Keep the draft",
    });
    renderWithNavigation();
    const project = screen.getByRole("region", { name: "工作区 MiniCode" });
    const folder = within(project).getByRole("button", { name: "MiniCode", exact: true });
    const list = screen.getByTestId("conversation-list");
    fireEvent.click(folder);
    list.scrollTop = 84;
    fireEvent.scroll(list);
    chooseMode("代码");
    expect(screen.getByRole("region", { name: "工作区 MiniCode" })).toBe(project);
    expect(folder.getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByTestId("conversation-list")).toBe(list);
    chooseMode("聊天");
    expect(screen.getByRole("region", { name: "工作区 MiniCode" })).toBe(project);
    expect(list.scrollTop).toBe(84);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "conv-restored", draft: "Keep the draft" });
    expect(screen.queryByText("普通任务")).toBeNull();
  });

  it("returns to conversations through the rail chat home", () => {
    useAppStore.setState({ appMode: "code" });
    renderWithNavigation();

    const choice = chooseMode("聊天");

    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(choice.getAttribute("aria-current")).toBe("page");
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("uses rail navigation while an embedded sidebar retains its conversation list", () => {
    const onNavigate = vi.fn();
    useAppStore.setState({ appMode: "code" });

    renderWithNavigation({ embedded: true, onNavigate });
    chooseMode("聊天");

    expect(onNavigate).not.toHaveBeenCalled();
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("keeps chat home and Code in the rail without duplicate header or sidebar mode switchers", () => {
    const { container } = renderWithNavigation();
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    const cowork = within(rail).getByRole("button", { name: "聊天首页", exact: true });
    const code = within(rail).getByRole("button", { name: "Code", exact: true });
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(container.querySelector(".mc-header-mode-switch")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Code", exact: true })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "聊天首页", exact: true })).toHaveLength(1);
    expect(container.querySelector(".mc-sidebar-left")!.querySelector('[role="tablist"]')).toBeNull();
    expect(cowork.getAttribute("aria-current")).toBe("page");

    fireEvent.click(code);
    expect(useAppStore.getState().appMode).toBe("code");
    expect(code.getAttribute("aria-current")).toBe("page");

    fireEvent.click(cowork);
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(cowork.getAttribute("aria-current")).toBe("page");
  });

  it("keeps settings in the persistent sidebar footer", () => {
    useAppStore.setState({ settingsOpen: false });
    render(<SidebarLeft />);

    const settingsButton = screen.getByRole("button", { name: "设置" });
    expect(settingsButton.closest(".mc-sidebar-footer")).toBeTruthy();
    fireEvent.click(settingsButton);
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("keeps a working theme toggle beside settings", () => {
    render(<SidebarLeft />);

    const lightButton = screen.getByRole("button", { name: "切换到浅色模式" });
    expect(lightButton.closest(".mc-sidebar-footer")).toBeTruthy();
    fireEvent.click(lightButton);
    expect(useAppStore.getState().themeMode).toBe("light");

    fireEvent.click(screen.getByRole("button", { name: "切换到深色模式" }));
    expect(useAppStore.getState().themeMode).toBe("dark");
  });

  it("retains task navigation while leaving global settings and plugin actions to the rail", () => {
    render(<SidebarLeft withGlobalRail />);
    expect(screen.getByRole("button", { name: "新聊天" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "搜索", exact: true })).toBeTruthy();
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "设置", exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "插件", exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "已安排", exact: true })).toBeNull();
  });

  it("keeps narrow stored widths as a full session sidebar", () => {
    useAppStore.setState({
      appMode: "cowork",
      leftSidebarWidth: 252,
      workingDirectory: "C:\\Desktop\\MiniCode",
    });

    render(<SidebarLeft />);

    expect(screen.queryByRole("button", { name: "返回会话列表" })).toBeNull();
    expect(screen.getByRole("button", { name: "新聊天" })).toBeTruthy();
  });

  it("does not retain padding or a border when its inline width is collapsed", () => {
    useAppStore.setState({ leftSidebarWidth: 0 });

    const { container } = render(<SidebarLeft />);
    const sidebar = container.querySelector<HTMLElement>(".mc-sidebar-left");

    expect(sidebar?.style.width).toBe("0px");
    expect(sidebar?.style.padding).toBe("0px");
    expect(sidebar?.style.borderRightWidth).toBe("0px");
  });

  it("routes scheduled tasks to the scheduler settings page", async () => {
    useAppStore.setState({ settingsOpen: false, automationsOpen: false, settingsTab: "general" });
    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("button", { name: "已安排" }));
    expect(useAppStore.getState().automationsOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(true);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("scheduler"));

    useAppStore.getState().setSettingsTab("general");
    fireEvent.click(screen.getByRole("button", { name: "已安排" }));
    expect(useAppStore.getState().automationsOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(true);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("scheduler"));
  });

  it("keeps the editor panel when switching back to Code from Cowork", () => {
    useAppStore.setState({
      appMode: "cowork",
      workingDirectory: "C:\\Desktop\\MiniCode",
      editorTabs: [{ id: "editor-fixture-1", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
      activeEditorPath: "README.md",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true },
      ],
    });

    renderWithNavigation();

    chooseMode("代码");

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    expect(state.activeTabPath).toBe("README.md");
    expect(state.activeEditorPath).toBe("README.md");
  });

  it("requests a workspace-bound new chat from Code and returns to the conversation UI", async () => {
    const editorTabs = [{ id: "kept-editor", path: "README.md", content: "unsaved code", original: "", loading: false, error: null }];
    const panelSlots = [{ id: "main-chat", kind: "chat" as const, label: "Chat", focused: false }, { id: "main-editor", kind: "editor" as const, label: "File", focused: true }];
    useAppStore.setState({
      appMode: "code",
      workingDirectory: "C:\\Desktop\\MiniCode",
      draft: "unfinished prompt", editorTabs, panelSlots, activeTabPath: "README.md", activeEditorPath: "README.md",
    });

    render(<SidebarLeft />);
    vi.mocked(sendClientCommand).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "新聊天" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalled());
    await waitFor(() => expect(useAppStore.getState().appMode).toBe("cowork"));
    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { type?: string; workspace_root?: string };
    const state = useAppStore.getState();
    expect(command).toMatchObject({ type: "conversation.create", workspace_root: "C:\\Desktop\\MiniCode" });
    expect(state.appMode).toBe("cowork");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state).toMatchObject({ conversationId: "conv-restored", draft: "unfinished prompt", activeTabPath: "README.md", activeEditorPath: "README.md" });
    expect(state.editorTabs).toBe(editorTabs);
    expect(state.panelSlots).toBe(panelSlots);
    expect(state.conversations[0].workspaceRoot).toBeUndefined();
  });

  it("closes an embedded drawer after starting a session", () => {
    const onNavigate = vi.fn();
    useAppStore.setState({ appMode: "cowork" });

    render(<SidebarLeft embedded onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "新聊天" }));

    expect(onNavigate).toHaveBeenCalled();
  });

  it("closes an embedded drawer when reselecting the active session", () => {
    const onNavigate = vi.fn();
    render(<SidebarLeft embedded onNavigate={onNavigate} />);

    fireEvent.click(screen.getByText("Restored pending prompt"));

    expect(onNavigate).toHaveBeenCalled();
  });

  it("does not show workspace cleanup actions for Computer chats", () => {
    useAppStore.setState({
      appMode: "cowork",
      workingDirectory: "",
      conversations: [{
        id: "conv-computer",
        title: "Computer chat",
        updatedAt: "2026-05-24T00:00:00.000Z",
      }],
    });

    render(<SidebarLeft />);

    expect(screen.queryByRole("button", { name: "Delete all sessions for the current workspace" })).toBeNull();
    expect(screen.queryByText("Clear workspace")).toBeNull();
    expect(screen.getByText("Computer chat")).toBeTruthy();
  });

  it("keeps workspace membership and includes both bound and ordinary tasks in recents", () => {
    useAppStore.setState({
      recentWorkspaces: [{ path: "C:\\Desktop\\MiniCode", name: "MiniCode", projectType: "unknown", lastOpened: 1 }],
      conversations: [
        {
          id: "conv-workspace",
          title: "Workspace task",
          updatedAt: "2026-05-25T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\MiniCode",
        },
        {
          id: "conv-ordinary",
          title: "Ordinary task",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
      ],
    });

    render(<SidebarLeft />);

    const workspaceSection = screen.getByRole("region", { name: "工作区 MiniCode" });
    const taskSection = screen.getByRole("region", { name: "最近" });
    expect(within(workspaceSection).getByText("Workspace task")).toBeTruthy();
    expect(within(workspaceSection).queryByText("Ordinary task")).toBeNull();
    expect(within(taskSection).getByText("Ordinary task")).toBeTruthy();
    expect(within(taskSection).getByText("Workspace task")).toBeTruthy();
  });

  it("marks session action menus for hover-only presentation", () => {
    render(<SidebarLeft />);

    const action = screen.getAllByRole("button", { name: "会话操作" })[0];
    expect(action?.parentElement?.classList.contains("session-row-actions")).toBe(true);
  });

  it("renders the session action menu above clipped conversation groups", () => {
    render(<SidebarLeft />);

    const action = screen.getAllByRole("button", { name: "会话操作" })[0];
    fireEvent.click(action!);

    const menu = screen.getByRole("menu", { name: "会话操作" });
    expect(menu.parentElement).toBe(document.body);
    expect(menu.style.position).toBe("fixed");
    expect(menu.closest(".mc-workspace-group-body")).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "切换会话" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "重命名" })).toBeTruthy();
    expect(menu.classList.contains("mc-conversation-menu")).toBe(true);
    expect(action?.getAttribute("aria-expanded")).toBe("true");
  });

  it("renames a session from the action menu", async () => {
    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("button", { name: "会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByDisplayValue("Restored pending prompt");
    fireEvent.change(input, { target: { value: "Renamed task" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(
      {
        type: "conversation.rename",
        conversation_id: "conv-restored",
        title: "Renamed task",
      },
      "conversation.rename",
    ));
  });

  it("keeps Escape scoped to cancelling session rename", () => {
    const outsideEscape = vi.fn();
    document.addEventListener("keydown", outsideEscape);
    render(<SidebarLeft embedded />);

    fireEvent.click(screen.getByRole("button", { name: "会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
    const input = screen.getByDisplayValue("Restored pending prompt");
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByDisplayValue("Restored pending prompt")).toBeNull();
    expect(outsideEscape).not.toHaveBeenCalled();
    document.removeEventListener("keydown", outsideEscape);
  });

  it("shows tasks without selection or bulk controls", () => {
    useAppStore.setState({
      conversations: [
        { id: "conv-alpha", title: "Alpha", updatedAt: "2026-05-24T00:00:00.000Z" },
        { id: "conv-beta", title: "Beta", updatedAt: "2026-05-25T00:00:00.000Z" },
      ],
    });

    render(<SidebarLeft />);
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "选择会话" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("uses the streaming state map instead of scanning stale cached messages", () => {
    const staleMessages: ChatMessage[] = [
      {
        id: "stale-user",
        role: "user",
        content: "old request",
        artifacts: [],
        timestamp: 1,
      },
      {
        id: "stale-assistant",
        role: "assistant",
        content: "old answer",
        blocks: [{ type: "text", content: "old answer" }],
        artifacts: [],
        timestamp: 2,
        isStreaming: true,
      },
      ...Array.from({ length: 8 }, (_, index) => ({
        id: `settled-${index}`,
        role: "user" as const,
        content: `settled ${index}`,
        artifacts: [],
        timestamp: index + 3,
      })),
    ];
    useAppStore.setState({
      conversationId: "conv-restored",
      conversations: [
        {
          id: "conv-restored",
          title: "Active session",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
        {
          id: "conv-stale",
          title: "Stale cached session",
          updatedAt: "2026-05-25T00:00:00.000Z",
        },
      ],
      conversationMessages: { "conv-stale": staleMessages },
      conversationStreaming: { "conv-stale": false },
      messages: [],
      isStreaming: false,
    });

    render(<SidebarLeft />);

    expect(screen.queryByRole("button", { name: /运行中/ })).toBeNull();
    expect(screen.getByText("Stale cached session")).toBeTruthy();
  });

  it("marks inactive sessions running from conversationStreaming", () => {
    useAppStore.setState({
      conversationId: "conv-restored",
      conversations: [
        {
          id: "conv-restored",
          title: "Active session",
          updatedAt: "2026-05-24T00:00:00.000Z",
        },
        {
          id: "conv-bg",
          title: "Background run",
          updatedAt: "2026-05-25T00:00:00.000Z",
        },
      ],
      conversationMessages: { "conv-bg": [] },
      conversationStreaming: { "conv-bg": true },
      messages: [],
      isStreaming: false,
    });

    render(<SidebarLeft />);

    expect(screen.getByText("Background run")).toBeTruthy();
    expect(screen.getByLabelText("任务运行中")).toBeTruthy();
    expect(screen.getByText("Active session")).toBeTruthy();
  });

  it("keeps tasks from all projects visible in their workspace groups", () => {
    useAppStore.setState({
      recentWorkspaces: ["C:\\Desktop\\MiniCode", "C:\\Desktop\\Other"].map(path => ({ path, name: path, projectType: "unknown", lastOpened: 1 })),
      appMode: "cowork",
      workingDirectory: "C:\\Desktop\\MiniCode",
      workspaceGit: {
        branch: "main",
        isWorktree: false,
        currentPath: "C:\\Desktop\\MiniCode",
      },
      conversations: [
        {
          id: "conv-current",
          title: "Current project task",
          updatedAt: "2026-05-26T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\MiniCode",
        },
        {
          id: "conv-other",
          title: "Other project task",
          updatedAt: "2026-05-27T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\Other",
        },
      ],
    });

    render(<SidebarLeft />);

    expect(within(screen.getByRole("region", { name: "工作区 MiniCode" })).getByText("Current project task")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "工作区 Other" })).getByText("Other project task")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "最近" })).getByText("Current project task")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "最近" })).getByText("Other project task")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "当前工作区" })).toBeNull();
  });

  it("uses open and closed folder icons for workspace groups", () => {
    useAppStore.setState({
      recentWorkspaces: [{ path: "C:\\Desktop\\MiniCode", name: "MiniCode", projectType: "unknown", lastOpened: 1 }],
      appMode: "cowork",
      workingDirectory: "C:\\Desktop\\MiniCode",
      conversations: [
        {
          id: "conv-workspace",
          title: "Workspace task",
          updatedAt: "2026-05-26T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\MiniCode",
        },
      ],
    });

    render(<SidebarLeft />);

    const workspaceSection = screen.getByRole("region", { name: "工作区 MiniCode" });
    const workspaceToggle = within(workspaceSection).getByRole("button", { name: "MiniCode" });
    expect(within(workspaceSection).getByTestId(/workspace-folder-open/)).toBeTruthy();
    expect(within(workspaceSection).getByText("Workspace task")).toBeTruthy();

    fireEvent.click(workspaceToggle);
    expect(within(workspaceSection).getByTestId(/workspace-folder-closed/)).toBeTruthy();
    expect(within(workspaceSection).queryByText("Workspace task")).toBeNull();

    fireEvent.click(workspaceToggle);
    expect(within(workspaceSection).getByTestId(/workspace-folder-open/)).toBeTruthy();
    expect(within(workspaceSection).getByText("Workspace task")).toBeTruthy();
  });

  it("starts a workspace-bound task from the project row", async () => {
    useAppStore.setState({
      recentWorkspaces: [{ path: "C:\\Desktop\\MiniCode", name: "MiniCode", projectType: "unknown", lastOpened: 1 }],
      appMode: "cowork",
      workingDirectory: "C:\\Desktop\\MiniCode",
      conversations: [
        {
          id: "conv-workspace",
          title: "Workspace task",
          updatedAt: "2026-05-26T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\MiniCode",
        },
      ],
    });

    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("button", { name: "在 MiniCode 中新建任务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "conversation.create",
      workspace_root: "C:\\Desktop\\MiniCode",
    }), "conversation.create"));
    expect(useAppStore.getState().workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(useAppStore.getState().appMode).toBe("cowork");
  });
});

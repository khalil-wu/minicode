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
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import type { ChatMessage } from "../stores/types";

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

  it("hides empty recent-session chrome", () => {
    useAppStore.setState({ conversations: [], conversationId: null });

    render(<SidebarLeft />);

    expect(screen.queryByText("最近会话")).toBeNull();
    expect(screen.queryByPlaceholderText("搜索会话")).toBeNull();
    expect(screen.queryByText("No sessions yet.")).toBeNull();
  });

  it("pins the sidebar to the fixed minimum width with no resize handle", () => {
    useAppStore.setState({ leftSidebarWidth: 320 });

    const { container } = render(<SidebarLeft />);

    expect(screen.queryByRole("separator", { name: "调整左侧栏宽度" })).toBeNull();
    const aside = container.querySelector(".mc-sidebar-left");
    expect(aside).not.toBeNull();
    expect(aside!.getAttribute("style")).toContain("272px");
  });

  it("groups search with new task and keeps project navigation outside primary commands", () => {
    render(<SidebarLeft />);
    const navigation = screen.getByRole("navigation", { name: "工作区导航" });
    expect(within(navigation).getByRole("button", { name: "搜索" }).querySelector("svg")).toBeTruthy();
    expect(screen.getByRole("button", { name: "切换项目" }).closest("nav")).toBeNull();
    const code = screen.getByRole("tab", { name: "代码" });
    fireEvent.keyDown(code, { key: "End" });
    expect(useAppStore.getState().appMode).toBe("code");
    expect(document.activeElement).toBe(code);
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

  it("uses the Code mode tab as the only project-files switch", () => {
    useAppStore.setState({
      appMode: "cowork",
      workingDirectory: "",
    });

    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("tab", { name: "代码" }));

    expect(openWorkspaceFolder).not.toHaveBeenCalled();
    expect(useAppStore.getState().appMode).toBe("code");
    expect(document.querySelector('.mc-sidebar-mode-content')?.getAttribute("data-mode")).toBe("code");
    expect(screen.queryByRole("button", { name: "项目文件" })).toBeNull();
    expect(screen.queryByRole("button", { name: "返回会话" })).toBeNull();
  });

  it("returns to conversations through the Cowork mode tab", () => {
    useAppStore.setState({ appMode: "code" });
    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("tab", { name: "协作" }));

    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(screen.getByRole("tab", { name: "协作" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("uses the same mode switch in the embedded drawer", () => {
    const onNavigate = vi.fn();
    useAppStore.setState({ appMode: "code" });

    render(<SidebarLeft embedded onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("tab", { name: "协作" }));

    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("restores the Cowork and Code mode switch", () => {
    render(<SidebarLeft />);

    const modeSwitch = screen.getByTestId("sidebar-mode-switch");
    const cowork = screen.getByRole("tab", { name: "协作" });
    const code = screen.getByRole("tab", { name: "代码" });
    expect(modeSwitch.style.height).toBe("40px");
    expect(modeSwitch.style.minHeight).toBe("40px");
    expect(modeSwitch.style.padding).toBe("2px");
    expect(modeSwitch.style.gap).toBe("2px");
    for (const tab of [cowork, code]) {
      expect(tab.style.height).toBe("34px");
      expect(tab.style.minHeight).toBe("34px");
      expect(tab.style.padding).toBe("0px 8px");
      expect(tab.style.boxSizing).toBe("border-box");
    }
    expect(cowork.getAttribute("aria-selected")).toBe("true");

    fireEvent.click(code);
    expect(useAppStore.getState().appMode).toBe("code");
    expect(code.getAttribute("aria-selected")).toBe("true");

    fireEvent.click(cowork);
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(cowork.getAttribute("aria-selected")).toBe("true");
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

  it("keeps narrow stored widths as a full session sidebar", () => {
    useAppStore.setState({
      appMode: "cowork",
      leftSidebarWidth: 252,
      workingDirectory: "C:\\Desktop\\MiniCode",
    });

    render(<SidebarLeft />);

    expect(screen.queryByRole("button", { name: "返回会话列表" })).toBeNull();
    expect(screen.getByRole("button", { name: "新建任务" })).toBeTruthy();
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
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
      activeEditorPath: "README.md",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true },
      ],
    });

    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("tab", { name: "代码" }));

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.panelSlots.find((slot) => slot.kind === "editor")?.focused).toBe(true);
    expect(state.activeTabPath).toBe("README.md");
    expect(state.activeEditorPath).toBe("README.md");
  });

  it("requests a workspace-bound session from Code without switching modes", async () => {
    useAppStore.setState({
      appMode: "code",
      workingDirectory: "C:\\Desktop\\MiniCode",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
    });

    render(<SidebarLeft />);
    vi.mocked(sendClientCommand).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "新建任务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalled());
    const command = vi.mocked(sendClientCommandAwaitResult).mock.calls[0]?.[0] as { type?: string; workspace_root?: string };
    const state = useAppStore.getState();
    expect(command).toMatchObject({ type: "conversation.create", workspace_root: "C:\\Desktop\\MiniCode" });
    expect(state.appMode).toBe("code");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state.conversations[0].workspaceRoot).toBeUndefined();
  });

  it("closes an embedded drawer after starting a session", () => {
    const onNavigate = vi.fn();
    useAppStore.setState({ appMode: "cowork" });

    render(<SidebarLeft embedded onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "新建任务" }));

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

  it("separates workspace tasks from ordinary tasks", () => {
    useAppStore.setState({
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
    const taskSection = screen.getByRole("region", { name: "普通任务" });
    expect(within(workspaceSection).getByText("Workspace task")).toBeTruthy();
    expect(within(workspaceSection).queryByText("Ordinary task")).toBeNull();
    expect(within(taskSection).getByText("Ordinary task")).toBeTruthy();
    expect(within(taskSection).queryByText("Workspace task")).toBeNull();
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

    expect(screen.getByText("Current project task")).toBeTruthy();
    expect(screen.getByText("Other project task")).toBeTruthy();

    expect(screen.getByRole("region", { name: "工作区 MiniCode" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "工作区 Other" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "当前工作区" })).toBeNull();
  });

  it("uses open and closed folder icons for workspace groups", () => {
    useAppStore.setState({
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

/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

import { CommandPalette } from "./CommandPalette";
import { useAppStore } from "../stores";
import { sendChatMessage } from "../chat/sendChatMessage";
import { sendClientCommand } from "../protocol/ws-outbox";
import { showConfirm } from "./DialogService";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../chat/openWebInBrowser";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
}));

vi.mock("../chat/sendChatMessage", () => ({
  sendChatMessage: vi.fn(),
}));

vi.mock("./DialogService", () => ({
  showConfirm: vi.fn(async () => true),
}));

describe("CommandPalette pending user action guard", () => {
  it.each([
    ["搜索项目内容", "search"],
    ["查看代码问题", "problems"],
    ["管理预览服务", "preview"],
  ] as const)("opens %s through its concrete workspace entry", (label, target) => {
    useAppStore.setState({ conversationId: "conv-entry", workingDirectory: "C:/Workspace", runtimeSession: null,
      runtimeCapabilities: null, workspaceSearchOpen: false, dockCollapsed: true, rightPanelOpen: false,
      previewServiceManagerRequest: null, appMode: "code" });
    render(<CommandPalette />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: label } });
    fireEvent.click(screen.getByRole("option", { name: new RegExp(label) }));
    const state = useAppStore.getState();
    expect(state.commandPaletteOpen).toBe(false);
    if (target === "search") {
      expect(state.workspaceSearchOpen).toBe(true);
      expect(state.appMode).toBe("code");
    } else if (target === "problems") {
      expect(state.activeBottomTab).toBe("problems");
      expect(state.dockCollapsed).toBe(false);
    } else {
      expect(state.rightStackTab).toBe("browser");
      expect(state.rightPanelOpen).toBe(true);
      expect(state.previewServiceManagerRequest).toMatchObject({ conversationId: "conv-entry", workspaceRoot: "C:/Workspace" });
    }
  });
  it("finds the end of a long conversation goal using a trimmed query", () => {
    useAppStore.setState({ commandPaletteOpen: true, runtimeSession: null, runtimeCapabilities: null,
      conversationId: "active", conversations: [{ id: "goal-result", title: "Review the project", goal: { text: `${"Earlier details ".repeat(8)}needle-at-the-end`, status: "active" } }],
      pendingApproval: null, pendingDiffReview: null, pendingAskUser: null, slashCommands: [] });
    render(<CommandPalette />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "  needle-at-the-end  " } });
    expect(screen.getByRole("option", { name: /Review the project/ })).toBeTruthy();
    expect(screen.getByText(/needle-at-the-end/)).toBeTruthy();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    __resetOpenWebInBrowserForTests();
    useAppStore.setState({
      commandPaletteOpen: true,
      appMode: "cowork",
      defaultTerminalLocation: "bottom",
      conversations: [],
      pendingApproval: null,
      pendingDiffReview: null,
      pendingAskUser: null,
      runtimeSession: {
        pending_approval_count: 1,
        pending_approvals: [{ request_id: "ask-1", type: "ask_user" }],
      },
      slashCommands: [],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps the palette open on Escape when restored runtime state is waiting for user action", () => {
    render(<CommandPalette />);

    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });

    expect(screen.getByRole("combobox")).toBeTruthy();
    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
  });

  it("allows closing when only another conversation is waiting for user action", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-other",
          type: "ask_user",
          conversation_id: "conv-other",
        }],
      },
    });
    render(<CommandPalette />);

    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });

    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  });

  it("allows closing when only another conversation has a local pending prompt", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      pendingAskUser: {
        requestId: "ask-other",
        conversationId: "conv-other",
        question: "Continue other conversation?",
      },
      runtimeSession: null,
    });

    render(<CommandPalette />);

    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Escape" });

    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  });

  it("runs backend slash catalog commands from the palette", async () => {
    useAppStore.setState({
      runtimeSession: null,
      slashCommands: [
        {
          name: "status",
          command: "status",
          label: "/status",
          description: "Show runtime status",
          type: "local",
        },
      ],
    });

    render(<CommandPalette />);

    fireEvent.click(screen.getByText("运行 /status"));

    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/status",
      backendContent: "/status",
      skipLocalAppend: true,
    }));
  });

  it("refreshes command metadata when opened and searches slash descriptions", () => {
    useAppStore.setState({
      runtimeSession: null,
      slashCommands: [
        {
          name: "status",
          command: "status",
          label: "/status",
          description: "Show runtime metrics",
          type: "local",
        },
      ],
    });

    render(<CommandPalette />);

    expect(sendClientCommand).toHaveBeenCalledWith({ type: "commands.list" });

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "metrics" } });

    expect(screen.getByText("运行 /status")).toBeTruthy();
  });

  it.each([
    { appMode: "cowork", defaultTerminalLocation: "right" },
    { appMode: "code", defaultTerminalLocation: "bottom" },
  ] as const)("opens the preferred $defaultTerminalLocation Terminal in $appMode", ({ appMode, defaultTerminalLocation }) => {
    useAppStore.setState({
      runtimeSession: null,
      appMode,
      defaultTerminalLocation,
      dockCollapsed: true,
      activeBottomTab: "git",
      rightPanelOpen: false,
      rightStackTab: "tasks",
    });
    render(<CommandPalette />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "打开终端" } });
    fireEvent.click(screen.getByText("打开终端"));

    expect(useAppStore.getState()).toMatchObject(defaultTerminalLocation === "bottom" ? {
      appMode, activeBottomTab: "terminal", dockCollapsed: false, rightPanelOpen: false,
    } : { appMode, activeBottomTab: "git", dockCollapsed: true, rightStackTab: "terminal", rightPanelOpen: true });
  });

  it.each(["cowork", "code"] as const)("removes internal launchers and scopes editor commands in %s", (appMode) => {
    useAppStore.setState({ appMode, runtimeSession: null, runtimeCapabilities: null });
    render(<CommandPalette />);
    for (const label of ["打开检查器", "打开运行状态", "运行诊断", "智能体编辑器"]) {
      expect(screen.queryByRole("option", { name: new RegExp(label) })).toBeNull();
    }
    for (const label of ["打开编辑器", "搜索项目内容", "查看代码问题", "管理预览服务", "打开预览",
      "检测开发服务器", "打开当前应用预览", "聚焦编辑器", "最大化当前面板", "关闭当前面板", "重置工作区布局", "打开 Git",
      "新建隔离会话", "切换主题", "打开子智能体"]) {
      expect(Boolean(screen.queryByRole("option", { name: new RegExp(`^${label}`) }))).toBe(appMode === "code");
    }
    for (const label of ["打开差异面板", "打开上下文", "打开浏览器", "打开终端", "快速打开文件"]) {
      expect(screen.getByRole("option", { name: new RegExp(`^${label}`) })).toBeTruthy();
    }
  });

  it.each([
    ["cowork", "打开上下文", "tasks"], ["cowork", "打开差异面板", "diff"],
    ["code", "打开上下文", "tasks"], ["code", "打开差异面板", "diff"],
  ] as const)("routes %s / %s without changing the chat, draft, approval or main slots", (appMode, label, tab) => {
    const approval = { requestId: "approval-kept", conversationId: "conv-kept", toolName: "run_command", args: { command: "pwd" } };
    const attachment = { id: "file-kept", name: "notes.txt", type: "text/plain", size: 1, status: "ready" as const };
    const quote = { id: "quote-kept", role: "assistant" as const, content: "Keep quoted text" };
    const panelSlots = [{ id: "main-chat", kind: "chat" as const, label: "Chat", focused: true },
      { id: "main-editor", kind: "editor" as const, label: "Editor" }];
    useAppStore.setState({ appMode, conversationId: "conv-kept", runtimeSession: null, draft: "Keep unsent text",
      attachments: [attachment], quotedMessage: quote, pendingApproval: approval, panelSlots,
      rightPanelOpen: false, rightStackTab: "browser" });
    render(<CommandPalette />);
    fireEvent.click(screen.getByRole("option", { name: new RegExp(`^${label}`) }));
    expect(useAppStore.getState()).toMatchObject({ appMode, conversationId: "conv-kept", draft: "Keep unsent text",
      attachments: [attachment], quotedMessage: quote, pendingApproval: approval, panelSlots,
      rightPanelOpen: true, rightStackTab: tab, commandPaletteOpen: false });
  });

  it("opens a browser tab through the actual request producer without switching the layout", () => {
    useAppStore.setState({ appMode: "cowork", conversationId: "conv-browser", runtimeSession: null,
      rightPanelOpen: false, rightStackTab: "tasks", draft: "Keep browser draft" });
    const requests: unknown[] = [];
    const unsubscribe = subscribeBrowserRequests(request => requests.push(request));
    render(<CommandPalette />);
    fireEvent.click(screen.getByRole("option", { name: /^打开浏览器/ }));
    unsubscribe();
    expect(requests).toEqual([expect.objectContaining({ kind: "new", conversationId: "conv-browser", url: "" })]);
    expect(useAppStore.getState()).toMatchObject({ appMode: "cowork", draft: "Keep browser draft",
      rightPanelOpen: true, rightStackTab: "browser", commandPaletteOpen: false });
  });

  it("sends /plan to the backend-owned slash executor", async () => {
    useAppStore.setState({
      runtimeSession: null,
      permissionMode: "auto",
      rightStackTab: "preview",
      rightPanelOpen: false,
      slashCommands: [
        {
          name: "plan",
          command: "plan",
          label: "/plan",
          description: "Switch to plan mode",
          type: "local",
        },
      ],
    });

    render(<CommandPalette />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "plan" } });
    // The matched substring is highlighted, so the label spans multiple elements.
    fireEvent.click(screen.getByRole("option", { name: /运行 \/plan/ }));

    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/plan",
      backendContent: "/plan",
      skipLocalAppend: true,
    }));
    expect(useAppStore.getState().permissionMode).toBe("auto");
    expect(useAppStore.getState().rightStackTab).toBe("preview");
  });

  it("opens Quick Open from the palette without leaving stacked modals", () => {
    useAppStore.setState({
      runtimeSession: null,
      runtimeCapabilities: {
        feature_flags: {
          global_search: { enabled: true, source: "default" },
        },
      },
      commandPaletteOpen: true,
      quickOpenVisible: false,
      settingsOpen: false,
      shortcutsHelpOpen: false,
      skillsMarketplaceOpen: false,
      liveArtifactsOpen: false,
    });

    render(<CommandPalette />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "快速打开文件" } });
    fireEvent.click(screen.getByText("快速打开文件"));

    const state = useAppStore.getState();
    expect(state.quickOpenVisible).toBe(true);
    expect(state.commandPaletteOpen).toBe(false);
    expect(state.settingsOpen).toBe(false);
    expect(state.shortcutsHelpOpen).toBe(false);
    expect(state.skillsMarketplaceOpen).toBe(false);
    expect(state.liveArtifactsOpen).toBe(false);
  });

  it("hides feature-flagged palette actions when disabled", () => {
    useAppStore.setState({
      runtimeSession: null,
      runtimeCapabilities: {
        feature_flags: {
          global_search: { enabled: false, source: "settings" },
          agent_editor: { enabled: false, source: "settings" },
        },
      },
      commandPaletteOpen: true,
      slashCommands: [],
    });

    render(<CommandPalette />);

    expect(screen.queryByText("快速打开文件")).toBeNull();
    expect(screen.queryByText("智能体编辑器")).toBeNull();
  });

  it("treats Open settings as an open action instead of a toggle", () => {
    useAppStore.setState({
      runtimeSession: null,
      commandPaletteOpen: true,
      settingsOpen: true,
      shortcutsHelpOpen: false,
      skillsMarketplaceOpen: false,
      liveArtifactsOpen: false,
      slashCommands: [],
    });

    render(<CommandPalette />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "打开设置" } });
    fireEvent.click(screen.getByText("打开设置"));

    const state = useAppStore.getState();
    expect(state.settingsOpen).toBe(true);
    expect(state.commandPaletteOpen).toBe(false);
  });

  it("routes first-class slash actions through the backend", async () => {
    useAppStore.setState({
      runtimeSession: null,
      conversationId: "conv-1",
      skillsMarketplaceOpen: false,
    });

    render(<CommandPalette />);

    fireEvent.click(screen.getByText("清空会话"));
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/clear",
      backendContent: "/clear",
      skipLocalAppend: true,
    }));
    expect(showConfirm).not.toHaveBeenCalled();

    cleanup();
    useAppStore.setState({ commandPaletteOpen: true });
    render(<CommandPalette />);
    fireEvent.click(screen.getByText("压缩上下文"));
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/compact",
      backendContent: "/compact",
      skipLocalAppend: true,
    }));

    cleanup();
    useAppStore.setState({ commandPaletteOpen: true });
    render(<CommandPalette />);
    fireEvent.click(screen.getByText("技能市场"));
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/skills",
      backendContent: "/skills",
      skipLocalAppend: true,
    }));
  });

  it("groups localized navigation and workbench actions without duplicate terminal entries", () => {
    useAppStore.setState({ runtimeSession: null, slashCommands: [] });

    render(<CommandPalette />);

    expect(screen.getByPlaceholderText("搜索命令或会话…")).toBeTruthy();
    expect(Array.from(document.querySelectorAll("[data-palette-group]")).map((node) => node.textContent)).toEqual([
      "导航",
      "工作区",
      "命令",
    ]);
    expect(screen.getAllByText("打开终端")).toHaveLength(1);
  });

  it("keeps recent conversation shortcuts free of workspace labels", () => {
    useAppStore.setState({
      runtimeSession: null,
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "当前会话", updatedAt: "2026-08-07T00:00:00.000Z" },
        {
          id: "conv-recent",
          title: "最近会话",
          updatedAt: "2026-08-06T00:00:00.000Z",
          workspaceRoot: "C:\\Desktop\\冒险岛",
          gitBranch: "main",
        },
      ],
      slashCommands: [],
    });

    render(<CommandPalette />);

    expect(screen.getByText("Ctrl+1")).toBeTruthy();
    expect(screen.queryByText(/冒险岛|Computer|main/)).toBeNull();
    expect(document.querySelector(".command-palette-hint, .mc-kbd")?.textContent).toBe("Ctrl+1");
  });
});

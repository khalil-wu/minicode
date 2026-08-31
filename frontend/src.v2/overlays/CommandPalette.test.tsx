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
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      commandPaletteOpen: true,
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

    fireEvent.keyDown(window, { key: "Escape" });

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

    fireEvent.keyDown(window, { key: "Escape" });

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

  it("opens Terminal in the bottom drawer", () => {
    useAppStore.setState({
      runtimeSession: null,
      appMode: "cowork",
      dockCollapsed: true,
      activeBottomTab: "git",
    });
    render(<CommandPalette />);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "打开终端" } });
    fireEvent.click(screen.getByText("打开终端"));

    expect(useAppStore.getState()).toMatchObject({
      appMode: "code",
      activeBottomTab: "terminal",
      dockCollapsed: false,
    });
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

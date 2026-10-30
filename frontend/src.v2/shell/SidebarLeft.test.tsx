/* @vitest-environment jsdom */
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
import { useAppStore, waitEditorDraftRecovery } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, sendConversationDeleteCommand } from "../protocol/ws-outbox";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import type { ChatMessage } from "../stores/types";
import { LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_RAIL_MIN_WIDTH, LEFT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { closeAutomations, getAutomationsPageState } from "../lib/automations-navigation";

const renderSidebarShell = (props: { embedded?: boolean; onNavigate?: () => void } = {}) => render(<>
  <HeaderBar leftPanelAvailable={false} leftPanelOpen={false} rightPanelAvailable={false} rightPanelOpen={false}
    onToggleLeftPanel={vi.fn()} onToggleRightPanel={vi.fn()} />
  <SidebarLeft {...props} />
</>);
const chooseMode = (name: "代码" | "聊天") => {
  const trigger = screen.getByRole("button", { name: "切换工作模式" });
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole("menuitem", { name: name === "代码" ? "代码" : "协作", exact: true }));
  return trigger;
};
const initialWindowWidth = window.innerWidth;

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
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1280 });
    localStorage.removeItem("minicode.sidebar.conversations.state");
    localStorage.removeItem("minicode.layout.left-width");
    localStorage.removeItem("minicode.layout.left-expanded-width");
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
      leftSidebarExpandedWidth: 280,
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
    window.innerWidth = initialWindowWidth;
  });

  it("keeps an already open extensions page open when its navigation item is clicked", () => {
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills" });
    render(<SidebarLeft />);
    fireEvent.click(screen.getByRole("button", { name: "插件", exact: true }));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");
  });

  it("uses declared installed-plugin icons in the current workspace and theme without guessing brands", async () => {
    const navigate = vi.fn();
    const fetchPlugins = vi.fn(async () => Response.json({ plugins: [
      { id: "local@team", name: "local", displayName: "Local Brand", path: "C:/plugins/local", marketplace: "team", enabled: false,
        iconVariant: "logo", iconVariants: ["logo", "logo-dark"] },
      { id: "remote@team", name: "remote", displayName: "Remote Brand", path: "C:/plugins/remote", marketplace: "team", enabled: true,
        iconUrl: "https://assets.example/plugin.png" },
      { id: "figma@team", name: "figma", path: "C:/plugins/plain", marketplace: "team", enabled: true },
    ] }));
    vi.stubGlobal("fetch", fetchPlugins);
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "plugins", resolvedTheme: "light", draft: "keep plugin draft" });
    try {
      render(<SidebarLeft withGlobalRail onNavigate={navigate} />);
      const local = await screen.findByRole("button", { name: "Local Brand", exact: true });
      const light = local.querySelector("img")!;
      const asset = new URL(light.src);
      expect(asset.pathname).toBe("/api/plugins/asset");
      expect(asset.searchParams.get("plugin_path")).toBe("C:/plugins/local");
      expect(asset.searchParams.get("workspace_root")).toBe("C:\\Desktop\\MiniCode");
      expect(asset.searchParams.get("variant")).toBe("logo");
      fireEvent.load(light);
      expect(light.hidden).toBe(false);
      const remote = screen.getByRole("button", { name: "Remote Brand", exact: true }).querySelector("img")!;
      expect(remote.src).toBe("https://assets.example/plugin.png");
      fireEvent.load(remote);
      expect(remote.hidden).toBe(false);
      const plain = screen.getByRole("button", { name: "figma", exact: true });
      expect(plain.querySelector("img")).toBeNull();
      expect(plain.querySelector(".brand-icon")?.getAttribute("data-brand")).toBe("generic");
      act(() => useAppStore.setState({ resolvedTheme: "dark" }));
      const dark = local.querySelector("img")!;
      expect(new URL(dark.src).searchParams.get("variant")).toBe("logo-dark");
      fireEvent.load(dark);
      expect(dark.hidden).toBe(false);
      fireEvent.click(local);
      expect(useAppStore.getState()).toMatchObject({ skillsMarketplaceSelectedPluginId: "local@team", draft: "keep plugin draft" });
      expect(navigate).toHaveBeenCalledOnce();
      expect(fetchPlugins).toHaveBeenCalledOnce();
    } finally { cleanup(); vi.unstubAllGlobals(); }
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

  it("drags without transition lag and retains folder content when collapsed", async () => {
    useAppStore.setState({ leftSidebarWidth: 300, leftSidebarExpandedWidth: 300 });
    localStorage.setItem("minicode.layout.left-width", "300");
    localStorage.setItem("minicode.layout.left-expanded-width", "300");
    const { container } = render(<SidebarLeft />);
    const content = container.querySelector(".mc-sidebar-mode-content");
    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    handle.setPointerCapture = vi.fn();
    fireEvent(handle, new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 300 }));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 348 }));
    await waitFor(() => expect(useAppStore.getState().leftSidebarWidth).toBe(348));
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(300);
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("300");
    expect(localStorage.getItem("minicode.layout.left-expanded-width")).toBe("300");
    expect(document.body.classList.contains("layout-dragging")).toBe(true);
    fireEvent(handle, new MouseEvent("pointerup", { bubbles: true, clientX: 348 }));
    expect(document.body.classList.contains("layout-dragging")).toBe(false);
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("348");
    expect(localStorage.getItem("minicode.layout.left-expanded-width")).toBe("348");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(358);
    useAppStore.getState().setLeftSidebarWidth(0);
    expect(content!.isConnected).toBe(true);
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(358);
  });

  it("keeps keyboard sizes and persistence in total panel units with an external rail", () => {
    useAppStore.getState().setLeftSidebarWidth(340);
    const { container } = render(<SidebarLeft withGlobalRail />);
    const aside = container.querySelector<HTMLElement>(".mc-sidebar-left")!;
    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    expect(aside.style.width).toBe("calc(340px - var(--desktop-rail-width))");
    expect(handle.getAttribute("aria-valuenow")).toBe("340");
    expect(handle.getAttribute("aria-valuemin")).toBe(String(LEFT_SIDEBAR_RAIL_MIN_WIDTH));
    fireEvent.keyDown(handle, { key: "ArrowRight", shiftKey: true });
    expect(useAppStore.getState().leftSidebarWidth).toBe(380);
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("380");
    fireEvent.keyDown(handle, { key: "Home" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(useAppStore.getState().leftSidebarWidth).toBe(290);
    expect(aside.style.width).toBe("calc(290px - var(--desktop-rail-width))");
    fireEvent.keyDown(handle, { key: "End" });
    expect(useAppStore.getState().leftSidebarWidth).toBe(520);
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(useAppStore.getState().leftSidebarWidth).toBe(340);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.doubleClick(handle);
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(340);
  });

  it("starts a global-rail drag from the total, then collapses and restores without losing its draft", async () => {
    useAppStore.getState().setLeftSidebarWidth(340);
    useAppStore.setState({ draft: "global rail resize draft" });
    const { container } = render(<SidebarLeft withGlobalRail />);
    const aside = container.querySelector<HTMLElement>(".mc-sidebar-left")!;
    const inner = container.querySelector<HTMLElement>(".mc-sidebar-left-inner")!;
    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    handle.setPointerCapture = vi.fn();
    fireEvent(handle, new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 340 }));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 380 }));
    await waitFor(() => expect(useAppStore.getState().leftSidebarWidth).toBe(380));
    expect(aside.style.width).toBe("calc(380px - var(--desktop-rail-width))");
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("340");
    fireEvent(handle, new MouseEvent("pointerup", { bubbles: true, clientX: 380 }));
    expect(localStorage.getItem("minicode.layout.left-expanded-width")).toBe("380");
    fireEvent(handle, new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 380 }));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 145 }));
    await waitFor(() => expect(useAppStore.getState().leftSidebarWidth).toBe(290));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 144 }));
    await waitFor(() => expect(useAppStore.getState().leftSidebarWidth).toBe(0));
    fireEvent(handle, new MouseEvent("pointerup", { bubbles: true, clientX: 144 }));
    expect(aside.style.width).toBe("0px");
    expect(inner.isConnected).toBe(true);
    expect(inner.style.width).toBe("calc(380px - var(--desktop-rail-width))");
    act(() => useAppStore.getState().setLeftSidebarWidth(useAppStore.getState().leftSidebarExpandedWidth));
    expect(aside.style.width).toBe("calc(380px - var(--desktop-rail-width))");
    expect(useAppStore.getState().draft).toBe("global rail resize draft");
    expect(document.body.classList.contains("layout-dragging")).toBe(false);
  });

  it("keeps an embedded drawer at full width without subtracting the external rail twice", () => {
    useAppStore.getState().setLeftSidebarWidth(340);
    const { container } = render(<SidebarLeft embedded withGlobalRail />);
    expect(container.querySelector<HTMLElement>(".mc-sidebar-left")!.style.width).toBe("100%");
    expect(container.querySelector<HTMLElement>(".mc-sidebar-left-inner")!.style.width).toBe("100%");
    expect(screen.queryByRole("separator", { name: "调整左侧栏宽度" })).toBeNull();
    expect(useAppStore.getState().leftSidebarWidth).toBe(340);
  });

  it("collapses below half the minimum and retains the saved expanded width and draft", async () => {
    useAppStore.getState().setLeftSidebarWidth(300);
    useAppStore.setState({ draft: "unfinished resize draft" });
    const { container } = render(<SidebarLeft />);
    const content = container.querySelector(".mc-sidebar-mode-content");
    const handle = screen.getByRole("separator", { name: "调整左侧栏宽度" });
    handle.setPointerCapture = vi.fn();
    fireEvent(handle, new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 300 }));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 120 }));
    await waitFor(() => expect(useAppStore.getState().leftSidebarWidth).toBe(240));
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 119 }));
    await waitFor(() => expect(useAppStore.getState()).toMatchObject({ leftSidebarWidth: 0, leftSidebarExpandedWidth: 300 }));
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("300");
    fireEvent(handle, new MouseEvent("pointerup", { bubbles: true, clientX: 119 }));
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("0");
    expect(localStorage.getItem("minicode.layout.left-expanded-width")).toBe("300");
    expect(content!.isConnected).toBe(true);
    expect(useAppStore.getState().draft).toBe("unfinished resize draft");
    expect(document.body.classList.contains("layout-dragging")).toBe(false);
  });

  it("limits the rendered width to leave 240 for the main content without overwriting the saved width", () => {
    useAppStore.getState().setLeftSidebarWidth(520);
    const { container } = render(<SidebarLeft />);
    const aside = container.querySelector<HTMLElement>(".mc-sidebar-left")!;
    window.innerWidth = 600;
    fireEvent(window, new Event("resize"));
    expect(aside.style.width).toBe("360px");
    expect(useAppStore.getState()).toMatchObject({ leftSidebarWidth: 520, leftSidebarExpandedWidth: 520 });
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("520");
    window.innerWidth = 1280;
    fireEvent(window, new Event("resize"));
    expect(aside.style.width).toBe("520px");
  });

  it("uses the actual workspace width as the workspace is resized", () => {
    let measured: ResizeObserverCallback;
    const observe = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { measured = callback; }
      observe = observe;
      disconnect() {}
    });
    useAppStore.getState().setLeftSidebarWidth(520);
    const { container } = render(<div className="mc-desktop-workspace"><SidebarLeft /></div>);
    const workspace = container.querySelector(".mc-desktop-workspace")!;
    expect(observe).toHaveBeenCalledWith(workspace);
    act(() => measured([{ target: workspace, contentRect: { width: 580 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(container.querySelector<HTMLElement>(".mc-sidebar-left")!.style.width).toBe("340px");
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("520");
  });

  it("uses the shell width including the global rail while retaining the requested total", () => {
    let measured: ResizeObserverCallback;
    const observe = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { measured = callback; }
      observe = observe;
      disconnect() {}
    });
    useAppStore.getState().setLeftSidebarWidth(520);
    const { container } = render(<div className="mc-desktop-body"><nav className="mc-navigation-rail" /><div className="mc-desktop-workspace"><SidebarLeft withGlobalRail /></div></div>);
    const body = container.querySelector(".mc-desktop-body")!;
    expect(observe).toHaveBeenCalledWith(body);
    act(() => measured([{ target: body, contentRect: { width: 600 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(screen.getByRole("separator", { name: "调整左侧栏宽度" }).getAttribute("aria-valuenow")).toBe("360");
    expect(container.querySelector<HTMLElement>(".mc-sidebar-left")!.style.width).toBe("calc(360px - var(--desktop-rail-width))");
    expect(localStorage.getItem("minicode.layout.left-width")).toBe("520");
    expect(useAppStore.getState().leftSidebarExpandedWidth).toBe(520);
    vi.unstubAllGlobals();
  });

  it("opens the brand mode menu and retains the conversation and draft", () => {
    useAppStore.setState({ draft: "unfinished brand draft" });
    render(<SidebarLeft />);
    const navigation = screen.getByRole("navigation", { name: "工作区导航" });
    expect(within(navigation).getByRole("button", { name: "新聊天" })).toBeTruthy();
    expect(within(navigation).queryByRole("button", { name: "搜索" })).toBeNull();
    expect(screen.getByRole("button", { name: "搜索" }).closest(".mc-sidebar-heading")).toBeTruthy();
    expect(screen.getByRole("button", { name: "添加项目" }).closest(".mc-sidebar-project-heading")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "切换项目" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "代码" }));
    expect(useAppStore.getState()).toMatchObject({ appMode: "code", conversationId: "conv-restored", draft: "unfinished brand draft" });
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(screen.queryByLabelText("MiniCode · 切换工作模式")).toBeNull();
  });

  it("moves the brand menu focus in the actual arrow direction without changing the draft", () => {
    useAppStore.setState({ draft: "Unsent keyboard navigation draft", appMode: "cowork" });
    render(<SidebarLeft />);
    fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
    const menu = screen.getByRole("menu", { name: "工作模式" });
    const chat = within(menu).getByRole("menuitem", { name: "协作", exact: true });
    const code = within(menu).getByRole("menuitem", { name: "代码", exact: true });
    expect(document.activeElement).toBe(chat);
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(document.activeElement).toBe(code);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(chat);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(code);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(chat);
    expect(useAppStore.getState().draft).toBe("Unsent keyboard navigation draft");
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "工作模式" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "切换工作模式" }));
  });

  it("keeps restored task status in the real conversation list without the removed activity button", () => {
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
    expect(screen.queryByRole("button", { name: "查看任务状态" })).toBeNull();
    expect(screen.queryByRole("button", { name: "返回项目与最近" })).toBeNull();
    expect(within(screen.getByRole("region", { name: "最近" })).getByText("Waiting task")).toBeTruthy();
    expect(screen.getByLabelText("等待回复")).toBeTruthy();
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

  it("keeps project navigation in place when Code opens from the brand menu", () => {
    useAppStore.setState({
      appMode: "cowork",
      workingDirectory: "",
    });

    renderSidebarShell();

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
    renderSidebarShell();
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

  it("returns to conversations through the brand mode menu", () => {
    useAppStore.setState({ appMode: "code" });
    renderSidebarShell();

    const choice = chooseMode("聊天");

    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(choice.getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("completes embedded sidebar navigation through its actual mode menu", () => {
    const onNavigate = vi.fn();
    useAppStore.setState({ appMode: "code" });

    renderSidebarShell({ embedded: true, onNavigate });
    chooseMode("聊天");

    expect(onNavigate).toHaveBeenCalledOnce();
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
  });

  it("uses one brand mode menu and retains the selected mode when reopened", () => {
    const { container } = renderSidebarShell();
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(container.querySelector(".mc-header-mode-switch")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "应用导航" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "切换工作模式" })).toHaveLength(1);
    expect(container.querySelector(".mc-sidebar-left")!.querySelector('[role="tablist"]')).toBeNull();
    chooseMode("代码");
    expect(useAppStore.getState().appMode).toBe("code");
    fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
    expect(screen.getByRole("menuitem", { name: "代码", exact: true }).getAttribute("aria-current")).toBe("true");
    fireEvent.click(screen.getByRole("menuitem", { name: "协作", exact: true }));
    expect(useAppStore.getState().appMode).toBe("cowork");
  });

  it("keeps IME keys inside the mode picker and restores focus only on explicit Escape", () => {
    render(<SidebarLeft />);
    const trigger = screen.getByRole("button", { name: "切换工作模式" });
    fireEvent.click(trigger);
    const current = document.activeElement!;
    fireEvent.keyDown(current, { key: "Escape", isComposing: true });
    fireEvent.keyDown(current, { key: "ArrowDown", keyCode: 229 });
    expect(screen.getByRole("menu", { name: "工作模式" })).toBeTruthy();
    expect(document.activeElement).toBe(current);
    const outside = vi.fn();
    document.addEventListener("keydown", outside);
    try {
      fireEvent.keyDown(current, { key: "Escape" });
      expect(screen.queryByRole("menu", { name: "工作模式" })).toBeNull();
      expect(document.activeElement).toBe(trigger);
      expect(outside).not.toHaveBeenCalled();
    } finally { document.removeEventListener("keydown", outside); }
    fireEvent.click(trigger);
    const search = screen.getByRole("button", { name: "搜索", exact: true });
    act(() => search.focus());
    expect(screen.queryByRole("menu", { name: "工作模式" })).toBeNull();
    expect(document.activeElement).toBe(search);
  });

  it.each(["mode", "profile"])("retires the %s portal when its sidebar closes without reopening it later", menu => {
    useAppStore.setState({ leftSidebarWidth: LEFT_SIDEBAR_DEFAULT_WIDTH });
    render(<SidebarLeft />);
    fireEvent.click(screen.getByRole("button", { name: menu === "mode" ? "切换工作模式" : "打开个人资料菜单" }));
    expect(screen.getByRole("menu")).toBeTruthy();
    act(() => useAppStore.getState().setLeftSidebarWidth(0));
    expect(screen.queryByRole("menu")).toBeNull();
    act(() => useAppStore.getState().setLeftSidebarWidth(LEFT_SIDEBAR_DEFAULT_WIDTH));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: menu === "mode" ? "切换工作模式" : "打开个人资料菜单" }).getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps settings in the persistent sidebar footer", () => {
    useAppStore.setState({ settingsOpen: false });
    render(<SidebarLeft />);

    const profile = screen.getByRole("button", { name: "打开个人资料菜单" });
    expect(profile.closest(".mc-sidebar-footer")).toBeTruthy();
    fireEvent.click(profile);
    expect(useAppStore.getState().settingsOpen).toBe(false);
    fireEvent.click(screen.getByRole("menuitem", { name: /^设置/ }));
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("keeps appearance in Settings and omits the removed footer theme toggle", () => {
    render(<SidebarLeft />);
    expect(screen.queryByRole("button", { name: "切换到浅色模式" })).toBeNull();
    expect(screen.queryByRole("button", { name: "切换到深色模式" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开个人资料菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /^设置/ }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "general" });
    expect(useAppStore.getState().themeMode).toBe("dark");
  });

  it("keeps task, scheduled, plugin and profile navigation in the real sidebar", () => {
    render(<SidebarLeft />);
    expect(screen.getByRole("button", { name: "新聊天" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "搜索", exact: true })).toBeTruthy();
    expect(screen.getByText("Restored pending prompt")).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开个人资料菜单" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "插件", exact: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: "已安排", exact: true })).toBeTruthy();
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

  it("routes scheduled tasks to the independent Scheduled page without opening Settings", () => {
    closeAutomations();
    useAppStore.setState({ settingsOpen: false, settingsTab: "general" });
    render(<SidebarLeft />);

    fireEvent.click(screen.getByRole("button", { name: "已安排" }));
    expect(getAutomationsPageState().open).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, settingsTab: "general" });

    useAppStore.getState().setSettingsTab("general");
    fireEvent.click(screen.getByRole("button", { name: "已安排" }));
    expect(getAutomationsPageState().open).toBe(true);
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, settingsTab: "general" });
    closeAutomations();
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

    renderSidebarShell();

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
    // Cold editor recovery is independent of the new-chat command. Capture
    // its settled array so this checks the command's ownership boundary.
    await waitEditorDraftRecovery();
    const settledEditorTabs = useAppStore.getState().editorTabs;
    expect(settledEditorTabs).toStrictEqual(editorTabs);

    render(<SidebarLeft />);
    vi.mocked(sendClientCommand).mockClear();
    fireEvent.click(screen.getByRole("button", { name: "新聊天" }));

    await waitFor(() => expect(useAppStore.getState().appMode).toBe("cowork"));
    const state = useAppStore.getState();
    expect(state.conversationDrafts[state.activeConversationDraftId!]).toMatchObject({ bindWorkspace: true, workspaceRoot: "C:\\Desktop\\MiniCode" });
    expect(state.appMode).toBe("cowork");
    expect(state.workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(state).toMatchObject({ activeTabPath: "README.md", activeEditorPath: "README.md" });
    expect(state.editorTabs).toBe(settledEditorTabs);
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

    await waitFor(() => {
      const state = useAppStore.getState();
      expect(state.conversationDrafts[state.activeConversationDraftId!]).toMatchObject({ bindWorkspace: true, workspaceRoot: "C:\\Desktop\\MiniCode" });
    });
    expect(useAppStore.getState().workingDirectory).toBe("C:\\Desktop\\MiniCode");
    expect(useAppStore.getState().appMode).toBe("cowork");
  });
});

/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

vi.mock("../desktop/runtime", () => ({
  desktop: () => nativeDesktop,
  isDesktop: () => nativeDesktop !== null,
  runtime: () => runtimeState,
}));

const reconnect = vi.hoisted(() => vi.fn());
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ reconnect }) }));

vi.mock("./SidebarLeft", () => ({
  SidebarLeft: ({ onNavigate }: { onNavigate?: () => void }) => (
    <button type="button" data-testid="left-sidebar" onClick={onNavigate}>Left sidebar</button>
  ),
}));

vi.mock("./SidebarRight", () => ({
  SidebarRight: ({ visible }: { visible?: boolean }) => <button type="button" data-testid="right-sidebar" data-visible={String(visible)} data-initial-tab={useAppStore.getState().rightStackTab}>Right sidebar</button>,
}));

vi.mock("./MainSlots", () => ({
  MainSlots: ({ forceChat }: { forceChat?: boolean }) => (
    <main>{forceChat ? "Chat" : "Code workspace"}<textarea data-composer-input aria-label="主对话输入" /></main>
  ),
}));
vi.mock("../panels/SideChatPanel", () => ({ SideChatPanel: () => null }));
vi.mock("../overlays/SkillsMarketplace", () => ({ SkillsMarketplace: () => <main>Extensions page</main> }));
vi.mock("../chat/ChatPane", () => ({ ChatPane: () => <main>Chat</main> }));

import { useAppStore } from "../stores";
import { WorkbenchShell } from "./WorkbenchShell";

let runtimeState: { runtimeToken: string } | null = { runtimeToken: "test-token" };
let nativeDesktop: { windowControls: { minimize: () => void; maximize: () => void; close: () => void } } | null = null;

describe("WorkbenchShell narrow navigation", () => {
  beforeEach(() => {
    runtimeState = { runtimeToken: "test-token" };
    nativeDesktop = null;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    useAppStore.setState({
      skillsMarketplaceOpen: false,
      settingsOpen: false,
      settingsTab: "general",
      appMode: "code",
      isConnected: true,
      connectionPhase: "connecting",
      reconnectAttempt: 0,
      reconnectMaxAttempts: null,
      connectionError: null,
      themeMode: "dark",
      leftSidebarWidth: 320,
      rightPanelOpen: true,
      rightPanelExpanded: false,
      rightStackTab: "tasks",
      previewArtifact: null,
      dockCollapsed: true,
      sideChatOpen: false,
      connectionPhase: "connecting",
      reconnectAttempt: 0,
      reconnectMaxAttempts: null,
      connectionError: null,
      conversationId: "conversation-1",
      pendingConversationSwitchId: null,
      conversations: [{ id: "conversation-1", title: "Test", updatedAt: "2026-07-11T00:00:00Z" }],
      messages: [{ id: "message-1", role: "user", content: "hello", artifacts: [], timestamp: 1 }],
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorTabs: [],
      activeTabPath: null,
      draft: "",
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps real header, navigation and native window controls visible while settings hides only the mounted workspace", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    const minimize = vi.fn();
    nativeDesktop = { windowControls: { minimize, maximize: vi.fn(), close: vi.fn() } };
    const { container } = render(<WorkbenchShell />);
    const titlebar = screen.getByRole("banner");
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    const input = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    const sidebar = screen.getByTestId("right-sidebar");
    fireEvent.change(input, { target: { value: "unfinished workspace draft" } });
    act(() => useAppStore.setState({ settingsOpen: true }));
    const workspace = container.querySelector<HTMLElement>(".mc-desktop-workspace")!;
    expect(workspace.hidden).toBe(true);
    expect(workspace.style.display).toBe("none");
    expect(screen.queryByRole("textbox", { name: "主对话输入" })).toBeNull();
    expect(screen.getByRole("banner")).toBe(titlebar);
    expect(screen.getByRole("navigation", { name: "应用导航" })).toBe(rail);
    expect(titlebar.closest("[hidden]")).toBeNull();
    expect(rail.closest("[hidden]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "最小化" }));
    expect(minimize).toHaveBeenCalledOnce();
    expect(input.isConnected).toBe(true);
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.dataset.visible).toBe("false");
    act(() => useAppStore.setState({ settingsOpen: false }));
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(input);
    expect(input.value).toBe("unfinished workspace draft");
    expect(workspace.hidden).toBe(false);
  });

  it("keeps a compact sidebar instance hidden during settings and restores its drawer", () => {
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    const sidebar = screen.getByTestId("left-sidebar");
    act(() => useAppStore.setState({ settingsOpen: true }));
    expect(sidebar.isConnected).toBe(true);
    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Left sidebar" })).toBeNull();
    act(() => useAppStore.setState({ settingsOpen: false }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
    expect(screen.getByTestId("left-sidebar")).toBe(sidebar);
  });

  it("uses the existing side panel as the primary workspace and releases it for composer focus", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    useAppStore.setState({ rightStackTab: "browser" });
    const { container } = render(<WorkbenchShell />);
    const workspace = screen.getByText("Code workspace");
    const sidebar = screen.getByTestId("right-sidebar");
    act(() => useAppStore.getState().setRightPanelExpanded(true));
    expect(container.querySelector<HTMLElement>(".workbench-primary")!.dataset.floating).toBe("true");
    expect(screen.getByRole("textbox", { name: "主对话输入" }).isConnected).toBe(true);
    expect(workspace.isConnected).toBe(true);
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.getAttribute("data-visible")).toBe("true");
    act(() => window.dispatchEvent(new Event("composer:focus")));
    expect(useAppStore.getState().rightPanelExpanded).toBe(false);
    expect(container.querySelector<HTMLElement>(".workbench-primary")!.style.display).toBe("flex");
    expect(screen.getByText("Code workspace")).toBe(workspace);
  });

  it("expands a narrow drawer into the workspace without retaining a modal focus trap", () => {
    useAppStore.setState({ rightStackTab: "browser" });
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    act(() => useAppStore.getState().setRightPanelExpanded(true));
    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-visible")).toBe("true");
    act(() => useAppStore.getState().setRightPanelExpanded(false));
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
  });

  it("preserves the workbench and sidebars while visiting plugins and skills", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    render(<WorkbenchShell />);
    const workspace = screen.getByText("Code workspace");
    const sidebar = screen.getByTestId("right-sidebar");
    act(() => useAppStore.setState({ skillsMarketplaceOpen: true }));
    await screen.findByText("Extensions page");
    expect(screen.getByTestId("left-sidebar")).toBeTruthy();
    expect(workspace.isConnected).toBe(true);
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.dataset.visible).toBe("false");
    act(() => useAppStore.setState({ skillsMarketplaceOpen: false }));
    expect(screen.getByText("Code workspace")).toBe(workspace);
    expect(sidebar.dataset.visible).toBe("true");
  });

  it("opens the existing sidebars as drawers and closes them with Escape", () => {
    render(<WorkbenchShell />);

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
    expect(screen.getByTestId("left-sidebar")).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("left-sidebar")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar")).toBeTruthy();
  });

  it("releases the compact drawer when review or preview context returns to the composer", () => {
    useAppStore.setState({ rightStackTab: "diff" });
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    const sidebar = screen.getByTestId("right-sidebar");
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();

    act(() => window.dispatchEvent(new Event("composer:focus")));

    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();
    expect(sidebar.isConnected).toBe(true);
    expect(sidebar.dataset.visible).toBe("false");
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "diff" });
    const composer = screen.getByRole("textbox", { name: "主对话输入" });
    composer.focus();
    const tab = new KeyboardEvent("keydown", { key: "Tab", cancelable: true, bubbles: true });
    composer.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(composer);
    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.dataset.visible).toBe("true");
  });

  it("keeps the wide review visible when context is added to the composer", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ rightStackTab: "diff" });
    render(<WorkbenchShell />);
    const sidebar = screen.getByTestId("right-sidebar");
    act(() => window.dispatchEvent(new Event("composer:focus")));
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.dataset.visible).toBe("true");
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, rightStackTab: "diff" });
  });

  it("uses semantic header icons and keeps the healthy connection status icon-only", () => {
    render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "命令面板" }).querySelector("svg.lucide-search")).toBeTruthy();
    expect(within(screen.getByRole("banner")).queryByRole("button", { name: "设置" })).toBeNull();
    expect(screen.getByRole("button", { name: "设置" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "后端已连接" }).textContent).toBe("");
    expect(screen.getByRole("status").textContent).toBe("后端已连接");
  });

  it("announces disconnect and recovery through one polite live region", () => {
    render(<WorkbenchShell />);
    const status = screen.getByRole("status");

    act(() => useAppStore.setState({ isConnected: false }));
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toContain("后端不可用");
    expect(document.querySelector(".mc-connection-banner")?.textContent).toContain("后端不可用");

    act(() => useAppStore.setState({ isConnected: true }));
    expect(status.textContent).toBe("后端已连接");
    expect(document.querySelector(".mc-connection-banner")).toBeNull();
  });

  it("announces each transport reconnect attempt and terminal failure", () => {
    render(<WorkbenchShell />);

    act(() => useAppStore.setState({
      isConnected: false,
      connectionPhase: "reconnecting",
      reconnectAttempt: 1,
      reconnectMaxAttempts: 5,
      connectionError: null,
    }));
    expect(screen.getByRole("status").textContent).toBe("正在重连 1/5");
    expect(document.querySelector(".mc-connection-banner")?.textContent).toContain("正在重连 1/5");
    expect(screen.getByRole("img", { name: "正在重连 1/5" }).getAttribute("data-kind")).toBe("reconnecting");

    act(() => useAppStore.setState({
      connectionPhase: "failed",
      connectionError: "连接认证已失效，请重新登录。",
    }));
    expect(screen.getByRole("status").textContent).toBe("连接认证已失效，请重新登录。");
    expect(document.querySelector(".mc-connection-banner")?.textContent).toContain("连接认证已失效");
  });

  it("does not render git or budget dock chrome in Code mode", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1200 });
    useAppStore.setState({ contextUsage: { used: 1_000, limit: 10_000 } });

    render(<WorkbenchShell />);

    expect(screen.queryByText("Budget")).toBeNull();
    expect(screen.queryByText("Git")).toBeNull();
    expect(screen.queryByTitle(/Context usage:/)).toBeNull();
  });

  it("uses drawer layout through the 1023px compact breakpoint", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1023 });
    render(<WorkbenchShell />);

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
  });

  it("keeps sidebars in drawers while the window cannot fit the full context card", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1199 });
    render(<WorkbenchShell />);

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
  });

  it("returns sidebars to the inline layout at 1600px", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    render(<WorkbenchShell />);

    expect(screen.getByTestId("left-sidebar")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
  });

  it("uses Chinese guidance when the browser preview has no desktop runtime token", () => {
    runtimeState = null;
    useAppStore.setState({ isConnected: false });

    render(<WorkbenchShell />);

    const banner = document.querySelector(".mc-connection-banner");
    expect(screen.getByRole("status").textContent).toContain("浏览器预览模式");
    expect(banner?.textContent).toContain("桌面功能暂不可用");
    expect(banner?.getAttribute("data-kind")).toBe("preview");
    expect(screen.getByRole("img", { name: "浏览器预览模式" }).querySelector("svg.lucide-monitor")).toBeTruthy();
  });

  it("keeps desktop tool regions mounted while their panels are closed", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ rightPanelOpen: false, dockCollapsed: true });
    render(<WorkbenchShell />);

    expect(screen.getByTestId("right-sidebar")).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开终端" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Terminal" })).toBeNull();
  });

  it("keeps a 1000px workbench out of the compressed three-column layout", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
    render(<WorkbenchShell />);

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-visible")).toBe("false");
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开右侧栏" })).toBeTruthy();
  });

  it("switches an already mounted workbench into compact drawers after resize", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600, writable: true });
    render(<WorkbenchShell />);
    expect(screen.getByTestId("left-sidebar")).toBeTruthy();

    act(() => {
      window.innerWidth = 1199;
      window.dispatchEvent(new Event("resize"));
    });

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
  });

  it("opens the compact right drawer for programmatic Preview navigation", () => {
    useAppStore.setState({ rightPanelOpen: false, rightStackTab: "tasks" });
    render(<WorkbenchShell />);

    act(() => useAppStore.setState({ rightPanelOpen: true, rightStackTab: "preview" }));

    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-initial-tab")).toBe("preview");
  });

  it("reopens the compact drawer for another attachment preview on the active tab", () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "preview" });
    render(<WorkbenchShell />);

    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭右侧面板" }));
    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();

    act(() => useAppStore.setState({
      previewArtifact: {
        artifactId: "attachment-2",
        content: "",
        name: "second.pdf",
        loading: true,
        source: "attachment",
        loadedAt: 2,
      },
    }));

    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-initial-tab")).toBe("preview");
  });

  it("keeps the desktop sidebar mounted when switching between Cowork and Code", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ appMode: "cowork", conversations: [], messages: [] });
    render(<WorkbenchShell />);

    const sidebar = screen.getByTestId("left-sidebar");
    fireEvent.click(screen.getByRole("button", { name: "Code" }));

    expect(screen.getByTestId("left-sidebar")).toBe(sidebar);
  });

  it.each([390, 1199, 1600])("keeps one Code/chat-home rail through navigation and resizing from %s px", (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
    useAppStore.setState({ appMode: "cowork", editorTabs: [{ id: "retained-file", path: "src/main.ts", content: "unsaved", original: "disk", loading: false }], draft: "retained chat question" });
    render(<WorkbenchShell />);
    const rail = screen.getByRole("navigation", { name: "应用导航" });
    const code = within(rail).getByRole("button", { name: "Code" });
    const home = within(rail).getByRole("button", { name: "聊天首页" });
    const composer = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: "local input draft" } });
    const messages = useAppStore.getState().messages;
    fireEvent.click(code);
    expect(useAppStore.getState().appMode).toBe("code");
    expect(screen.getByRole("navigation", { name: "应用导航" })).toBe(rail);
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(composer);
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("editor");
    act(() => { window.innerWidth = width < 1200 ? 1600 : 390; window.dispatchEvent(new Event("resize")); });
    expect(screen.getByRole("navigation", { name: "应用导航" })).toBe(rail);
    expect(screen.getAllByRole("button", { name: "Code" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "聊天首页" })).toHaveLength(1);
    fireEvent.click(home);
    expect(useAppStore.getState().appMode).toBe("cowork");
    expect(screen.getByText("Chat")).toBeTruthy();
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(composer);
    expect(composer.value).toBe("local input draft");
    expect(useAppStore.getState().messages).toBe(messages);
    expect(useAppStore.getState().conversationId).toBe("conversation-1");
    expect(useAppStore.getState().draft).toBe("retained chat question");
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved");
  });

  it("uses one wide global rail while retaining the conversation and main workspace", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    render(<WorkbenchShell />);
    expect(screen.getAllByRole("navigation", { name: "应用导航" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "设置", exact: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Git 与工作树" })).toBeTruthy();
    const workspace = screen.getByText("Code workspace");
    const sidebar = screen.getByTestId("left-sidebar");
    fireEvent.click(screen.getByRole("button", { name: "Git 与工作树" }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "workspaceGit", conversationId: "conversation-1" });
    expect(useAppStore.getState().messages[0].id).toBe("message-1");
    expect(screen.getByText("Code workspace")).toBe(workspace);
    expect(screen.getByTestId("left-sidebar")).toBe(sidebar);
  });

  it("keeps the collapsed sidebar mounted and restores its chosen width", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ leftSidebarWidth: 0, leftSidebarExpandedWidth: 336 });
    render(<WorkbenchShell />);

    const sidebar = screen.getByTestId("left-sidebar");
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByTestId("left-sidebar")).toBe(sidebar);
    expect(useAppStore.getState().leftSidebarWidth).toBe(336);
  });

  it("closes a narrow drawer after its content completes navigation", () => {
    render(<WorkbenchShell />);

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    fireEvent.click(screen.getByTestId("left-sidebar"));

    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
  });

  it("hides the narrow side-chat panel without closing its thread", () => {
    render(<WorkbenchShell />);
    act(() => useAppStore.getState().toggleSideChat());
    expect(screen.getByRole("dialog", { name: "右侧面板" }).getAttribute("tabindex")).toBe("-1");
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.click(screen.getByRole("button", { name: "关闭右侧面板" }));

    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();
    expect(useAppStore.getState().sideChatOpen).toBe(true);
    expect(document.body.style.overflow).toBe("");
  });

  it("keeps Side Chat and narrow drawers mutually exclusive", () => {
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();

    act(() => useAppStore.getState().toggleSideChat());

    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
  });

  it("keeps the same right panel mounted when a desktop is narrowed and hidden", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440, writable: true });
    render(<WorkbenchShell />);
    const panel = screen.getByTestId("right-sidebar");
    expect(screen.queryByRole("dialog")).toBeNull();
    act(() => { window.innerWidth = 390; window.dispatchEvent(new Event("resize")); });
    expect(screen.getByTestId("right-sidebar")).toBe(panel);
    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar")).toBe(panel);
  });

  it("keeps the right sidebar available in Chat without a second left sidebar control", () => {
    useAppStore.setState({ appMode: "chat" });
    render(<WorkbenchShell />);

    expect(screen.queryByRole("button", { name: /左侧栏/ })).toBeNull();
    expect(screen.getByRole("button", { name: /右侧栏/ })).toBeTruthy();
  });

  it("keeps workspace and tool controls available for an empty Cowork session", () => {
    useAppStore.setState({ appMode: "cowork", conversations: [], messages: [] });
    render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开右侧栏" })).toBeTruthy();
    expect(screen.getByText("Chat")).toBeTruthy();
    expect(screen.queryByText("Cowork")).toBeNull();
  });

  it("keeps the tool entry but no open right card for an empty Code chat", () => {
    useAppStore.setState({ appMode: "code", conversationId: null, conversations: [], messages: [] });
    render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开右侧栏" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-visible")).toBe("false");
    expect(screen.getByText("Code workspace")).toBeTruthy();
    expect(screen.queryByText("Cowork")).toBeNull();
  });

  it("hides sidebar controls while a Code panel is maximized", () => {
    render(<WorkbenchShell />);
    act(() => {
      useAppStore.setState({
        panelSlots: [{ id: "editor", kind: "editor", label: "Editor", focused: true, maximized: true }],
      });
    });

    expect(screen.queryByRole("button", { name: /左侧栏/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /右侧栏/ })).toBeNull();
  });
  it("offers a keyboard-accessible reconnect action after transport failure", () => {
    useAppStore.setState({ isConnected: false, connectionPhase: "failed", connectionError: "Service unavailable" });
    render(<WorkbenchShell />);
    const button = screen.getByRole("button", { name: "重新连接" });
    expect(button.closest('[aria-hidden="true"]')).toBeNull();
    fireEvent.click(button);
    expect(reconnect).toHaveBeenCalledOnce();
  });

});

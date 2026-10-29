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

vi.mock("../desktop/runtime", async original => ({
  ...await original<typeof import("../desktop/runtime")>(),
  desktop: () => nativeDesktop,
  isDesktop: () => nativeDesktop !== null,
  runtime: () => runtimeState,
  getDesktopPreferences: async () => ({ showBottomPanel: true }),
  onDesktopPreferencesChanged: () => () => {},
}));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: { showBottomPanel: true }, error: "", update: vi.fn() }) }));

const reconnect = vi.hoisted(() => vi.fn());
const workspaceRenders = vi.hoisted(() => vi.fn());
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ reconnect }) }));

vi.mock("../composer/useComposerPlugins", () => ({ useComposerPlugins: () => ({ plugins: [], loading: false, error: "", refresh: vi.fn() }) }));

vi.mock("./SidebarRight", () => ({
  SidebarRight: ({ visible }: { visible?: boolean }) => <button type="button" data-testid="right-sidebar" data-visible={String(visible)} data-initial-tab={useAppStore.getState().rightStackTab}>Right sidebar</button>,
}));

vi.mock("./MainSlots", () => ({
  MainSlots: ({ forceChat }: { forceChat?: boolean }) => {
    workspaceRenders();
    return <main>{forceChat ? "Chat" : "Code workspace"}<textarea data-composer-input aria-label="主对话输入" /></main>;
  },
}));
vi.mock("../panels/SideChatPanel", () => ({ SideChatPanel: () => null }));
vi.mock("../overlays/SkillsMarketplace", () => ({ SkillsMarketplace: () => <main>Extensions page</main> }));
vi.mock("./ProjectsPage", () => ({ ProjectsPage: () => <main>Projects page</main> }));
vi.mock("../overlays/ScheduledTasksPage", () => ({ ScheduledTasksPage: () => <main>Scheduled page<textarea aria-label="Scheduled task draft" /></main> }));
vi.mock("../chat/ChatPane", () => ({ ChatPane: () => <main>Chat</main> }));
vi.mock("../overlays/GeneralTab", () => ({ GeneralTab: () => <div>General settings content</div> }));
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(), fetchLLMSettings: vi.fn(async () => ({ provider: "custom", custom: {} })) }));

import { useAppStore } from "../stores";
import { WorkbenchShell } from "./WorkbenchShell";
import { SettingsCenter } from "../overlays/SettingsCenter";
import { closeAutomations, getAutomationsPageState } from "../lib/automations-navigation";

const leftSidebar = () => document.querySelector<HTMLElement>(".mc-sidebar-left");
const chooseMode = (name: "代码" | "协作") => {
  if (!screen.queryByRole("button", { name: "切换工作模式" })) fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
  fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
  fireEvent.click(screen.getByRole("menuitem", { name, exact: true }));
};

var runtimeState: { runtimeToken: string } | null = { runtimeToken: "test-token" };
var nativeDesktop: { windowControls: { minimize: () => void; maximize: () => void; close: () => void;
  getState: () => Promise<{ isMaximized: boolean }>; onStateChanged: () => () => void } } | null = null;

describe("WorkbenchShell narrow navigation", () => {
  beforeEach(() => {
    closeAutomations();
    runtimeState = { runtimeToken: "test-token" };
    nativeDesktop = null;
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    useAppStore.setState({
      skillsMarketplaceOpen: false,
      workingDirectory: "",
      recentWorkspaces: [],
      availableSkills: [],
      projectsOpen: false,
      settingsOpen: false,
      settingsTab: "general",
      appMode: "code",
      compactPanel: null,
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
    vi.unstubAllGlobals();
  });

  it("keeps native window controls live while Settings hides the mounted workspace and sidebar", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    const minimize = vi.fn();
    nativeDesktop = { windowControls: { minimize, maximize: vi.fn(), close: vi.fn(),
      getState: vi.fn(async () => ({ isMaximized: false })), onStateChanged: vi.fn(() => () => {}) } };
    const { container } = render(<WorkbenchShell />);
    const titlebar = screen.getByRole("banner");
    const left = leftSidebar()!;
    const input = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    const sidebar = screen.getByTestId("right-sidebar");
    fireEvent.change(input, { target: { value: "unfinished workspace draft" } });
    act(() => useAppStore.setState({ settingsOpen: true }));
    const workspace = container.querySelector<HTMLElement>(".mc-desktop-workspace")!;
    expect(workspace.hidden).toBe(true);
    expect(workspace.style.display).toBe("none");
    expect(screen.queryByRole("textbox", { name: "主对话输入" })).toBeNull();
    expect(screen.getByRole("banner")).toBe(titlebar);
    expect(leftSidebar()).toBe(left);
    expect(titlebar.closest("[hidden]")).toBeNull();
    expect(left.closest("[hidden]")).toBe(workspace);
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
    const sidebar = leftSidebar()!;
    act(() => useAppStore.setState({ settingsOpen: true }));
    expect(sidebar.isConnected).toBe(true);
    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
    expect(screen.queryByRole("button", { name: "切换工作模式" })).toBeNull();
    act(() => useAppStore.setState({ settingsOpen: false }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
    expect(leftSidebar()!).toBe(sidebar);
  });

  it("keeps the workspace out of parent capability notifications", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    const Parent = () => { useAppStore(state => state.runtimeCapabilities); return <WorkbenchShell />; };
    render(<Parent />);
    workspaceRenders.mockClear();
    act(() => useAppStore.setState({ runtimeCapabilities: { version: 1, tools: [], commands: [], skills: [], summary: {} } as never }));
    expect(workspaceRenders).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBeTruthy();
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
    expect(leftSidebar()!).toBeTruthy();
    expect(workspace.isConnected).toBe(true);
    expect(screen.getByTestId("right-sidebar")).toBe(sidebar);
    expect(sidebar.dataset.visible).toBe("false");
    act(() => useAppStore.setState({ skillsMarketplaceOpen: false }));
    expect(screen.getByText("Code workspace")).toBe(workspace);
    expect(sidebar.dataset.visible).toBe("true");
  });

  it("opens the existing sidebars as drawers and closes them with Escape", () => {
    render(<WorkbenchShell />);

    expect(leftSidebar()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
    expect(leftSidebar()!).toBeTruthy();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(leftSidebar()).toBeNull();

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
    const { container } = render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "命令面板" }).querySelector("svg.lucide-search")).toBeTruthy();
    expect(within(screen.getByRole("banner")).queryByRole("button", { name: /^设置/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("button", { name: "打开个人资料菜单" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "后端已连接" }).textContent).toBe("");
    expect(container.querySelector('.mc-desktop-shell > span[role="status"]')!.textContent).toBe("后端已连接");
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

    expect(leftSidebar()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
  });

  it("keeps sidebars in drawers while the window cannot fit the full context card", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1199 });
    render(<WorkbenchShell />);

    expect(leftSidebar()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();
  });

  it("returns sidebars to the inline layout at 1600px", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    render(<WorkbenchShell />);

    expect(leftSidebar()!).toBeTruthy();
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

    expect(leftSidebar()).toBeNull();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-visible")).toBe("false");
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开右侧栏" })).toBeTruthy();
  });

  it("switches an already mounted workbench into compact drawers after resize", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600, writable: true });
    render(<WorkbenchShell />);
    expect(leftSidebar()!).toBeTruthy();

    act(() => {
      window.innerWidth = 1199;
      window.dispatchEvent(new Event("resize"));
    });

    expect(leftSidebar()).toBeNull();
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
  });

  it("opens the compact right drawer for programmatic Preview navigation", () => {
    useAppStore.setState({ rightPanelOpen: false, rightStackTab: "tasks" });
    render(<WorkbenchShell />);

    act(() => useAppStore.setState({ rightPanelOpen: true, rightStackTab: "preview" }));

    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-initial-tab")).toBe("preview");
  });

  it("does not reopen a closed compact preview on async completion but opens a new request", () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "preview" });
    render(<WorkbenchShell />);

    fireEvent.click(screen.getByRole("button", { name: "打开右侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭右侧面板" }));
    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();

    act(() => useAppStore.setState({
      previewArtifact: {
        artifactId: "attachment-1",
        content: "",
        name: "first.pdf",
        loading: false,
        source: "attachment",
        loadedAt: 2,
      },
    }));
    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();

    act(() => useAppStore.getState().setRightStackTab("preview"));

    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").getAttribute("data-initial-tab")).toBe("preview");
  });

  it("moves an open wide preview into the drawer when the viewport becomes compact", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1600 });
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "preview", previewArtifact: {
      artifactId: "attachment-1", content: "", name: "first.pdf", loading: true, source: "attachment", loadedAt: 1,
    } });
    render(<WorkbenchShell />);
    expect(screen.queryByRole("dialog", { name: "右侧面板" })).toBeNull();
    act(() => { Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 }); window.dispatchEvent(new Event("resize")); });
    expect(screen.getByRole("dialog", { name: "右侧面板" })).toBeTruthy();
  });

  it("keeps the desktop sidebar mounted when switching between Cowork and Code", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ appMode: "cowork", conversations: [], messages: [] });
    render(<WorkbenchShell />);

    const sidebar = leftSidebar()!;
    chooseMode("代码");

    expect(leftSidebar()!).toBe(sidebar);
  });

  it.each([390, 1199, 1600])("keeps the workspace and unsaved content through sidebar mode navigation and resizing from %s px", (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
    useAppStore.setState({ appMode: "cowork", editorTabs: [{ id: "retained-file", path: "src/main.ts", content: "unsaved", original: "disk", loading: false }], draft: "retained chat question" });
    render(<WorkbenchShell />);
    const composer = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: "local input draft" } });
    const messages = useAppStore.getState().messages;
    chooseMode("代码");
    expect(useAppStore.getState().appMode).toBe("code");
    expect(screen.getAllByRole("navigation", { name: "应用导航" })).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(composer);
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("editor");
    act(() => { window.innerWidth = width < 1200 ? 1600 : 390; window.dispatchEvent(new Event("resize")); });
    chooseMode("协作");
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

  it("opens Settings through the persistent global rail profile while retaining the workspace", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    render(<><WorkbenchShell /><SettingsCenter /></>);
    expect(screen.getAllByRole("navigation", { name: "应用导航" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "打开个人资料菜单", exact: true })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Git 与工作树" })).toBeNull();
    const workspace = screen.getByText("Code workspace");
    const sidebar = leftSidebar()!;
    const input = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "Unsent settings round-trip draft" } });
    fireEvent.click(screen.getByRole("button", { name: "打开个人资料菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /^设置/ }));
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "general", conversationId: "conversation-1" });
    expect(useAppStore.getState().messages[0].id).toBe("message-1");
    expect(screen.getByText("Code workspace")).toBe(workspace);
    expect(leftSidebar()!).toBe(sidebar);
    expect(screen.queryByRole("textbox", { name: "主对话输入" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "首页" }));
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(input);
    expect(input.value).toBe("Unsent settings round-trip draft");
    expect(leftSidebar()).toBe(sidebar);
  });

  it("keeps the collapsed sidebar mounted and restores its chosen width", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ leftSidebarWidth: 0, leftSidebarExpandedWidth: 336 });
    render(<WorkbenchShell />);

    const sidebar = leftSidebar()!;
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(leftSidebar()!).toBe(sidebar);
    expect(useAppStore.getState().leftSidebarWidth).toBe(336);
  });

  it("closes a narrow drawer after its content completes navigation", () => {
    render(<WorkbenchShell />);

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    chooseMode("代码");

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

  it("keeps the Chat titlebar free of editor controls while the real panel route remains available", () => {
    useAppStore.setState({ appMode: "chat" });
    render(<WorkbenchShell />);

    expect(screen.queryByRole("button", { name: /左侧栏/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /右侧栏/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "命令面板" })).toBeNull();
    act(() => useAppStore.getState().setRightStackTab("browser"));
    expect(screen.getByTestId("right-sidebar").getAttribute("data-initial-tab")).toBe("browser");
  });

  it("uses the Source Cowork titlebar without adding editor palette, terminal, panel or connection controls", () => {
    useAppStore.setState({ appMode: "cowork", conversations: [], messages: [] });
    render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开右侧栏" })).toBeNull();
    expect(screen.queryByRole("button", { name: "打开终端" })).toBeNull();
    expect(screen.queryByRole("button", { name: "命令面板" })).toBeNull();
    expect(document.querySelector(".mc-connection-status")).toBeNull();
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

  it("opens Projects without remounting the hidden chat or discarding its draft", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    render(<WorkbenchShell />);
    const input = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "preserved project-switch draft" } });
    const sidebar = leftSidebar()!;
    act(() => useAppStore.setState({ projectsOpen: true }));
    expect(await screen.findByText("Projects page")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "主对话输入" })).toBeNull();
    expect(input.isConnected).toBe(true);
    expect(sidebar.isConnected).toBe(true);
    expect(sidebar.closest("[hidden]")).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").dataset.visible).toBe("false");
    act(() => useAppStore.setState({ projectsOpen: false }));
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(input);
    expect(input.value).toBe("preserved project-switch draft");
  });

  it("opens Scheduled beside the rail while keeping the hidden workspace and task draft mounted", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    useAppStore.setState({ appMode: "cowork" });
    render(<WorkbenchShell />);
    const input = screen.getByRole("textbox", { name: "主对话输入" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "preserved Scheduled navigation draft" } });
    const sidebar = leftSidebar()!;
    fireEvent.click(screen.getByRole("button", { name: "定时任务", exact: true }));
    expect(await screen.findByText("Scheduled page")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "主对话输入" })).toBeNull();
    expect(input.isConnected).toBe(true); expect(sidebar.isConnected).toBe(true); expect(sidebar.closest("[hidden]")).toBeTruthy();
    expect(screen.getByTestId("right-sidebar").dataset.visible).toBe("false");
    expect(useAppStore.getState().settingsOpen).toBe(false);
    const scheduledInput = screen.getByRole("textbox", { name: "Scheduled task draft" }) as HTMLTextAreaElement;
    fireEvent.change(scheduledInput, { target: { value: "unsubmitted Scheduled task" } });
    fireEvent.click(screen.getByRole("button", { name: "收起左侧栏" }));
    expect(getAutomationsPageState().sidebarOpen).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "首页", exact: true }));
    expect(screen.getByRole("textbox", { name: "主对话输入" })).toBe(input); expect(input.value).toBe("preserved Scheduled navigation draft");
    expect(scheduledInput.isConnected).toBe(true); expect(scheduledInput.closest("[hidden]")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "定时任务", exact: true }));
    expect(screen.getByRole("textbox", { name: "Scheduled task draft" })).toBe(scheduledInput); expect(scheduledInput.value).toBe("unsubmitted Scheduled task");
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

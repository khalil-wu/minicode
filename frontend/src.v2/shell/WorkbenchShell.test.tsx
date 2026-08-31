/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  desktop: () => null,
  isDesktop: () => false,
  runtime: () => runtimeState,
}));

vi.mock("./SidebarLeft", () => ({
  SidebarLeft: ({ onNavigate }: { onNavigate?: () => void }) => (
    <button type="button" data-testid="left-sidebar" onClick={onNavigate}>Left sidebar</button>
  ),
}));

vi.mock("./SidebarRight", () => ({
  SidebarRight: ({ initialTab }: { initialTab?: string }) => <button type="button" data-testid="right-sidebar" data-initial-tab={initialTab}>Right sidebar</button>,
}));

vi.mock("./MainSlots", () => ({
  MainSlots: ({ forceChat }: { forceChat?: boolean }) => (
    <main>{forceChat ? "Chat" : "Code workspace"}</main>
  ),
}));
vi.mock("../panels/SideChatPanel", () => ({ SideChatPanel: () => null }));
vi.mock("../chat/ChatPane", () => ({ ChatPane: () => <main>Chat</main> }));

import { useAppStore } from "../stores";
import { WorkbenchShell } from "./WorkbenchShell";

let runtimeState: { runtimeToken: string } | null = { runtimeToken: "test-token" };

describe("WorkbenchShell narrow navigation", () => {
  beforeEach(() => {
    runtimeState = { runtimeToken: "test-token" };
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    useAppStore.setState({
      appMode: "code",
      isConnected: true,
      connectionPhase: "connecting",
      reconnectAttempt: 0,
      reconnectMaxAttempts: null,
      connectionError: null,
      themeMode: "dark",
      leftSidebarWidth: 320,
      rightPanelOpen: true,
      previewArtifact: null,
      dockCollapsed: true,
      sideChatOpen: false,
      connectionPhase: "connecting",
      reconnectAttempt: 0,
      reconnectMaxAttempts: null,
      connectionError: null,
      conversationId: "conversation-1",
      conversations: [{ id: "conversation-1", title: "Test", updatedAt: "2026-07-11T00:00:00Z" }],
      messages: [{ id: "message-1", role: "user", content: "hello", artifacts: [], timestamp: 1 }],
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
    });
  });

  afterEach(() => {
    cleanup();
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

  it("uses semantic header icons and keeps the healthy connection status icon-only", () => {
    render(<WorkbenchShell />);

    expect(screen.getByRole("button", { name: "命令面板" }).querySelector("svg.lucide-search")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "设置" })).toBeNull();
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
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1599 });
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
    expect(screen.queryByTestId("right-sidebar")).toBeNull();
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开右侧栏" })).toBeTruthy();
  });

  it("switches an already mounted workbench into compact drawers after resize", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600, writable: true });
    render(<WorkbenchShell />);
    expect(screen.getByTestId("left-sidebar")).toBeTruthy();

    act(() => {
      window.innerWidth = 1599;
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
    act(() => useAppStore.setState({ appMode: "code" }));

    expect(screen.getByTestId("left-sidebar")).toBe(sidebar);
  });

  it("fully removes a collapsed desktop sidebar from the layout", () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1600 });
    useAppStore.setState({ leftSidebarWidth: 0 });
    render(<WorkbenchShell />);

    expect(screen.queryByTestId("left-sidebar")).toBeNull();
    expect(screen.getByRole("button", { name: "打开左侧栏" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByTestId("left-sidebar")).toBeTruthy();
  });

  it("closes a narrow drawer after its content completes navigation", () => {
    render(<WorkbenchShell />);

    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    fireEvent.click(screen.getByTestId("left-sidebar"));

    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
  });

  it("traps Side Chat focus and restores the page after close", () => {
    useAppStore.setState({ sideChatOpen: true });
    render(<WorkbenchShell />);

    expect(screen.getByRole("dialog", { name: "侧边对话" }).getAttribute("tabindex")).toBe("-1");
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.click(screen.getByRole("button", { name: "关闭侧边对话" }));

    expect(screen.queryByRole("dialog", { name: "侧边对话" })).toBeNull();
    expect(document.body.style.overflow).toBe("");
  });

  it("keeps Side Chat and narrow drawers mutually exclusive", () => {
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    expect(screen.getByRole("dialog", { name: "左侧栏" })).toBeTruthy();

    act(() => useAppStore.setState({ sideChatOpen: true }));

    expect(screen.queryByRole("dialog", { name: "左侧栏" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "侧边对话" })).toBeTruthy();
  });

  it("returns Side Chat focus to stable shell UI when a Cowork drawer trigger unmounts", () => {
    useAppStore.setState({ appMode: "cowork", conversations: [], messages: [] });
    render(<WorkbenchShell />);
    fireEvent.click(screen.getByRole("button", { name: "打开左侧栏" }));
    screen.getByTestId("left-sidebar").focus();

    act(() => useAppStore.setState({ sideChatOpen: true }));
    fireEvent.click(screen.getByRole("button", { name: "关闭侧边对话" }));

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "命令面板" }));
  });

  it("hides sidebar controls in modes where no sidebar can render", () => {
    useAppStore.setState({ appMode: "chat" });
    render(<WorkbenchShell />);

    expect(screen.queryByRole("button", { name: /左侧栏/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /右侧栏/ })).toBeNull();
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
    expect(screen.queryByTestId("right-sidebar")).toBeNull();
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
});

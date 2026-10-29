/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

vi.mock("../chat/ChatPane", () => ({
  ChatPane: () => {
    const owner = useAppStore((state) => state.conversationId);
    return <div data-testid="chat-owner-fixture" data-conversation-owner={owner}>Chat pane<textarea aria-label="Chat input" /></div>;
  },
}));

vi.mock("../panels/EditorPanel", () => ({
  EditorPanel: () => <div>Editor pane<input aria-label="Editor input" /></div>,
}));
vi.mock("../panels/FileViewerPanel", () => ({
  FileViewerPanel: ({ tabId, active }: { tabId: string; active: boolean }) => <div data-testid={`file-viewer-${tabId}`} data-active={active}>File {tabId}</div>,
}));

import { useAppStore } from "../stores";
import { MainSlots } from "./MainSlots";
import { SidebarLeft } from "./SidebarLeft";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../chat/openWebInBrowser";

const chooseMode = (name: "代码" | "协作") => {
  fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
  fireEvent.click(screen.getByRole("menuitem", { name, exact: true }));
};
const expectCodeSelected = () => {
  fireEvent.click(screen.getByRole("button", { name: "切换工作模式" }));
  expect(screen.getByRole("menuitem", { name: "代码", exact: true }).getAttribute("aria-current")).toBe("true");
  fireEvent.keyDown(screen.getByRole("menu", { name: "工作模式" }), { key: "Escape" });
};
const LayoutFixture = () => {
  const layout = useAppStore((state) => state.workbenchLayout);
  const appMode = useAppStore((state) => state.appMode);
  return <><SidebarLeft embedded /><MainSlots mode={appMode === "code" ? layout : "tabs"} forceChat={appMode !== "code"} /></>;
};

describe("MainSlots", () => {
  it("opens an explicit Home file reference without changing the current workspace or draft", async () => {
    useAppStore.setState({ conversationId: null, workingDirectory: "/home-B", draft: "Retain Home B draft", workbenchLayout: "split",
      workspaceFileTabs: [{ id: "home-A-file", owner: { scopeId: "home:/home-A", conversationId: null, executionCwd: "/home-A", browserRoot: "/home-A" },
        path: "file.ts", title: "Home A file", open: true, pinned: true, pane: "left" }],
      panelSlots: [{ id: "chat", kind: "chat" }, { id: "home-file", kind: "file", fileTabId: "home-A-file", focused: true }],
    });
    render(<MainSlots mode="split" />);
    const file = await screen.findByTestId("file-viewer-home-A-file");
    expect(file.getAttribute("data-active")).toBe("true");
    fireEvent.mouseDown(screen.getByLabelText("Chat input"));
    expect(file.getAttribute("data-active")).toBe("true");
    expect(screen.getByRole("tab", { name: "Home A file" }).getAttribute("aria-selected")).toBe("true");
    expect(useAppStore.getState()).toMatchObject({ workingDirectory: "/home-B", draft: "Retain Home B draft" });
  });
  it("keeps the last selected auxiliary file when chatting, and hides other owners without deleting them", async () => {
    const owner = { scopeId: "conversation:owner-A", conversationId: "owner-A", executionCwd: "/a", browserRoot: "/a" };
    useAppStore.setState({ conversationId: "owner-A", workingDirectory: "/a", workbenchLayout: "split",
      editorTabs: [{ path: "unsaved.ts", content: "Keep this unsaved", original: "", language: "typescript" }],
      workspaceFileTabs: ["first", "second"].map(id => ({ id, owner, path: `${id}.ts`, title: id, open: true, pinned: false, pane: "left" as const })),
      panelSlots: [{ id: "chat", kind: "chat" }, { id: "editor", kind: "editor" },
        { id: "file-first", kind: "file", fileTabId: "first" }, { id: "file-second", kind: "file", fileTabId: "second", focused: true }],
    });
    render(<MainSlots mode="split" />);
    const second = await screen.findByTestId("file-viewer-second");
    expect(second.getAttribute("data-active")).toBe("true");
    fireEvent.mouseDown(screen.getByLabelText("Chat input"));
    expect(second.getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("file-viewer-first").getAttribute("data-active")).toBe("false");
    act(() => useAppStore.setState({ conversationId: "owner-B", workingDirectory: "/b" }));
    expect(second.getAttribute("data-active")).toBe("false");
    expect(useAppStore.getState().workspaceFileTabs).toHaveLength(2);
    expect(useAppStore.getState().editorTabs[0].content).toBe("Keep this unsaved");
  });
  let surfaceWidth = 1200;
  let notifySurfaceResize: () => void;
  beforeEach(() => {
    surfaceWidth = 1200;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, surfaceWidth, 800));
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { notifySurfaceResize = callback; }
      observe() {}
      disconnect() {}
    });
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1200,
    });
    useAppStore.setState({
      appMode: "code",
      settingsOpen: false,
      isConnected: false,
      recentWorkspaces: [],
      projectsOpen: false,
      skillsMarketplaceOpen: false,
      conversationId: null,
      pendingConversationSwitchId: null,
      conversations: [],
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorTabs: [],
      activeTabPath: null,
      contextCardCollapsed: true,
      contextCardPinned: false,
      compactPanel: null,
      workspaceFileTabs: [],
      rightPanelOpen: false,
      rightPanelExpanded: false,
      rightStackTab: "preview",
      workbenchLayout: "tabs",
      diffReview: null,
    });
  });

  afterEach(() => {
    cleanup();
    __resetOpenWebInBrowserForTests();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("does not duplicate right side panel controls inside the workbench canvas", () => {
    render(<MainSlots />);

    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Activity" })).toBeNull();
    expect(screen.queryByRole("button", { name: /side panel/i })).toBeNull();
  });

  it("keeps the empty home canvas free of conversation-only controls", () => {
    useAppStore.setState({ conversationId: null, pendingConversationSwitchId: null });
    const { container } = render(<MainSlots />);
    expect(container.querySelector(".mc-workbench-toolbar")?.getAttribute("data-home")).toBe("true");
    expect(screen.queryByRole("button", { name: "聊天操作" })).toBeNull();
    expect(screen.queryByRole("button", { name: "切换摘要" })).toBeNull();
    expect(screen.getByRole("button", { name: "新建标签页" })).toBeTruthy();
  });

  it("focuses the floating composer without leaving the full workspace view", () => {
    useAppStore.setState({ rightPanelOpen: true, rightPanelExpanded: true, panelSlots: [
      { id: "main-chat", kind: "chat", label: "Chat", focused: false },
      { id: "editor", kind: "editor", label: "Editor", focused: true },
    ] });
    render(<MainSlots />);
    const input = screen.getByRole("textbox", { name: "Chat input" });
    fireEvent.mouseDown(input);
    fireEvent.focus(input);
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.id).toBe("main-chat");
    expect(useAppStore.getState().rightPanelExpanded).toBe(true);
    act(() => useAppStore.getState().focusPanel("editor"));
    expect(useAppStore.getState().rightPanelExpanded).toBe(false);
  });

  it("shows the active conversation title and opens the existing chat search", () => {
    useAppStore.setState({
      conversationId: "active-task",
      conversations: [{ id: "active-task", title: "Review the workspace", updatedAt: "2026-09-05" }],
    });
    const onSearch = vi.fn();
    window.addEventListener("chat:request-search", onSearch);
    render(<MainSlots mode="tabs" />);

    expect(screen.getByTitle("Review the workspace")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "搜索当前对话" }));
    expect(onSearch).toHaveBeenCalledTimes(1);
    window.removeEventListener("chat:request-search", onSearch);
  });

  it("shows a pending task title while preserving the canonical owner, messages, and mounted chat", () => {
    const messages = [{ id: "owner-message", role: "user" as const, content: "Owner A", timestamp: 1, artifacts: [] }];
    useAppStore.setState({ conversationId: "owner-A", messages, conversations: [
      { id: "owner-A", title: "Canonical task", updatedAt: "2026-10-05" },
      { id: "owner-B", title: "Requested task", updatedAt: "2026-10-05" },
    ] });
    render(<MainSlots mode="tabs" />);
    const chat = screen.getByTestId("chat-owner-fixture");
    act(() => useAppStore.setState({ pendingConversationSwitchId: "owner-B" }));
    expect(screen.getByTitle("Requested task")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "搜索当前对话" })).toBeNull();
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
    expect(chat.getAttribute("data-conversation-owner")).toBe("owner-A");
    expect(useAppStore.getState().messages).toBe(messages);
    expect(useAppStore.getState().conversationId).toBe("owner-A");
    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    expect(screen.getByTitle("Canonical task")).toBeTruthy();
    expect(screen.getByRole("button", { name: "搜索当前对话" })).toBeTruthy();
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
  });

  it("toggles the floating summary and opens actual chat actions without remounting chat", () => {
    useAppStore.setState({ conversationId: "owner-A", commandPaletteOpen: false,
      conversations: [{ id: "owner-A", title: "Chat actions", updatedAt: "2026-10-05" }],
    });
    render(<MainSlots mode="tabs" />);
    Object.defineProperty(document.querySelector('.mc-main-slot-frame[data-panel-slot-kind="chat"]'), "clientWidth", { value: 1200 });
    const chat = screen.getByTestId("chat-owner-fixture");
    const summary = screen.getByRole("button", { name: "切换摘要" });
    expect(summary.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(summary);
    expect(summary.getAttribute("aria-pressed")).toBe("true");
    expect(useAppStore.getState()).toMatchObject({ contextCardCollapsed: false, rightStackTab: "preview", rightPanelOpen: false, conversationId: "owner-A" });
    fireEvent.click(summary);
    expect(useAppStore.getState().contextCardCollapsed).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
    expect(screen.getByRole("menuitem", { name: "重命名" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "复制" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "导出会话树" })).toBeNull();
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
    expect(chat.getAttribute("data-conversation-owner")).toBe("owner-A");
  });

  it("uses the actual sidebar mode menu while retaining the chat and editor trees", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true },
        { id: "main-editor", kind: "editor", label: "File", focused: false },
      ],
    });
    render(<LayoutFixture />);
    chooseMode("代码");
    expectCodeSelected();
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("editor");
    await screen.findByText("Editor pane");
    chooseMode("协作");
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("flex");
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
    expect(screen.queryByRole("tablist", { name: "工作模式" })).toBeNull();
  });

  it("shows chat and an opened editor side by side on wide workbench windows", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-1", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<MainSlots />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "对话" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "README.md" })).toBeNull();
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
    const handle = screen.getByRole("separator", { name: "调整主面板宽度" });
    expect(handle.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect((useAppStore.getState().panelSlots[0].size ?? 0)).toBeGreaterThan(1);
    fireEvent.keyDown(handle, { key: "Enter" });
    expect(useAppStore.getState().panelSlots[0].size).toBeCloseTo(useAppStore.getState().panelSlots[1].size ?? 0, 5);
  });

  it("keeps compact Code on the editor and switches to chat through the sidebar brand menu", async () => {
    surfaceWidth = 700;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 700,
    });
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "editor-readme", kind: "editor", label: "README.md", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-2", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<LayoutFixture />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.getByRole("button", { name: "切换工作模式" })).toBeTruthy();
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
    chooseMode("协作");

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByText("Editor pane").closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
  });

  it("retains a visited empty editor while the chat is in front", async () => {
    useAppStore.setState({ appMode: "code", editorTabs: [], panelSlots: [
      { id: "main-chat", kind: "chat", focused: false, size: 1 },
      { id: "main-editor", kind: "editor", focused: true, size: 1 },
    ] });
    render(<LayoutFixture />);
    const input = await screen.findByRole("textbox", { name: "Editor input" });
    fireEvent.change(input, { target: { value: "unfinished file search" } });
    chooseMode("协作");
    expect(input.isConnected).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Editor input" })).toBeNull();
    expect(useAppStore.getState().editorTabs).toHaveLength(0);
    chooseMode("代码");
    expect(screen.getByRole("textbox", { name: "Editor input" })).toBe(input);
    expect((input as HTMLInputElement).value).toBe("unfinished file search");
  });

  it("adapts to sidebar space without changing the split preference or remounting either panel", async () => {
    useAppStore.setState({
      workbenchLayout: "split",
      panelSlots: [
        { id: "main-chat", kind: "chat", focused: true, size: 0.8 },
        { id: "main-editor", kind: "editor", focused: false, size: 1.2 },
      ],
      editorTabs: [{ id: "responsive-file", path: "src/main.ts", content: "unsaved", original: "disk", loading: false }],
      activeTabPath: "src/main.ts",
      draft: "Keep this question",
    });
    render(<LayoutFixture />);
    const editor = await screen.findByText("Editor pane");
    const chat = screen.getByText("Chat pane");
    const chatFrame = chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!;
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();

    act(() => screen.getByRole("textbox", { name: "Editor input" }).focus());
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("editor");
    act(() => { surfaceWidth = 760; notifySurfaceResize(); });
    expect(window.innerWidth).toBe(1200);
    expect(chatFrame.style.display).toBe("none");
    expectCodeSelected();
    expect(screen.queryByRole("separator", { name: "调整主面板宽度" })).toBeNull();
    expect(useAppStore.getState().workbenchLayout).toBe("split");

    act(() => useAppStore.getState().focusPanel("main-chat"));
    expect(chatFrame.style.display).toBe("none");
    expect(editor.closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("flex");
    act(() => { surfaceWidth = 1000; notifySurfaceResize(); });
    expect(chatFrame.style.display).toBe("flex");
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(screen.getByText("Editor pane")).toBe(editor);
    expect(useAppStore.getState().panelSlots.map((slot) => slot.size)).toEqual([0.8, 1.2]);
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved");
    expect(useAppStore.getState().draft).toBe("Keep this question");
  });

  it("keeps wide single-page Code on the editor and retains it behind the collaboration chat", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-3", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<LayoutFixture />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
    expect(screen.queryByRole("separator", { name: "调整主面板宽度" })).toBeNull();

    chooseMode("协作");

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByText("Editor pane").closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
  });

  it("keeps the chat tree mounted when switching between Cowork chat and a Code editor", async () => {
    const messages = [{ id: "retained-owner-message", role: "user" as const, content: "Keep this chat", timestamp: 1, artifacts: [] }];
    useAppStore.setState({
      appMode: "cowork",
      conversationId: "retained-owner",
      conversations: [{ id: "retained-owner", title: "Retained task", updatedAt: "2026-10-05" }],
      messages,
      draft: "unfinished draft",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-4", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<LayoutFixture />);
    const chatPane = screen.getByText("Chat pane");

    expect(screen.queryByRole("tab", { name: "文件" })).toBeNull();
    chooseMode("代码");

    const editorPane = await screen.findByText("Editor pane");
    expect(editorPane.closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("flex");
    expect(chatPane.closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(useAppStore.getState().appMode).toBe("code");
    expect(chatPane.getAttribute("data-conversation-owner")).toBe("retained-owner");
    expect(useAppStore.getState().messages).toBe(messages);
    expect(useAppStore.getState().draft).toBe("unfinished draft");

    chooseMode("协作");
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(screen.queryByRole("tab", { name: "文件" })).toBeNull();
    expect(chatPane.closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("flex");
    expect(editorPane.closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
    expect(chatPane.getAttribute("data-conversation-owner")).toBe("retained-owner");
    expect(useAppStore.getState().messages).toBe(messages);
    expect(useAppStore.getState().draft).toBe("unfinished draft");
  });

  it("moves focus and maximization through actual panel and sidebar mode controls while retaining both trees", async () => {
    useAppStore.setState({
      appMode: "code",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, maximized: false },
        { id: "main-editor", kind: "editor", label: "File", focused: true, maximized: true },
      ],
      editorTabs: [{ id: "editor-maximized-fixture", path: "src/main.ts", content: "draft", original: "disk", loading: false }],
      activeTabPath: "src/main.ts",
    });
    render(<LayoutFixture />);
    const editorPane = await screen.findByText("Editor pane");
    const chatPane = screen.getByText("Chat pane");
    const editorFrame = editorPane.closest<HTMLElement>('[data-panel-slot-kind="editor"]')!;
    const chatFrame = chatPane.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!;
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("none");
    fireEvent.click(screen.getByRole("button", { name: "退出专注模式" }));
    fireEvent.click(screen.getByRole("button", { name: "并排显示对话与文件" }));
    fireEvent.mouseDown(chatFrame);
    fireEvent.click(screen.getByRole("button", { name: "专注当前面板" }));
    expect(chatFrame.style.display).toBe("flex");
    expect(editorFrame.style.display).toBe("none");
    expect(useAppStore.getState().panelSlots).toMatchObject([
      { id: "main-chat", focused: true, maximized: true },
      { id: "main-editor", focused: false, maximized: false },
    ]);

    chooseMode("协作");
    expect(chatFrame.style.display).toBe("flex");
    chooseMode("代码");
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("flex");
    expect(useAppStore.getState().panelSlots).toMatchObject([
      { id: "main-chat", focused: false, maximized: false },
      { id: "main-editor", focused: true, maximized: false },
    ]);
    fireEvent.click(screen.getByRole("button", { name: "专注当前面板" }));
    expect(useAppStore.getState().panelSlots[1].maximized).toBe(true);
    expect(screen.getByText("Editor pane")).toBe(editorPane);
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(useAppStore.getState().editorTabs[0].content).toBe("draft");
  });

  it("lets the single Code editor fill the canvas even when a persisted split size is below one", () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true, size: 0.45 },
        { id: "main-editor", kind: "editor", label: "File", focused: false, size: 1.55 },
      ],
    });

    const { container } = render(<MainSlots mode="tabs" />);

    const frame = container.querySelector<HTMLElement>('[data-panel-slot-kind="editor"]');
    expect(frame?.style.display).toBe("flex");
    expect(frame?.style.flex).toBe("1 1 0px");
  });

  it("shows the empty Code editor without restoring removed Chat and File tabs", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: false, size: 1 },
      ],
      editorTabs: [],
      activeTabPath: null,
    });

    render(<MainSlots mode="tabs" />);

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(await screen.findByRole("textbox", { name: "Editor input" })).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
  });

  it("opens files from Cowork by switching into Code mode", () => {
    useAppStore.setState({
      appMode: "cowork",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorOpenRequests: [],
      activeEditorPath: null,
    });

    useAppStore.getState().openEditorFile("README.md", "README.md");

    const state = useAppStore.getState();
    expect(state.appMode).toBe("code");
    expect(state.panelSlots.some((slot) => slot.kind === "chat")).toBe(true);
    expect(state.panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
    expect(state.activeEditorPath).toBe("README.md");
  });

  it("retains the chat and editor while changing layout and exiting editor focus mode", async () => {
    useAppStore.setState({
      appMode: "code",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false },
        { id: "main-editor", kind: "editor", label: "File", focused: true },
      ],
      editorTabs: [{ id: "layout-buffer", path: "src/main.ts", content: "unsaved", original: "disk", loading: false }],
      activeTabPath: "src/main.ts",
    });
    render(<LayoutFixture />);
    const editor = await screen.findByText("Editor pane");
    const chat = screen.getByText("Chat pane");
    const editorFrame = editor.closest<HTMLElement>('[data-panel-slot-kind="editor"]')!;
    const chatFrame = chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!;
    fireEvent.click(screen.getByRole("button", { name: "并排显示对话与文件" }));
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("flex");
    expect(localStorage.getItem("minicode.layout.workbench")).toBe("split");
    fireEvent.click(screen.getByRole("button", { name: "专注当前面板" }));
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("none");
    fireEvent.click(screen.getByRole("button", { name: "退出专注模式" }));
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("flex");
    expect(screen.getByText("Editor pane")).toBe(editor);
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved");
  });

  it("returns from source to the review's actual preview page without losing the editor", async () => {
    useAppStore.setState({
      conversationId: "preview-task",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat" }, { id: "main-editor", kind: "editor", label: "File", focused: true }],
      editorTabs: [{ id: "preview-source", path: "src/app.ts", content: "draft", original: "disk", loading: false }],
      activeTabPath: "src/app.ts",
      diffReview: { requestId: "preview-review", conversationId: "preview-task", diff: "", files: [], status: "viewing", fileDecisions: {}, previewReturnTarget: { conversationId: "preview-task", tab: "browser", url: "http://localhost:3000/dashboard", targetId: "page-1" } },
    });
    render(<MainSlots mode="tabs" />);
    const editor = await screen.findByText("Editor pane");
    const onBrowserRequest = vi.fn();
    subscribeBrowserRequests(onBrowserRequest);
    fireEvent.click(screen.getByRole("button", { name: "返回预览" }));
    expect(onBrowserRequest).toHaveBeenCalledWith(expect.objectContaining({
      kind: "resume", conversationId: "preview-task", targetId: "page-1", url: "http://localhost:3000/dashboard",
    }));
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(useAppStore.getState().rightPanelOpen).toBe(true);
    expect(screen.getByText("Editor pane")).toBe(editor);
    expect(useAppStore.getState().editorTabs[0].content).toBe("draft");
  });

  it("retains chat reading state when entering and leaving the empty file page", async () => {
    useAppStore.setState({
      appMode: "cowork",
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }, { id: "main-editor", kind: "editor", label: "File", focused: false }],
    });
    render(<LayoutFixture />);
    const chat = screen.getByText("Chat pane");
    chooseMode("代码");
    await screen.findByText("Editor pane");
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!.style.display).toBe("none");
    chooseMode("协作");
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!.style.display).toBe("flex");
  });

  it.each(["tabs", "split"] as const)("keeps Code on the editor after chat focus and right-panel width changes (%s)", async (layout) => {
    useAppStore.setState({ workbenchLayout: layout, panelSlots: [
      { id: "main-chat", kind: "chat", focused: true, size: 1 },
      { id: "main-editor", kind: "editor", focused: false, size: 1 },
    ], editorTabs: [{ id: "keep-code-file", path: "src/main.ts", content: "retained", original: "disk", loading: false }] });
    render(<LayoutFixture />);
    const editor = await screen.findByText("Editor pane");
    const chat = screen.getByTestId("chat-owner-fixture");
    act(() => {
      useAppStore.getState().setRightStackTab("browser");
      surfaceWidth = 700;
      notifySurfaceResize();
      useAppStore.getState().focusPanel("main-chat");
    });
    expect(useAppStore.getState().appMode).toBe("code");
    expect(editor.closest<HTMLElement>('[data-panel-slot-kind="editor"]')!.style.display).toBe("flex");
    expect(chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!.style.display).toBe("none");
    expectCodeSelected();
    expect(screen.queryByRole("tablist", { name: "主工作区" })).toBeNull();
    expect(useAppStore.getState().workbenchLayout).toBe(layout);
    expect(useAppStore.getState().editorTabs[0].content).toBe("retained");
  });

  it("opens and closes the right pane in Chat while retaining the mounted conversation", () => {
    useAppStore.setState({ appMode: "chat", rightPanelOpen: false, contextCardCollapsed: false });
    render(<MainSlots forceChat />);
    const chat = screen.getByTestId("chat-owner-fixture");
    fireEvent.click(screen.getByRole("button", { name: "新建标签页" }));
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: true, appMode: "chat", contextCardCollapsed: true });
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
    act(() => useAppStore.getState().toggleRightPanel());
    expect(screen.getByRole("button", { name: "新建标签页" })).toBeTruthy();
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
  });

  it("opens a narrow summary without replacing the selected right content or mounted chat", () => {
    useAppStore.setState({ appMode: "chat", conversationId: "summary-owner", rightPanelOpen: false, contextCardCollapsed: true });
    render(<MainSlots forceChat />);
    Object.defineProperty(document.querySelector('.mc-main-slot-frame[data-panel-slot-kind="chat"]'), "clientWidth", { value: 600 });
    const chat = screen.getByTestId("chat-owner-fixture");
    fireEvent.click(screen.getByRole("button", { name: "切换摘要" }));
    expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: false, rightStackTab: "preview", contextCardCollapsed: false });
    expect(screen.getByTestId("chat-owner-fixture")).toBe(chat);
  });
});

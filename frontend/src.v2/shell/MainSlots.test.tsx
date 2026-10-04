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
  ChatPane: () => <div>Chat pane</div>,
}));

vi.mock("../panels/EditorPanel", () => ({
  EditorPanel: () => <div>Editor pane<input aria-label="Editor input" /></div>,
}));

import { useAppStore } from "../stores";
import { MainSlots } from "./MainSlots";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../chat/openWebInBrowser";

const LayoutFixture = () => {
  const layout = useAppStore((state) => state.workbenchLayout);
  return <MainSlots mode={layout} />;
};

describe("MainSlots", () => {
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
      conversationId: null,
      conversations: [],
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }],
      editorTabs: [],
      activeTabPath: null,
      rightPanelOpen: false,
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

  it("moves focus and selection together when navigating workbench tabs by keyboard", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true },
        { id: "main-editor", kind: "editor", label: "File", focused: false },
      ],
    });
    render(<MainSlots mode="tabs" />);
    const chat = screen.getByRole("tab", { name: "对话" });
    const editor = screen.getByRole("tab", { name: "文件" });

    chat.focus();
    fireEvent.keyDown(chat, { key: "ArrowRight" });
    expect(document.activeElement).toBe(editor);
    expect(editor.getAttribute("aria-selected")).toBe("true");
    expect(chat.tabIndex).toBe(-1);
    await screen.findByText("Editor pane");

    fireEvent.keyDown(editor, { key: "Home" });
    expect(document.activeElement).toBe(chat);
    expect(chat.getAttribute("aria-selected")).toBe("true");
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

  it("keeps compact windows on a single active slot with the switcher", async () => {
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

    render(<MainSlots />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
    const switcher = screen.getByRole("tablist", { name: "主工作区" });
    expect(switcher.style.width).toBe("164px");
    expect(screen.getByRole("tab", { name: "对话" }).style.whiteSpace).toBe("nowrap");

    fireEvent.click(screen.getByRole("tab", { name: "对话" }));

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByText("Editor pane").closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
    expect(useAppStore.getState().panelSlots.some((slot) => slot.kind === "editor")).toBe(true);
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
    expect(screen.getByRole("tab", { name: "文件" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByRole("separator", { name: "调整主面板宽度" })).toBeNull();
    expect(useAppStore.getState().workbenchLayout).toBe("split");

    fireEvent.click(screen.getByRole("tab", { name: "对话" }));
    act(() => { surfaceWidth = 1000; notifySurfaceResize(); });
    expect(chatFrame.style.display).toBe("flex");
    expect(screen.getByRole("separator", { name: "调整主面板宽度" })).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(screen.getByText("Editor pane")).toBe(editor);
    expect(useAppStore.getState().panelSlots.map((slot) => slot.size)).toEqual([0.8, 1.2]);
    expect(useAppStore.getState().editorTabs[0].content).toBe("unsaved");
    expect(useAppStore.getState().draft).toBe("Keep this question");
  });

  it("keeps Code mode on one main slot with Chat and File tabs on wide windows", async () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-3", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    render(<MainSlots mode="tabs" />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane").closest<HTMLElement>('[data-panel-slot-kind="chat"]')?.style.display).toBe("none");
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
    expect(screen.queryByRole("separator", { name: "调整主面板宽度" })).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "对话" }));

    expect(screen.getByText("Chat pane")).toBeTruthy();
    expect(screen.getByText("Editor pane").closest<HTMLElement>('[data-panel-slot-kind="editor"]')?.style.display).toBe("none");
  });

  it("keeps the chat tree mounted when switching between Cowork chat and a Code editor", async () => {
    useAppStore.setState({
      appMode: "cowork",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, size: 1 },
        { id: "main-editor", kind: "editor", label: "File", focused: true, size: 1 },
      ],
      editorTabs: [{ id: "editor-fixture-4", path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
    });

    const { rerender } = render(<MainSlots mode="tabs" forceChat />);
    const chatPane = screen.getByText("Chat pane");

    fireEvent.click(screen.getByRole("tab", { name: "文件" }));
    rerender(<MainSlots mode="tabs" />);

    expect(await screen.findByText("Editor pane")).toBeTruthy();
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(useAppStore.getState().appMode).toBe("code");
  });

  it("moves maximization, active tab, and visible content together while retaining both panel trees", async () => {
    useAppStore.setState({
      appMode: "code",
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: false, maximized: false },
        { id: "main-editor", kind: "editor", label: "File", focused: true, maximized: true },
      ],
      editorTabs: [{ id: "editor-maximized-fixture", path: "src/main.ts", content: "draft", original: "disk", loading: false }],
      activeTabPath: "src/main.ts",
    });
    render(<MainSlots mode="tabs" />);
    const editorPane = await screen.findByText("Editor pane");
    const chatPane = screen.getByText("Chat pane");
    const editorFrame = editorPane.closest<HTMLElement>('[data-panel-slot-kind="editor"]')!;
    const chatFrame = chatPane.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!;
    const editorTab = screen.getByRole("tab", { name: "文件" });
    const chatTab = screen.getByRole("tab", { name: "对话" });
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("none");

    fireEvent.click(chatTab);
    expect(chatTab.getAttribute("aria-selected")).toBe("true");
    expect(editorTab.getAttribute("aria-selected")).toBe("false");
    expect(chatFrame.style.display).toBe("flex");
    expect(editorFrame.style.display).toBe("none");
    expect(useAppStore.getState().panelSlots).toMatchObject([
      { id: "main-chat", focused: true, maximized: true },
      { id: "main-editor", focused: false, maximized: false },
    ]);

    fireEvent.keyDown(chatTab, { key: "ArrowRight" });
    expect(document.activeElement).toBe(editorTab);
    expect(editorTab.getAttribute("aria-selected")).toBe("true");
    expect(chatTab.getAttribute("aria-selected")).toBe("false");
    expect(editorFrame.style.display).toBe("flex");
    expect(chatFrame.style.display).toBe("none");
    expect(useAppStore.getState().panelSlots).toMatchObject([
      { id: "main-chat", focused: false, maximized: false },
      { id: "main-editor", focused: true, maximized: true },
    ]);
    fireEvent.click(editorTab);
    expect(useAppStore.getState().panelSlots[1].maximized).toBe(true);
    expect(screen.getByText("Editor pane")).toBe(editorPane);
    expect(screen.getByText("Chat pane")).toBe(chatPane);
    expect(useAppStore.getState().editorTabs[0].content).toBe("draft");
  });

  it("lets a single Code tab fill the canvas even when a persisted split size is below one", () => {
    useAppStore.setState({
      panelSlots: [
        { id: "main-chat", kind: "chat", label: "Chat", focused: true, size: 0.45 },
        { id: "main-editor", kind: "editor", label: "File", focused: false, size: 1.55 },
      ],
    });

    const { container } = render(<MainSlots mode="tabs" />);

    const frame = container.querySelector<HTMLElement>('[data-panel-slot-kind="chat"]');
    expect(frame?.style.flex).toBe("1 1 0px");
  });

  it("keeps the Chat and File switcher when no editor tabs are open", () => {
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
    expect(screen.queryByText("Editor pane")).toBeNull();
    expect(screen.getByRole("tab", { name: "对话" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "文件" })).toBeTruthy();
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

  it("retains the chat and editor while changing layout and transfers focus from a maximized editor", async () => {
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
    fireEvent.click(screen.getByRole("tab", { name: "对话" }));
    expect(chatFrame.style.display).toBe("flex");
    expect(editorFrame.style.display).toBe("none");
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
      panelSlots: [{ id: "main-chat", kind: "chat", label: "Chat", focused: true }, { id: "main-editor", kind: "editor", label: "File", focused: false }],
    });
    render(<MainSlots mode="tabs" />);
    const chat = screen.getByText("Chat pane");
    fireEvent.click(screen.getByRole("tab", { name: "文件" }));
    await screen.findByText("Editor pane");
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!.style.display).toBe("none");
    fireEvent.click(screen.getByRole("tab", { name: "对话" }));
    expect(screen.getByText("Chat pane")).toBe(chat);
    expect(chat.closest<HTMLElement>('[data-panel-slot-kind="chat"]')!.style.display).toBe("flex");
  });
});

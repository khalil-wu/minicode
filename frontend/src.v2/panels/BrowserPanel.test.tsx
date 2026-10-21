/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handlePreviewEvent } from "../chat/previewEvents";
import type { ServerEvent } from "../protocol/events";
import type { EmbeddedBrowserState, EmbeddedBrowserSettingsSnapshot } from "../desktop/runtime";
import { BrowserPanel, normalizeBrowserInput } from "./BrowserPanel";
import { useDesktopEvents } from "../hooks/useDesktopEvents";
import { ContextMenu } from "../components/ContextMenu";
import { __resetOpenWebInBrowserForTests, focusBrowserAddressBar, openWebInBrowser, refreshWebInBrowser, returnToBrowserPage } from "../chat/openWebInBrowser";
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null, error: "", update: vi.fn() }) }));
vi.mock("./browser-annotation-capture", () => ({ captureBrowserAnnotationAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], composerAttachments: [] })) }));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
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

const runtimeMocks = vi.hoisted(() => ({
  capture: vi.fn(async ({ expectedUrl }: { expectedUrl: string }) => ({ mimeType: "image/png" as const, data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", width: 1, height: 1, url: expectedUrl })),
  pageCommand: vi.fn(async () => ({ zoomFactor: 1, origin: "https://example.com", hostname: "example.com", secure: true, cookieCount: 2 })),
  create: vi.fn(async () => ({ id: "browser-created", type: "updated", url: "about:blank", title: "新标签页", loading: false, canGoBack: false, canGoForward: false })),
  list: vi.fn(async () => []),
  navigate: vi.fn(async (conversationId: string, id: string, url: string) => ({
    id,
    conversationId,
    type: "updated" as const,
    url,
    title: url,
    loading: false,
    canGoBack: false,
    canGoForward: false,
  })),
  activate: vi.fn(async () => true),
  setBounds: vi.fn(async () => true),
  runAction: vi.fn(async () => true),
  inspect: vi.fn(async () => ({ ok: true, value: [] })),
  getSettings: vi.fn(async () => ({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "block" as const, origin: "https://example.com", permissions: [] as string[] })),
  setSettings: vi.fn(async (payload: { downloadPolicy?: "block" | "ask" | "allow"; origin?: string; permission?: string; allowed?: boolean }) => ({
    downloadPolicy: payload.downloadPolicy ?? "block" as const,
    origin: payload.origin ?? "https://example.com",
    permissions: payload.permission && payload.allowed ? [payload.permission] : [],
  })),
  clearSiteData: vi.fn(async () => true),
  close: vi.fn(async () => true),
  onEvent: vi.fn((_callback: (event: EmbeddedBrowserState) => void) => () => {}),
  onSettingsChanged: vi.fn((_callback: (settings: EmbeddedBrowserSettingsSnapshot) => void) => () => {}),
  openExternal: vi.fn(async () => true),
}));
const nativeListeners = new Set<(event: EmbeddedBrowserState) => void>();
const emitNative = (event: EmbeddedBrowserState) => { for (const listener of [...nativeListeners]) listener(event); };
const feedbackMocks = vi.hoisted(() => ({ send: vi.fn(async (_options: Record<string, unknown>) => true) }));
vi.mock("../chat/sendChatMessage", () => ({ sendChatMessage: feedbackMocks.send }));

vi.mock("../desktop/runtime", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../desktop/runtime")>();
  return {
    ...actual,
    desktop: () => ({ embeddedBrowser: { pageCommand: runtimeMocks.pageCommand } }),
    isDesktop: () => true,
    embeddedBrowserCreate: runtimeMocks.create,
    embeddedBrowserCapture: runtimeMocks.capture,
    embeddedBrowserList: runtimeMocks.list,
    embeddedBrowserActivate: runtimeMocks.activate,
    embeddedBrowserSetBounds: runtimeMocks.setBounds,
    embeddedBrowserNavigate: runtimeMocks.navigate,
    embeddedBrowserRunAction: runtimeMocks.runAction,
    embeddedBrowserInspect: runtimeMocks.inspect,
    embeddedBrowserGetSettings: runtimeMocks.getSettings,
    embeddedBrowserSetSettings: runtimeMocks.setSettings,
    embeddedBrowserClearSiteData: runtimeMocks.clearSiteData,
    embeddedBrowserClose: runtimeMocks.close,
    onEmbeddedBrowserEvent: runtimeMocks.onEvent,
    onEmbeddedBrowserSettingsChanged: runtimeMocks.onSettingsChanged,
    openExternal: runtimeMocks.openExternal,
  };
});

const loadOccludedSnapshot = async () => {
  await waitFor(() => expect(document.querySelector(".mc-browser-occluded-snapshot")).not.toBeNull());
  fireEvent.load(document.querySelector(".mc-browser-occluded-snapshot")!);
};

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const pending = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

const page = (id: string, title: string, url: string, active = false) => ({
  id, title, url, active,
  conversationId: "conv-browser",
  type: "updated" as const,
  loading: false, canGoBack: false, canGoForward: false,
});

const openBrowserAction = (name: string) => {
  fireEvent.click(screen.getByRole("button", { name: "更多浏览器操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name }));
};

describe("BrowserPanel", () => {
  it("adopts a native child page without replaying its POST navigation and applies the saved new-tab device preference", async () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser", draft: "Keep source draft" });
    runtimeMocks.list.mockResolvedValueOnce([page("original-opener", "Original opener", "https://source.example/", true)]);
    runtimeMocks.getSettings.mockResolvedValueOnce({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: true,
      downloadPolicy: "block", origin: "https://example.com", permissions: [] });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Original opener" });
    await act(async () => { await Promise.resolve(); });
    act(() => emitNative({ ...page("native-child", "Original POST destination", "https://source.example/submitted"), type: "page" }));
    await screen.findByRole("tab", { name: "Original POST destination" });
    expect(screen.getByRole("group", { name: "设备工具栏" })).toBeTruthy();
    expect((screen.getByRole("spinbutton", { name: "视口宽度" }) as HTMLInputElement).value).toBe("390");
    expect(runtimeMocks.create).not.toHaveBeenCalled(); expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("Keep source draft");
  });

  it("routes native guest number shortcuts through the existing tab and chat consumers with the original owner", async () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser", draft: "Keep source draft" });
    runtimeMocks.list.mockResolvedValueOnce([page("active-native", "Active native page", "https://source.example/", true)]);
    const tab = vi.fn(), chat = vi.fn();
    window.addEventListener("select-numbered-tab", tab); window.addEventListener("select-numbered-chat", chat);
    try {
      render(<BrowserPanel />);
      await screen.findByRole("tab", { name: "Active native page" });
      act(() => emitNative({ ...page("active-native", "Active native page", "https://source.example/"), type: "numbered-shortcut", numberedShortcut: { target: "tabs", index: 2 } }));
      act(() => emitNative({ ...page("active-native", "Active native page", "https://source.example/"), type: "numbered-shortcut", numberedShortcut: { target: "sidebar", index: 1 } }));
      expect(tab).toHaveBeenCalledTimes(1); expect(chat).toHaveBeenCalledTimes(1);
      expect((tab.mock.calls[0][0] as CustomEvent).detail).toEqual({ index: 2, conversationId: "conv-browser", surface: "sidebar" });
      expect((chat.mock.calls[0][0] as CustomEvent).detail).toEqual({ index: 1, conversationId: "conv-browser", surface: "sidebar" });
      expect(useAppStore.getState().draft).toBe("Keep source draft");
      expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("select-numbered-tab", tab); window.removeEventListener("select-numbered-chat", chat);
    }
  });

  it("keeps native guest number commands out of other tabs, switched conversations and hidden surfaces", async () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser" });
    runtimeMocks.list.mockResolvedValueOnce([page("active-native", "Active native page", "https://source.example/", true)]);
    const select = vi.fn(); window.addEventListener("select-numbered-tab", select);
    try {
      render(<BrowserPanel />);
      await screen.findByRole("tab", { name: "Active native page" });
      const event: EmbeddedBrowserState = { ...page("active-native", "Active native page", "https://source.example/"), type: "numbered-shortcut", numberedShortcut: { target: "tabs", index: 1 } };
      act(() => emitNative({ ...event, id: "other-native", title: "Never create this page" }));
      act(() => emitNative({ ...event, conversationId: "retired-owner" }));
      act(() => useAppStore.setState({ pendingConversationSwitchId: "next-owner" }));
      act(() => emitNative(event));
      act(() => useAppStore.setState({ pendingConversationSwitchId: null, rightPanelOpen: false }));
      act(() => emitNative(event));
      expect(select).not.toHaveBeenCalled();
      expect(screen.queryByRole("tab", { name: "Never create this page" })).toBeNull();
    } finally { window.removeEventListener("select-numbered-tab", select); }
  });

  it("opens a native guest external link through the existing action without borrowing a new tab or conversation", async () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "browser", draft: "Keep source draft" });
    runtimeMocks.list.mockResolvedValueOnce([page("active-native", "Active native page", "https://source.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Active native page" });
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "https://unsent.example/draft" } });
    const event: EmbeddedBrowserState = { ...page("active-native", "Active native page", "https://source.example/"), type: "external-link-request", requestedUrl: "https://destination.example/actually-clicked" };
    act(() => emitNative(event));
    expect(runtimeMocks.openExternal).toHaveBeenCalledExactlyOnceWith("https://destination.example/actually-clicked");
    expect((address as HTMLInputElement).value).toBe("https://unsent.example/draft");
    expect(useAppStore.getState().draft).toBe("Keep source draft");
    act(() => emitNative({ ...event, id: "other-native" }));
    act(() => emitNative({ ...event, conversationId: "retired-owner" }));
    act(() => useAppStore.setState({ pendingConversationSwitchId: "next-owner" }));
    act(() => emitNative(event));
    act(() => useAppStore.setState({ conversationId: "next-owner", pendingConversationSwitchId: null, draft: "Keep next draft" }));
    act(() => emitNative(event));
    expect(runtimeMocks.openExternal).toHaveBeenCalledTimes(1);
    expect(runtimeMocks.navigate).not.toHaveBeenCalled(); expect(runtimeMocks.create).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("Keep next draft");
  });

  it("keeps unsupported inspection controls out of every browser menu and closes the final native tab through its host", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("last-page", "Final page", "https://example.com/", true)]);
    const onLastTabClosed = vi.fn();
    render(<BrowserPanel onLastTabClosed={onLastTabClosed} />);
    await screen.findByRole("tab", { name: "Final page" });
    expect(screen.queryByRole("button", { name: "选择元素" })).toBeNull();
    expect(screen.queryByRole("button", { name: "添加页面批注" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "更多浏览器操作" }));
    expect(screen.queryByRole("menuitem", { name: "打开页面诊断" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu", { name: "浏览器选项" }), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "关闭 Final page" }));
    await waitFor(() => expect(onLastTabClosed).toHaveBeenCalledTimes(1));
    expect(runtimeMocks.close).toHaveBeenCalledWith("conv-browser", "last-page");
  });
  it("preserves an unsubmitted address through same-URL title/favicon/loading events", async () => {
    const listeners = new Set<(event: EmbeddedBrowserState) => void>();
    runtimeMocks.onEvent.mockImplementation(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; });
    runtimeMocks.list.mockResolvedValueOnce([page("address-draft", "Initial title", "https://example.com/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Initial title" });
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://unsent.example/new" } });
    act(() => { for (const listener of listeners) listener({ ...page("address-draft", "New title", "https://example.com/", true), loading: true, favicon: "https://example.com/favicon.ico" }); });
    expect(address.value).toBe("https://unsent.example/new");
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledWith("conv-browser", "address-draft", "https://unsent.example/new"));
  });
  beforeEach(() => {
    vi.clearAllMocks();
    feedbackMocks.send.mockReset().mockResolvedValue(true);
    nativeListeners.clear();
    runtimeMocks.onEvent.mockImplementation(callback => { nativeListeners.add(callback); return () => { nativeListeners.delete(callback); }; });
    runtimeMocks.onSettingsChanged.mockImplementation((_callback) => () => {});
    __resetOpenWebInBrowserForTests();
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    useAppStore.setState({
      conversationId: "conv-browser",
      pendingConversationSwitchId: null,
      rightStackTab: "tasks",
      rightPanelOpen: false,
      rightStackTabLocked: false,
      permissionMode: "bypass",
      workingDirectory: "C:/browser",
      conversationWorkbenchStates: {},
      conversations: [],
      sideChats: {},
      conversationMessages: {},
      previewLaunchProcesses: [],
      previewServers: [],
      livePreviewUrl: null,
      previewVerification: null,
      browserAnnotations: [],
      selectedMentions: [],
      messages: [],
      turnDiffs: {},
      diffReview: null,
    });
  });

  it("presents an authorized tool's exact native target and reports the real pane bounds", async () => {
    const listeners = new Set<(event: EmbeddedBrowserState) => void>();
    runtimeMocks.onEvent.mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; });
    const bounds = { x: 650, y: 80, left: 650, top: 80, right: 1330, bottom: 840, width: 680, height: 760, toJSON: () => ({}) };
    const originalBounds = HTMLElement.prototype.getBoundingClientRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      return this.classList.contains("mc-browser-surface") ? bounds : originalBounds.call(this);
    });
    const target = { ...page("tool-native", "Tool page", "https://tool.example/"), type: "presentation-requested" as const };
    runtimeMocks.list.mockResolvedValueOnce([page("existing", "Existing page", "https://existing.example/", true), target]);
    const Harness = () => {
      useDesktopEvents();
      const visible = useAppStore(state => state.rightPanelOpen && state.rightStackTab === "browser");
      return visible ? <BrowserPanel /> : null;
    };
    render(<Harness />);
    act(() => { for (const listener of [...listeners]) listener(target); });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Tool page" }).getAttribute("aria-selected")).toBe("true"));
    await waitFor(() => expect(runtimeMocks.activate).toHaveBeenCalledWith("conv-browser", "tool-native"));
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenCalledWith(expect.objectContaining({
      id: "tool-native", conversationId: "conv-browser", x: 650, y: 80, width: 680, height: 760,
    })));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.create).not.toHaveBeenCalled();
    expect(useAppStore.getState().rightStackTabLocked).toBe(false);
  });

  it.each(["closed", "switched"] as const)("does not reopen or reactivate a tool page after the user %s during native listing", async (action) => {
    const listeners = new Set<(event: EmbeddedBrowserState) => void>();
    runtimeMocks.onEvent.mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; });
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    const target = { ...page("tool-native", "Tool page", "https://tool.example/"), type: "presentation-requested" as const };
    const Harness = () => {
      useDesktopEvents();
      const visible = useAppStore(state => state.rightPanelOpen && state.rightStackTab === "browser");
      return visible ? <BrowserPanel /> : null;
    };
    render(<Harness />);
    act(() => { for (const listener of [...listeners]) listener(target); });
    await waitFor(() => expect(runtimeMocks.list).toHaveBeenCalledWith("conv-browser"));
    act(() => {
      if (action === "closed") useAppStore.getState().toggleRightPanel();
      else useAppStore.setState({ conversationId: "next-owner", rightPanelOpen: false });
    });
    runtimeMocks.activate.mockClear();
    await act(async () => listing.resolve([target]));
    act(() => { for (const listener of [...listeners]) listener(target); });
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    expect(runtimeMocks.activate).not.toHaveBeenCalled();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.create).not.toHaveBeenCalled();
    act(() => useAppStore.getState().setRightStackTab("browser"));
    await screen.findByText("工具");
    expect(screen.queryByRole("tab", { name: "Tool page" })).toBeNull();
  });

  it("does not let a background owner or pending navigation steal the current pane", () => {
    const listeners = new Set<(event: EmbeddedBrowserState) => void>();
    runtimeMocks.onEvent.mockImplementation((listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; });
    const Harness = () => { useDesktopEvents(); return null; };
    render(<Harness />);
    const target = { ...page("background", "Background", "https://background.example/"), type: "presentation-requested" as const };
    act(() => { for (const listener of listeners) listener({ ...target, conversationId: "another-owner" }); });
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    act(() => useAppStore.setState({ pendingConversationSwitchId: "next-owner" }));
    act(() => { for (const listener of listeners) listener(target); });
    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    expect(runtimeMocks.list).not.toHaveBeenCalled();
  });

  it("does not reactivate a native page when navigation resolves after the panel unmounts", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    const navigation = pending<ReturnType<typeof page>>();
    const view = render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    runtimeMocks.navigate.mockReturnValueOnce(navigation.promise);
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "https://late.example/" } });
    fireEvent.submit(address.closest("form")!);
    view.unmount(); runtimeMocks.activate.mockClear();
    await act(async () => navigation.resolve(page("a", "Late page", "https://late.example/")));
    expect(runtimeMocks.activate).not.toHaveBeenCalled();
  });

  it.each(["activate", "reconcile"])("reports an actual native %s failure while selecting a restored tab", async (operation) => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    if (operation === "activate") runtimeMocks.activate.mockRejectedValueOnce(new Error("Native tab no longer available"));
    else {
      runtimeMocks.activate.mockResolvedValueOnce(false);
      runtimeMocks.list.mockRejectedValueOnce(new Error("Native tab no longer available"));
    }
    render(<BrowserPanel />);
    expect((await screen.findByRole("alert")).textContent).toContain("Native tab no longer available");
  });

  it("uses browser tab keyboard navigation and keeps IME Enter from submitting the address form", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true), page("b", "Page B", "https://b.example/")]);
    render(<BrowserPanel />);
    const first = await screen.findByRole("tab", { name: "Page A" });
    first.focus(); fireEvent.keyDown(first, { key: "ArrowRight" });
    const second = screen.getByRole("tab", { name: "Page B" });
    expect(second.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(second);
    expect(first.tabIndex).toBe(-1);
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" });
    expect(fireEvent.keyDown(address, { key: "Enter", isComposing: true })).toBe(false);
    expect(fireEvent.keyDown(address, { key: "Enter", keyCode: 229 })).toBe(false);
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("does not offer another conversation's turn changes from the preview", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("preview-tab", "Local preview", "http://localhost:4173/", true)]);
    useAppStore.setState({
      messages: [{ id: "answer", role: "assistant", content: "Done", artifacts: [], timestamp: 1, turnId: "turn-other" }],
      turnDiffs: { "conv-browser": { threadId: "another-owner", turnId: "turn-other", diff: "diff --git a/a.ts b/a.ts\n@@ -1 +1 @@\n-old\n+new", updatedAt: 1 } },
    });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Local preview" });
    expect(screen.queryByRole("button", { name: /审阅本轮/ })).toBeNull();
  });

  it("returns to the saved page in its original tab after that tab navigated elsewhere", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("preview-a", "Preview A", "https://example.com/other", true),
      page("preview-b", "Preview B", "http://localhost:4173/"),
    ]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview A" });
    act(() => returnToBrowserPage({ conversationId: "conv-browser", targetId: "preview-a", url: "http://localhost:4173/" }));
    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledWith("conv-browser", "preview-a", "http://localhost:4173/"));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });

  it("reopens a closed preview and ignores a return request belonging to another conversation", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("other", "Other page", "https://example.com/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Other page" });
    act(() => returnToBrowserPage({ conversationId: "someone-else", targetId: "closed", url: "http://localhost:4173/" }));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    act(() => returnToBrowserPage({ conversationId: "conv-browser", targetId: "closed", url: "http://localhost:4173/" }));
    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledWith("conv-browser", expect.stringMatching(/^browser_/), "http://localhost:4173/"));
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("normalizes domains, local addresses, and search queries", () => {
    expect(normalizeBrowserInput("example.com/docs")).toBe("https://example.com/docs");
    expect(normalizeBrowserInput("localhost:5173")).toBe("http://localhost:5173");
    expect(normalizeBrowserInput("MiniCode browser")).toBe("https://www.bing.com/search?q=MiniCode%20browser");
  });

  it("yields the native view to overlapping menus and restores it after all overlays close", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("context-menu-surface")
        ? new DOMRect(740, 90, 240, 300)
        : new DOMRect(700, 150, 300, 600);
    });
    render(<BrowserPanel />);
    const address = await screen.findByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 600 })));

    const menu = render(<ContextMenu position={{ x: 740, y: 90 }} items={[{ label: "文件" }]} onClose={() => {}} />);
    await loadOccludedSnapshot();
    expect((document.querySelector(".mc-browser-occluded-snapshot") as HTMLImageElement).style.width).toBe("100%");
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 0, height: 0 })));
    const modal = render(<div role="dialog" aria-modal="true">Modal</div>);
    await act(async () => {});
    menu.unmount();
    await act(async () => {});
    expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 0, height: 0 }));
    modal.unmount();
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 600 })));
    expect(document.querySelector(".mc-browser-occluded-snapshot")).toBeNull();
    expect(runtimeMocks.capture).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ conversationId: "conv-browser", expectedUrl: "https://example.com/" }));
    expect(runtimeMocks.navigate).toHaveBeenCalledTimes(1);
  });

  it("centres the actual device frame, keeps grips only on responsive presets, and restores the full pane on exit", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      return this.classList.contains("mc-browser-surface") ? new DOMRect(700, 150, 600, 700) : new DOMRect();
    });
    runtimeMocks.getSettings.mockResolvedValueOnce({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: true, downloadPolicy: "block", origin: "https://example.com", permissions: [] });
    runtimeMocks.list.mockResolvedValueOnce([page("device-page", "Device page", "https://example.com/", true)]);
    render(<BrowserPanel />);
    const address = await screen.findByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://unsubmitted.example/" } });
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({
      id: "device-page", conversationId: "conv-browser", x: 843, y: 150, width: 314, height: 680,
      viewport: expect.objectContaining({ width: 390, height: 844, scale: 680 / 844 }),
    })));
    expect(document.querySelectorAll(".mc-browser-device-handle")).toHaveLength(5);
    const grips = document.querySelector(".mc-browser-device-handles") as HTMLElement;
    expect(grips.style.left).toBe("143px");
    expect(grips.style.width).toBe("314px");
    expect(grips.style.height).toBe("680px");
    fireEvent.change(screen.getByRole("combobox", { name: "设备视口" }), { target: { value: "iphone-15-pro" } });
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({
      id: "device-page", width: 314, height: 680, viewport: expect.objectContaining({ width: 393, height: 852, scale: 680 / 852 }),
    })));
    expect(document.querySelectorAll(".mc-browser-device-handle")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "退出设备工具栏模式" }));
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({
      id: "device-page", x: 700, y: 150, width: 600, height: 700, viewport: null,
    })));
    expect(address.value).toBe("https://unsubmitted.example/");
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.create).not.toHaveBeenCalled();
  });

  it("yields the same native device page during grip dragging and restores its frame without changing the draft", async () => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      return this.classList.contains("mc-browser-surface") ? new DOMRect(700, 150, 600, 700) : new DOMRect();
    });
    runtimeMocks.getSettings.mockResolvedValueOnce({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: true, downloadPolicy: "block", origin: "https://example.com", permissions: [] });
    runtimeMocks.list.mockResolvedValueOnce([page("device-drag", "Drag page", "https://example.com/", true)]);
    render(<BrowserPanel />);
    const right = await screen.findByRole("button", { name: "从右边缘调整设备视口" });
    right.setPointerCapture = vi.fn();
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "unsent address" } });
    fireEvent.pointerDown(right, { clientX: 1157, clientY: 300 });
    await loadOccludedSnapshot();
    const image = document.querySelector(".mc-browser-occluded-snapshot") as HTMLImageElement;
    expect(image.style.left).toBe("143px");
    expect(image.style.width).toBe("314px");
    expect(image.style.height).toBe("680px");
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({
      id: "device-drag", conversationId: "conv-browser", width: 0, height: 0,
      viewport: expect.objectContaining({ width: 390, height: 844, scale: 680 / 844 }),
    })));
    fireEvent.pointerUp(right);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({
      id: "device-drag", conversationId: "conv-browser", x: 843, y: 150, width: 314, height: 680,
    })));
    expect(document.querySelector(".mc-browser-occluded-snapshot")).toBeNull();
    expect(runtimeMocks.capture).toHaveBeenCalledExactlyOnceWith({ id: "device-drag", conversationId: "conv-browser", expectedUrl: "https://example.com/" });
    expect(address.value).toBe("unsent address");
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.create).not.toHaveBeenCalled();
  });

  it("does not hide or replace the live native owner when an overlay closes before capture completes", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(700, 150, 300, 600));
    runtimeMocks.list.mockResolvedValueOnce([page("capture-owner", "Capture page", "https://example.com/", true)]);
    const frame = pending<Awaited<ReturnType<typeof runtimeMocks.capture>>>();
    runtimeMocks.capture.mockReturnValueOnce(frame.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Capture page" });
    const overlay = render(<div role="dialog" aria-modal="true">Overlay</div>);
    await waitFor(() => expect(runtimeMocks.capture).toHaveBeenCalledOnce());
    expect(runtimeMocks.setBounds).not.toHaveBeenLastCalledWith(expect.objectContaining({ width: 0, height: 0 }));
    overlay.unmount();
    await act(async () => frame.resolve({ mimeType: "image/png", data: "captured-page", width: 300, height: 600, url: "https://example.com/" }));
    expect(document.querySelector(".mc-browser-occluded-snapshot")).toBeNull();
    expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ id: "capture-owner", width: 300, height: 600 }));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("reserves the floating composer strip and updates it as the draft grows without navigating again", async () => {
    let composerTop = 630;
    let resize!: ResizeObserverCallback;
    vi.stubGlobal("ResizeObserver", class extends ResizeObserverMock {
      constructor(callback: ResizeObserverCallback) { super(); resize = callback; }
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("chat-pane-composer-region")
        ? new DOMRect(900, composerTop, 90, 100)
        : new DOMRect(700, 150, 300, 600);
    });
    useAppStore.setState({ rightPanelOpen: true, rightPanelExpanded: true });
    const { container } = render(<>
      <div className="workbench-primary" data-floating="true"><div className="chat-pane-composer-region" /></div>
      <BrowserPanel />
    </>);
    const address = await screen.findByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 472 })));
    composerTop = 400;
    act(() => resize([], {} as ResizeObserver));
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 242 })));
    container.querySelector(".workbench-primary")!.setAttribute("data-floating", "false");
    act(() => useAppStore.getState().setRightPanelExpanded(false));
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 600 })));
    expect(runtimeMocks.navigate).toHaveBeenCalledTimes(1);
  });

  it("keeps the native page inside its own modal drawer visible while yielding to a separate dialog", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(700, 150, 300, 600));
    runtimeMocks.list.mockResolvedValueOnce([page("drawer-page", "Drawer preview", "http://localhost:4173/", true)]);
    render(<div role="dialog" aria-modal="true" aria-label="右侧面板"><BrowserPanel /></div>);
    await screen.findByRole("tab", { name: "Drawer preview" });
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ id: "drawer-page", width: 300, height: 600 })));
    const externalDialog = render(<div role="dialog" aria-modal="true" aria-label="独立弹窗">需要处理的操作</div>);
    await loadOccludedSnapshot();
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ id: "drawer-page", width: 0, height: 0 })));
    externalDialog.unmount();
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ id: "drawer-page", width: 300, height: 600 })));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("leaves the native view visible for non-overlapping popups and avoids IPC for streaming text", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.getAttribute("role") === "menu"
        ? new DOMRect(10, 90, 240, 300)
        : new DOMRect(700, 150, 300, 600);
    });
    render(<BrowserPanel />);
    const address = await screen.findByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.submit(address.closest("form")!);
    const menu = render(<div role="menu">Menu</div>);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 600 })));
    await act(async () => { await new Promise(requestAnimationFrame); });
    runtimeMocks.setBounds.mockClear();
    const text = render(<div>streaming</div>);
    text.rerender(<div>streaming more text</div>);
    await act(async () => {});
    expect(runtimeMocks.setBounds).not.toHaveBeenCalled();
    menu.unmount();
  });

  it("removes externally closed native tabs without reintroducing them as page updates", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("closed-page", "Closing page", "https://one.example/", true), page("kept-page", "Kept page", "https://two.example/")]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Closing page" });
    const dispatch = emitNative;
    act(() => dispatch({ ...page("closed-page", "Closing page", "https://one.example/"), type: "closed" }));
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Closing page" })).toBeNull());
    expect(screen.getByRole("tab", { name: "Kept page" })).toBeTruthy();
    act(() => dispatch({ ...page("kept-page", "Kept page", "https://two.example/"), conversationId: "other-owner", type: "closed" }));
    expect(screen.getByRole("tab", { name: "Kept page" })).toBeTruthy();
  });

  it("renders a native-browser shell and opens typed addresses in the embedded view", async () => {
    render(<BrowserPanel />);

    expect(await screen.findByText("工具")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /新标签页/ })).toBeTruthy();
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.submit(address.closest("form")!);

    await waitFor(() => {
      expect(runtimeMocks.navigate).toHaveBeenCalledWith(
        "conv-browser",
        expect.stringMatching(/^browser_/),
        "https://example.com/",
      );
    });
  });

  it("reuses the initial blank tab when opening a requested web source", async () => {
    render(<BrowserPanel />);

    expect(openWebInBrowser("https://docs.example/guide")).toBe(true);

    await waitFor(() => {
      expect(runtimeMocks.navigate).toHaveBeenCalledWith(
        "conv-browser",
        expect.stringMatching(/^browser_/),
        "https://docs.example/guide",
      );
    });
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(runtimeMocks.create).not.toHaveBeenCalled();
  });

  it("presents the existing preview target across readiness events without a timing window", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("original-preview", "Preview", "https://preview.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview" });
    const clock = vi.spyOn(Date, "now").mockReturnValue(1000);
    try {
      act(() => { openWebInBrowser("https://preview.example/"); });
      clock.mockReturnValue(5000);
      act(() => { openWebInBrowser("https://preview.example/"); });
      expect(screen.getAllByRole("tab")).toHaveLength(1);
      expect(runtimeMocks.create).not.toHaveBeenCalled();
      expect(runtimeMocks.navigate).not.toHaveBeenCalled();
      expect(runtimeMocks.runAction).not.toHaveBeenCalled();
    } finally { clock.mockRestore(); }
  });

  it("delivers a ready preview received before mount to the owning native browser", async () => {
    useAppStore.setState({
      workingDirectory: "C:/browser", conversationWorkbenchStates: {},
      previewLaunchProcesses: [], previewServers: [], livePreviewUrl: null, previewVerification: null,
    });
    handlePreviewEvent({
      type: "preview.launch.started", conversation_id: "conv-browser", workspace_root: "C:/browser",
      id: "web", name: "web", command: "npm run dev", cwd: "C:/browser", port: 4173, url: "", status: "starting",
    });
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    handlePreviewEvent({
      type: "preview.server.ready", conversation_id: "conv-browser", workspace_root: "C:/browser",
      id: "web", port: 4173, url: "http://localhost:4173",
    });

    render(<BrowserPanel />);

    await waitFor(() => {
      expect(runtimeMocks.navigate).toHaveBeenCalledExactlyOnceWith(
        "conv-browser", expect.stringMatching(/^browser_/), "http://localhost:4173/",
      );
    });
    expect(useAppStore.getState().rightStackTab).toBe("browser");
  });

  it("restores browser tabs created by an agent before the panel opened", async () => {
    runtimeMocks.list.mockResolvedValueOnce([{
      id: "agent_browser",
      conversationId: "conv-browser",
      type: "page",
      url: "https://docs.example/guide",
      title: "Guide",
      faviconUrl: "https://docs.example/icon.png",
      loading: false,
      canGoBack: false,
      canGoForward: false,
      active: true,
    }]);

    render(<BrowserPanel />);

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Guide/ })).toBeTruthy();
    });
    const icon = document.querySelector<HTMLImageElement>('.brand-icon-remote[src="https://docs.example/icon.png"]')!;
    expect(icon.hidden).toBe(true);
    expect(icon.parentElement?.querySelector("svg")).toBeTruthy();
    fireEvent.load(icon);
    expect(icon.hidden).toBe(false);
    expect(document.querySelector('[data-brand="website"] img')).toBe(icon);
    expect(runtimeMocks.create).not.toHaveBeenCalled();
  });

  it.each(["updated", "error", "blank"])("keeps a live %s event instead of overwriting it with the initial list", async (eventKind) => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    const url = eventKind === "blank" ? "about:blank" : "https://live.example/document";
    const liveEvent: EmbeddedBrowserState = {
      ...page("native", "Live page", url),
      type: eventKind === "error" ? "error" : "updated",
      error: eventKind === "error" ? "Live navigation failed" : undefined,
      faviconUrl: "",
    };
    act(() => emitNative(liveEvent));
    await act(async () => listing.resolve([
      { ...page("native", "Old page", "https://old.example/", true), faviconUrl: "https://old.example/icon.png" },
    ]));

    expect((screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe(eventKind === "blank" ? "" : url);
    expect(screen.getByRole("tab", { name: "Live page" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Old page" })).toBeNull();
    expect(document.querySelector('img[src="https://old.example/icon.png"]')).toBeNull();
    if (eventKind === "error") expect(screen.getByRole("alert").textContent).toContain("Live navigation failed");
    if (eventKind === "blank") expect(screen.getByText("工具")).toBeTruthy();
  });

  it("preserves a native page received before the restoration request fails", async () => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    act(() => emitNative(page("native", "Live page", "https://live.example/")));

    await act(async () => listing.reject(new Error("Native listing failed")));

    expect(screen.getByRole("tab", { name: "Live page" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("https://live.example/");
    expect(screen.getByRole("alert").textContent).toContain("Native listing failed");
  });

  it("allocates distinct tabs for batched requests instead of overwriting the first blank tab", async () => {
    render(<BrowserPanel />);
    await screen.findByText("工具");

    act(() => {
      openWebInBrowser("https://first.example/");
      openWebInBrowser("https://second.example/");
    });

    await screen.findByRole("tab", { name: "https://first.example/" });
    expect(screen.getByRole("tab", { name: "https://second.example/" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(new Set(runtimeMocks.navigate.mock.calls.map((call) => call[1])).size).toBe(2);
    expect(runtimeMocks.navigate.mock.calls.map((call) => call[2])).toEqual(["https://first.example/", "https://second.example/"]);
  });

  it.each(["native-first", "open-first"])("keeps both pages when %s occurs in the same React batch", async (order) => {
    render(<BrowserPanel />);
    await screen.findByText("工具");
    const emit = () => emitNative(page("native", "Native page", "https://native.example/"));

    act(() => {
      if (order === "native-first") emit();
      openWebInBrowser("https://requested.example/");
      if (order === "open-first") emit();
    });

    await screen.findByRole("tab", { name: "https://requested.example/" });
    expect(screen.getByRole("tab", { name: "Native page" })).toBeTruthy();
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });

  it("clears an explicitly empty native favicon when the page changes", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      { ...page("native", "Old page", "https://old.example/", true), faviconUrl: "https://old.example/icon.png" },
    ]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Old page" });
    expect(document.querySelector('img[src="https://old.example/icon.png"]')).toBeTruthy();

    act(() => emitNative({
      ...page("native", "New page", "https://new.example/"), faviconUrl: "",
    }));

    expect(screen.getByRole("tab", { name: "New page" })).toBeTruthy();
    expect(document.querySelector('img[src="https://old.example/icon.png"]')).toBeNull();
  });

  it("keeps a native blank page hidden and reuses it for the next web request", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("native", "Old page", "https://old.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Old page" });
    act(() => emitNative(page("native", "Blank page", "about:blank")));
    expect(screen.getByText("工具")).toBeTruthy();

    act(() => openWebInBrowser("https://new.example/"));

    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledExactlyOnceWith("conv-browser", "native", "https://new.example/"));
    expect(screen.getAllByRole("tab")).toHaveLength(1);
  });

  it("selects the next live tab when closing the middle tab", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("first", "First page", "https://first.example/"),
      page("middle", "Middle page", "https://middle.example/", true),
      page("last", "Last page", "https://last.example/"),
    ]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Middle page" });

    fireEvent.click(screen.getByRole("button", { name: "关闭 Middle page" }));

    await waitFor(() => expect(screen.queryByRole("tab", { name: "Middle page" })).toBeNull());
    expect(screen.getByRole("tab", { name: "Last page" }).getAttribute("aria-selected")).toBe("true");
  });

  it.each(["page", "empty", "blank", "failed"])("opens a pending link after delayed %s restoration", async (restoration) => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    openWebInBrowser("https://requested.example/guide");
    render(<BrowserPanel />);

    expect(screen.getByRole("status").textContent).toContain("正在恢复浏览器标签页");
    expect(screen.queryByRole("textbox", { name: "搜索或输入网址" })).toBeNull();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();

    await act(async () => {
      if (restoration === "failed") listing.reject(new Error("Native listing failed"));
      else listing.resolve(restoration === "empty" ? [] : [
        page("existing", "Existing page", restoration === "blank" ? "about:blank" : "https://existing.example/", true),
      ]);
    });

    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledExactlyOnceWith(
      "conv-browser", expect.any(String), "https://requested.example/guide",
    ));
    expect((screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("https://requested.example/guide");
    expect(screen.getByRole("tab", { name: "https://requested.example/guide" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getAllByRole("tab")).toHaveLength(restoration === "page" ? 2 : 1);
    if (restoration === "page") expect(screen.getByRole("tab", { name: "Existing page" })).toBeTruthy();
    if (restoration === "blank") expect(runtimeMocks.navigate).toHaveBeenCalledWith("conv-browser", "existing", expect.any(String));
  });

  it("retains a native address-focus request until the existing page finishes hydration without adding a tab", async () => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    act(() => focusBrowserAddressBar());
    expect(screen.queryByRole("textbox", { name: "搜索或输入网址" })).toBeNull();
    await act(async () => listing.resolve([page("current", "Existing page", "https://existing.example/", true)]));
    const address = await screen.findByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    await waitFor(() => expect(document.activeElement).toBe(address));
    expect(address.selectionStart).toBe(0);
    expect(address.selectionEnd).toBe(address.value.length);
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.create).not.toHaveBeenCalled();
  });

  it.each([false, true])("waits for the new owner's restoration after switching from hydrated=%s", async (alreadyHydrated) => {
    const firstList = pending<ReturnType<typeof page>[]>();
    const secondList = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(firstList.promise).mockReturnValueOnce(secondList.promise);
    render(<BrowserPanel />);
    if (alreadyHydrated) {
      await act(async () => firstList.resolve([page("first-tab", "First owner", "https://first.example/", true)]));
      await screen.findByRole("tab", { name: "First owner" });
    }

    act(() => {
      useAppStore.setState({ conversationId: "conv-other" });
      openWebInBrowser("https://second.example/guide");
    });
    expect(screen.getByRole("status")).toBeTruthy();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    await act(async () => secondList.resolve([
      { ...page("second-tab", "Second owner", "https://second.example/", true), conversationId: "conv-other" },
    ]));
    await waitFor(() => expect(runtimeMocks.navigate).toHaveBeenCalledExactlyOnceWith(
      "conv-other", expect.any(String), "https://second.example/guide",
    ));
    if (!alreadyHydrated) {
      await act(async () => firstList.resolve([page("first-tab", "First owner", "https://first.example/", true)]));
    }

    expect(screen.queryByRole("tab", { name: "First owner" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Second owner" })).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("https://second.example/guide");
    expect(runtimeMocks.navigate).toHaveBeenCalledTimes(1);
  });

  it("reloads a restarted preview whose desktop workspace uses Windows separators and case", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("preview", "Preview", "http://localhost:4173/", true)]);
    useAppStore.setState({ workingDirectory: "C:\\Browser" });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview" });

    act(() => { refreshWebInBrowser("http://localhost:4173/", "conv-browser", "c:\\browser"); });

    await waitFor(() => expect(runtimeMocks.runAction).toHaveBeenCalledExactlyOnceWith("conv-browser", "preview", "reload"));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(screen.getAllByRole("tab")).toHaveLength(1);
  });

  it("reloads all tabs on the refreshed preview origin without navigating or stealing focus", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("preview-a", "Preview A", "http://localhost:4173/first"),
      page("preview-b", "Preview B", "http://localhost:4173/second"),
      page("other-preview", "Other preview", "http://localhost:5173/"),
      page("website", "Website", "https://example.com/", true),
    ]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Website" });
    runtimeMocks.activate.mockClear();

    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/source", path: "src/app.ts",
    }));

    await waitFor(() => expect(runtimeMocks.runAction.mock.calls).toEqual([
      ["conv-browser", "preview-a", "reload"],
      ["conv-browser", "preview-b", "reload"],
    ]));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(runtimeMocks.activate).not.toHaveBeenCalled();
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getByRole("tab", { name: "Website" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("https://example.com/");
  });

  it.each([
    { replayed: true },
    { conversation_id: "conv-background" },
    { workspace_root: "C:/other" },
    { url: "http://localhost:5173/" },
    { url: undefined },
  ])("does not reload unrelated or historical refresh evidence: %j", async (override) => {
    useAppStore.setState({ conversationMessages: { "conv-background": [] } });
    runtimeMocks.list.mockResolvedValueOnce([page("preview", "Preview", "http://localhost:4173/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview" });

    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/", ...override,
    } as ServerEvent));

    expect(runtimeMocks.runAction).not.toHaveBeenCalled();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("uses the owning live preview when an explicit refresh has no URL", async () => {
    useAppStore.setState({ livePreviewUrl: "http://localhost:4173/app" });
    runtimeMocks.list.mockResolvedValueOnce([
      page("preview", "Preview", "http://localhost:4173/route"),
      page("website", "Website", "https://example.com/", true),
    ]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Website" });

    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
    }));

    await waitFor(() => expect(runtimeMocks.runAction).toHaveBeenCalledExactlyOnceWith("conv-browser", "preview", "reload"));
    expect(screen.getByRole("tab", { name: "Website" }).getAttribute("aria-selected")).toBe("true");
  });

  it("retains and coalesces refreshes until browser hydration completes", async () => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    act(() => {
      for (const url of ["http://localhost:4173/first", "http://localhost:4173/latest", "http://localhost:5173/docs"]) {
        handlePreviewEvent({
          type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser", url,
        });
      }
    });
    expect(runtimeMocks.runAction).not.toHaveBeenCalled();

    await act(async () => listing.resolve([
      page("web", "Web preview", "http://localhost:4173/app"),
      page("docs", "Docs preview", "http://localhost:5173/guide"),
      page("website", "Website", "https://example.com/", true),
    ]));

    await waitFor(() => expect(runtimeMocks.runAction.mock.calls).toEqual([
      ["conv-browser", "web", "reload"],
      ["conv-browser", "docs", "reload"],
    ]));
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "Website" }).getAttribute("aria-selected")).toBe("true");
  });

  it("retains refreshes while unmounted and consumes them just once after remount", async () => {
    const pages = [
      page("preview", "Preview", "http://localhost:4173/app"),
      page("website", "Website", "https://example.com/", true),
    ];
    runtimeMocks.list.mockResolvedValueOnce(pages).mockResolvedValueOnce(pages).mockResolvedValueOnce(pages);
    const initial = render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Website" });
    initial.unmount();
    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/",
    }));
    expect(runtimeMocks.runAction).not.toHaveBeenCalled();

    const restored = render(<BrowserPanel />);
    await waitFor(() => expect(runtimeMocks.runAction).toHaveBeenCalledExactlyOnceWith("conv-browser", "preview", "reload"));
    expect(screen.getByRole("tab", { name: "Website" }).getAttribute("aria-selected")).toBe("true");
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    restored.unmount();
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Website" });
    expect(runtimeMocks.runAction).toHaveBeenCalledTimes(1);
  });

  it("discards a pending refresh when its workspace changes before restoration", async () => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    act(() => {
      handlePreviewEvent({
        type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
        url: "http://localhost:4173/",
      });
      useAppStore.setState({ workingDirectory: "C:/other" });
    });

    await act(async () => listing.resolve([page("preview", "Preview", "http://localhost:4173/", true)]));

    await screen.findByRole("tab", { name: "Preview" });
    expect(runtimeMocks.runAction).not.toHaveBeenCalled();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("keeps a pending refresh with its original conversation until that browser is restored", async () => {
    handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/",
    });
    useAppStore.setState({ conversationId: "conv-other" });
    runtimeMocks.list.mockResolvedValueOnce([
      { ...page("other", "Other owner", "http://localhost:4173/", true), conversationId: "conv-other" },
    ]).mockResolvedValueOnce([page("original", "Original owner", "http://localhost:4173/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Other owner" });
    expect(runtimeMocks.runAction).not.toHaveBeenCalled();

    act(() => useAppStore.setState({ conversationId: "conv-browser" }));

    await screen.findByRole("tab", { name: "Original owner" });
    expect(runtimeMocks.runAction).toHaveBeenCalledExactlyOnceWith("conv-browser", "original", "reload");
  });

  it.each([false, true])("shows an automatic refresh failure with IPC rejection=%s", async (reject) => {
    const preview = page("preview", "Preview", "http://localhost:4173/", true);
    runtimeMocks.list.mockResolvedValueOnce([preview]).mockResolvedValueOnce([preview]);
    if (reject) runtimeMocks.runAction.mockRejectedValueOnce(new Error("Native reload failed"));
    else runtimeMocks.runAction.mockResolvedValueOnce(false);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview" });

    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/",
    }));

    expect((await screen.findByRole("alert")).textContent).toContain(reject ? "Native reload failed" : "浏览器未接受刷新操作");
  });

  it("does not let an old automatic refresh failure overwrite a newer navigation", async () => {
    const reload = pending<boolean>();
    runtimeMocks.list.mockResolvedValueOnce([page("preview", "Preview", "http://localhost:4173/", true)]);
    runtimeMocks.runAction.mockReturnValueOnce(reload.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview" });
    act(() => handlePreviewEvent({
      type: "preview.refreshed", conversation_id: "conv-browser", workspace_root: "C:/browser",
      url: "http://localhost:4173/",
    }));
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://new.example/" } });
    fireEvent.submit(address.closest("form")!);
    await screen.findByRole("tab", { name: "https://new.example/" });

    await act(async () => reload.reject(new Error("Old automatic refresh failed")));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(address.value).toBe("https://new.example/");
    expect(runtimeMocks.list).toHaveBeenCalledTimes(1);
  });

  it("keeps the selected tab visible when a background navigation completes", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("a", "Page A", "https://a.example/", true),
      page("b", "Page B", "https://b.example/"),
    ]);
    const navigation = pending<ReturnType<typeof page>>();
    runtimeMocks.navigate.mockReturnValueOnce(navigation.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://slow.example/" } });
    fireEvent.submit(address.closest("form")!);
    fireEvent.click(screen.getByRole("tab", { name: "Page B" }));
    await waitFor(() => expect(runtimeMocks.activate).toHaveBeenLastCalledWith("conv-browser", "b"));
    runtimeMocks.activate.mockClear();
    runtimeMocks.setBounds.mockClear();

    await act(async () => navigation.resolve(page("a", "Slow A", "https://slow.example/")));

    expect(address.value).toBe("https://b.example/");
    expect(screen.getByRole("tab", { name: "Page B" }).getAttribute("aria-selected")).toBe("true");
    expect(runtimeMocks.activate).toHaveBeenCalledWith("conv-browser", "b");
    expect(runtimeMocks.setBounds.mock.calls.at(-1)?.[0]).toMatchObject({ id: "b" });
  });

  it("ignores an earlier navigation failure after a newer navigation succeeded", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    const earlier = pending<ReturnType<typeof page>>();
    runtimeMocks.navigate.mockReturnValueOnce(earlier.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://first.example/" } });
    fireEvent.submit(address.closest("form")!);
    fireEvent.change(address, { target: { value: "https://latest.example/" } });
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(address.value).toBe("https://latest.example/"));

    await act(async () => earlier.reject(new Error("net::ERR_ABORTED")));

    expect(address.value).toBe("https://latest.example/");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["navigate", "reload", "reload-rejected"])("keeps a newer page when an old %s reconciliation finishes", async (operation) => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    const reconciliation = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(reconciliation.promise);
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    if (operation === "navigate") {
      runtimeMocks.navigate.mockRejectedValueOnce(new Error("Old navigation failed"));
      fireEvent.change(address, { target: { value: "https://old.example/" } });
      fireEvent.submit(address.closest("form")!);
    } else {
      if (operation === "reload-rejected") runtimeMocks.runAction.mockRejectedValueOnce(new Error("Old reload failed"));
      else runtimeMocks.runAction.mockResolvedValueOnce(false);
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    }
    await waitFor(() => expect(runtimeMocks.list).toHaveBeenCalledTimes(2));
    fireEvent.change(address, { target: { value: "https://latest.example/" } });
    fireEvent.submit(address.closest("form")!);
    await screen.findByRole("tab", { name: "https://latest.example/" });

    await act(async () => reconciliation.resolve([page("a", "Stale page", "https://old.example/", true)]));

    expect(address.value).toBe("https://latest.example/");
    expect(screen.getByRole("tab", { name: "https://latest.example/" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores a pending reload rejection after another navigation succeeds", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    const reload = pending<boolean>();
    runtimeMocks.runAction.mockReturnValueOnce(reload.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const address = screen.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://latest.example/" } });
    fireEvent.submit(address.closest("form")!);
    await screen.findByRole("tab", { name: "https://latest.example/" });

    await act(async () => reload.reject(new Error("Old reload failed")));

    expect(runtimeMocks.list).toHaveBeenCalledTimes(1);
    expect(address.value).toBe("https://latest.example/");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not restore a closed tab from an earlier activation reconciliation", async () => {
    const pages = [page("a", "Page A", "https://a.example/", true), page("b", "Page B", "https://b.example/")];
    const reconciliation = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockResolvedValueOnce(pages).mockReturnValueOnce(reconciliation.promise);
    runtimeMocks.activate.mockResolvedValueOnce(false);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    await waitFor(() => expect(runtimeMocks.list).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "关闭 Page A" }));
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Page A" })).toBeNull());

    await act(async () => reconciliation.resolve(pages));

    expect(screen.queryByRole("tab", { name: "Page A" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Page B" }).getAttribute("aria-selected")).toBe("true");
  });

  it("does not reconcile a failed close against a new conversation", async () => {
    const closing = pending<boolean>();
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    runtimeMocks.close.mockReturnValueOnce(closing.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    fireEvent.click(screen.getByRole("button", { name: "关闭 Page A" }));
    await waitFor(() => expect(runtimeMocks.close).toHaveBeenCalledWith("conv-browser", "a"));
    runtimeMocks.list.mockResolvedValueOnce([{ ...page("a", "Page B", "https://b.example/", true), conversationId: "conv-other" }]);
    act(() => useAppStore.setState({ conversationId: "conv-other" }));
    await screen.findByRole("tab", { name: "Page B" });
    const listCalls = runtimeMocks.list.mock.calls.length;

    await act(async () => closing.reject(new Error("Old conversation close failed")));

    expect(runtimeMocks.list.mock.calls.length).toBe(listCalls);
    expect(screen.queryByText("Old conversation close failed")).toBeNull();
    expect(screen.getByRole("tab", { name: "Page B" })).toBeTruthy();
  });
});

/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handlePreviewEvent } from "../chat/previewEvents";
import type { ServerEvent } from "../protocol/events";
import type { EmbeddedBrowserState } from "../desktop/runtime";
import { BrowserPanel, normalizeBrowserInput } from "./BrowserPanel";
import { ContextMenu } from "../components/ContextMenu";
import { __resetOpenWebInBrowserForTests, openWebInBrowser, returnToBrowserPage } from "../chat/openWebInBrowser";

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
  getSettings: vi.fn(async () => ({ downloadPolicy: "block" as const, origin: "https://example.com", permissions: [] as string[] })),
  setSettings: vi.fn(async (payload: { downloadPolicy?: "block" | "ask" | "allow"; origin?: string; permission?: string; allowed?: boolean }) => ({
    downloadPolicy: payload.downloadPolicy ?? "block" as const,
    origin: payload.origin ?? "https://example.com",
    permissions: payload.permission && payload.allowed ? [payload.permission] : [],
  })),
  clearSiteData: vi.fn(async () => true),
  close: vi.fn(async () => true),
  onEvent: vi.fn((_callback: (event: EmbeddedBrowserState) => void) => () => {}),
  openExternal: vi.fn(async () => true),
}));

vi.mock("../desktop/runtime", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../desktop/runtime")>();
  return {
    ...actual,
    isDesktop: () => true,
    embeddedBrowserCreate: runtimeMocks.create,
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
    openExternal: runtimeMocks.openExternal,
  };
});

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
  beforeEach(() => {
    vi.clearAllMocks();
    __resetOpenWebInBrowserForTests();
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    useAppStore.setState({
      conversationId: "conv-browser",
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

  it("does not reactivate a native page when navigation resolves after the panel unmounts", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    const navigation = pending<ReturnType<typeof page>>();
    const view = render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    runtimeMocks.navigate.mockReturnValueOnce(navigation.promise);
    const address = screen.getByRole("textbox", { name: "地址栏" });
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

  it.each(["console", "element"])("shows the native semantic %s inspection failure instead of an empty result", async (kind) => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    runtimeMocks.inspect.mockResolvedValueOnce({ ok: false, error: "Native inspector refused this page", value: [] });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    if (kind === "console") openBrowserAction("打开页面诊断");
    else fireEvent.click(screen.getByRole("button", { name: "选择元素" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Native inspector refused this page");
  });

  it("serializes site writes and keeps a committed setting when a reopened pane's older read completes", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    openBrowserAction("打开站点设置");
    await screen.findByText("https://example.com");
    const write = pending<{ downloadPolicy: "allow"; origin: string; permissions: string[] }>();
    runtimeMocks.setSettings.mockReturnValueOnce(write.promise);
    fireEvent.click(screen.getByRole("button", { name: "下载策略，当前：阻止" }));
    fireEvent.click(screen.getByRole("option", { name: "保存到下载目录" }));
    expect(screen.getByRole("checkbox", { name: "位置" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "清除站点数据" }).hasAttribute("disabled")).toBe(true);
    openBrowserAction("打开站点设置");
    const read = pending<{ downloadPolicy: "block"; origin: string; permissions: string[] }>();
    runtimeMocks.getSettings.mockReturnValueOnce(read.promise);
    openBrowserAction("打开站点设置");
    await act(async () => write.resolve({ downloadPolicy: "allow", origin: "https://a.example", permissions: [] }));
    await act(async () => read.resolve({ downloadPolicy: "block", origin: "https://a.example", permissions: [] }));
    expect(screen.getByRole("button", { name: "下载策略，当前：保存到下载目录" }).hasAttribute("disabled")).toBe(false);
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
    const address = screen.getByRole("textbox", { name: "地址栏" });
    expect(fireEvent.keyDown(address, { key: "Enter", isComposing: true })).toBe(false);
    expect(fireEvent.keyDown(address, { key: "Enter", keyCode: 229 })).toBe(false);
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
  });

  it("starts element selection directly from the page toolbar and leaves the description for the user", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("design-page", "Design", "http://localhost:4173/", true)]);
    runtimeMocks.inspect.mockResolvedValueOnce({ ok: true, value: { selector: "#save", text: "保存", rect: { x: 10, y: 10, width: 120, height: 32 }, viewport: { width: 1000, height: 800 } } });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Design" });
    expect(screen.queryByRole("region", { name: "页面批注" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "选择元素" }));
    await screen.findByText("已选择 120 × 32 px");
    expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "design-page", "element");
    expect(screen.getByText("保存")).toBeTruthy();
    const description = screen.getByRole("textbox", { name: "批注内容" });
    expect((description as HTMLTextAreaElement).value).toBe("");
    const selector = screen.getByRole("textbox", { name: "元素选择器" });
    const details = selector.closest("details")!;
    expect(details.open).toBe(false);
    fireEvent.click(screen.getByText("高级元素信息"));
    expect(details.open).toBe(true);
    expect((selector as HTMLInputElement).value).toBe("#save");
    await waitFor(() => expect(document.activeElement).toBe(description));
    expect(useAppStore.getState().browserAnnotations).toEqual([]);
  });

  it("treats native Esc cancellation as a return to the unchanged description", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("design-page", "Design", "http://localhost:4173/", true)]);
    const selection = pending<{ ok: boolean; value: unknown }>();
    runtimeMocks.inspect.mockReturnValueOnce(selection.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Design" });
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    const description = screen.getByRole("textbox", { name: "批注内容" });
    fireEvent.change(description, { target: { value: "保留原说明" } });
    fireEvent.click(screen.getByRole("button", { name: "选择元素" }));
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "design-page", "element"));
    await act(async () => selection.resolve({ ok: true, value: null }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect((description as HTMLTextAreaElement).value).toBe("保留原说明");
    expect((screen.getByRole("button", { name: "选择元素" }) as HTMLButtonElement).disabled).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(description));
  });

  it("opens the current turn review from its real page while preserving the annotation draft", async () => {
    const diff = "diff --git a/src/page.tsx b/src/page.tsx\n--- a/src/page.tsx\n+++ b/src/page.tsx\n@@ -4 +4 @@\n-old\n+new";
    runtimeMocks.list.mockResolvedValueOnce([page("preview-tab", "Local preview", "http://localhost:4173/", true)]);
    useAppStore.setState({
      messages: [{ id: "answer", role: "assistant", content: "Done", artifacts: [], timestamp: 1, turnId: "turn-preview" }],
      turnDiffs: { "conv-browser": { threadId: "conv-browser", turnId: "turn-preview", messageId: "answer", diff, updatedAt: 1 } },
    });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Local preview" });
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "缩小这个按钮的间距" } });
    fireEvent.click(screen.getByRole("button", { name: "审阅本轮 1 个文件更改" }));
    expect(useAppStore.getState().diffReview).toMatchObject({
      conversationId: "conv-browser", turnId: "turn-preview", selectedPath: "src/page.tsx", mode: "view",
      previewReturnTarget: { conversationId: "conv-browser", tab: "browser", targetId: "preview-tab", url: "http://localhost:4173/" },
    });
    expect((screen.getByRole("textbox", { name: "批注内容" }) as HTMLTextAreaElement).value).toBe("缩小这个按钮的间距");
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

  it("restores the exact preview tab with its selected element and unfinished annotation", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("preview-a", "Preview A", "http://localhost:4173/", true),
      page("preview-b", "Preview B", "http://localhost:4173/"),
    ]);
    runtimeMocks.inspect.mockResolvedValueOnce({ ok: true, value: {
      selector: "#save", text: "Save", rect: { x: 10, y: 20, width: 120, height: 36 }, viewport: { width: 1000, height: 800 },
    } });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Preview A" });
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.click(screen.getByRole("button", { name: "选择元素" }));
    await screen.findByText("已选择 120 × 36 px");
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "缩小按钮" } });
    fireEvent.click(screen.getByRole("tab", { name: "Preview B" }));
    expect(screen.queryByRole("region", { name: "页面批注" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "第二个标签的批注" } });

    act(() => returnToBrowserPage({ conversationId: "conv-browser", targetId: "preview-a", url: "http://localhost:4173/" }));
    expect(screen.getByRole("tab", { name: "Preview A" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByRole("textbox", { name: "批注内容" }) as HTMLTextAreaElement).value).toBe("缩小按钮");
    fireEvent.click(screen.getByText("高级元素信息"));
    expect((screen.getByRole("textbox", { name: "元素选择器" }) as HTMLInputElement).value).toBe("#save");
    expect(screen.getByText("已选择 120 × 36 px")).toBeTruthy();
    expect(runtimeMocks.navigate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "Preview B" }));
    expect((screen.getByRole("textbox", { name: "批注内容" }) as HTMLTextAreaElement).value).toBe("第二个标签的批注");
    expect(useAppStore.getState().browserAnnotations).toEqual([]);
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
    const address = await screen.findByRole("textbox", { name: "地址栏" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 300, height: 600 })));

    const menu = render(<ContextMenu position={{ x: 740, y: 90 }} items={[{ label: "文件" }]} onClose={() => {}} />);
    await waitFor(() => expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 0, height: 0 })));
    const modal = render(<div role="dialog" aria-modal="true">Modal</div>);
    await act(async () => {});
    menu.unmount();
    await act(async () => {});
    expect(runtimeMocks.setBounds).toHaveBeenLastCalledWith(expect.objectContaining({ width: 0, height: 0 }));
    modal.unmount();
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
    const address = await screen.findByRole("textbox", { name: "地址栏" });
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

  it("renders a native-browser shell and opens typed addresses in the embedded view", async () => {
    render(<BrowserPanel />);

    expect(await screen.findByText("开始浏览")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /新标签页/ })).toBeTruthy();
    const address = screen.getByRole("textbox", { name: "地址栏" });
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
    expect(document.querySelector('[data-brand="website"] img')?.getAttribute("src")).toBe("https://docs.example/icon.png");
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
    act(() => runtimeMocks.onEvent.mock.calls.at(-1)![0](liveEvent));
    await act(async () => listing.resolve([
      { ...page("native", "Old page", "https://old.example/", true), faviconUrl: "https://old.example/icon.png" },
    ]));

    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe(eventKind === "blank" ? "" : url);
    expect(screen.getByRole("tab", { name: "Live page" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Old page" })).toBeNull();
    expect(document.querySelector('img[src="https://old.example/icon.png"]')).toBeNull();
    if (eventKind === "error") expect(screen.getByRole("alert").textContent).toContain("Live navigation failed");
    if (eventKind === "blank") expect(screen.getByText("开始浏览")).toBeTruthy();
  });

  it("preserves a native page received before the restoration request fails", async () => {
    const listing = pending<ReturnType<typeof page>[]>();
    runtimeMocks.list.mockReturnValueOnce(listing.promise);
    render(<BrowserPanel />);
    act(() => runtimeMocks.onEvent.mock.calls.at(-1)![0](page("native", "Live page", "https://live.example/")));

    await act(async () => listing.reject(new Error("Native listing failed")));

    expect(screen.getByRole("tab", { name: "Live page" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe("https://live.example/");
    expect(screen.getByRole("alert").textContent).toContain("Native listing failed");
  });

  it("allocates distinct tabs for batched requests instead of overwriting the first blank tab", async () => {
    render(<BrowserPanel />);
    await screen.findByText("开始浏览");

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
    await screen.findByText("开始浏览");
    const emit = () => runtimeMocks.onEvent.mock.calls.at(-1)![0](page("native", "Native page", "https://native.example/"));

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

    act(() => runtimeMocks.onEvent.mock.calls.at(-1)![0]({
      ...page("native", "New page", "https://new.example/"), faviconUrl: "",
    }));

    expect(screen.getByRole("tab", { name: "New page" })).toBeTruthy();
    expect(document.querySelector('img[src="https://old.example/icon.png"]')).toBeNull();
  });

  it("keeps a native blank page hidden and reuses it for the next web request", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("native", "Old page", "https://old.example/", true)]);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Old page" });
    act(() => runtimeMocks.onEvent.mock.calls.at(-1)![0](page("native", "Blank page", "about:blank")));
    expect(screen.getByText("开始浏览")).toBeTruthy();

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
    expect(screen.queryByRole("textbox", { name: "地址栏" })).toBeNull();
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
    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe("https://requested.example/guide");
    expect(screen.getByRole("tab", { name: "https://requested.example/guide" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getAllByRole("tab")).toHaveLength(restoration === "page" ? 2 : 1);
    if (restoration === "page") expect(screen.getByRole("tab", { name: "Existing page" })).toBeTruthy();
    if (restoration === "blank") expect(runtimeMocks.navigate).toHaveBeenCalledWith("conv-browser", "existing", expect.any(String));
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
    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe("https://second.example/guide");
    expect(runtimeMocks.navigate).toHaveBeenCalledTimes(1);
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
    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe("https://example.com/");
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
    const address = screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement;
    fireEvent.change(address, { target: { value: "https://new.example/" } });
    fireEvent.submit(address.closest("form")!);
    await screen.findByRole("tab", { name: "https://new.example/" });

    await act(async () => reload.reject(new Error("Old automatic refresh failed")));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(address.value).toBe("https://new.example/");
    expect(runtimeMocks.list).toHaveBeenCalledTimes(1);
  });

  it("shows per-tab console and network diagnostics without exposing browser internals", async () => {
    runtimeMocks.list.mockResolvedValueOnce([{
      id: "agent_browser",
      conversationId: "conv-browser",
      type: "page",
      url: "https://example.com/",
      title: "Example",
      loading: false,
      canGoBack: false,
      canGoForward: false,
      active: true,
    }]);
    render(<BrowserPanel />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /Example/ })).toBeTruthy());

    openBrowserAction("打开页面诊断");
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "agent_browser", "console"));
    expect(screen.getByRole("tab", { name: /控制台/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: /网络/ }));
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenLastCalledWith("conv-browser", "agent_browser", "network"));
  });

  it("loads and updates embedded browser site settings", async () => {
    runtimeMocks.list.mockResolvedValueOnce([{
      id: "agent_browser",
      conversationId: "conv-browser",
      type: "page",
      url: "https://example.com/",
      title: "Example",
      loading: false,
      canGoBack: false,
      canGoForward: false,
      active: true,
    }]);
    render(<BrowserPanel />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /Example/ })).toBeTruthy());

    openBrowserAction("打开站点设置");
    await waitFor(() => expect(runtimeMocks.getSettings).toHaveBeenCalledWith("https://example.com/"));
    fireEvent.click(screen.getByRole("button", { name: "下载策略，当前：阻止" }));
    fireEvent.click(screen.getByRole("option", { name: "每次询问" }));

    await waitFor(() => expect(runtimeMocks.setSettings).toHaveBeenCalledWith({ downloadPolicy: "ask" }));
  });

  it("adds a dragged page region to the next agent turn", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    runtimeMocks.list.mockResolvedValueOnce([{
      id: "agent_browser",
      conversationId: "conv-browser",
      type: "page",
      url: "https://example.com/",
      title: "Example",
      loading: false,
      canGoBack: false,
      canGoForward: false,
      active: true,
    }]);
    runtimeMocks.inspect.mockResolvedValueOnce({
      ok: true,
      value: {
        selector: "",
        rect: { x: 100, y: 50, width: 200, height: 100 },
        viewport: { width: 1000, height: 500, devicePixelRatio: 1 },
        text: "",
      },
    });
    render(<><BrowserPanel /><textarea data-composer-input aria-label="主对话输入" /></>);
    await waitFor(() => expect(screen.getByRole("tab", { name: /Example/ })).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.click(screen.getByRole("button", { name: "框选区域" }));
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "agent_browser", "region"));
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "调整这里的圆角" } });
    fireEvent.click(screen.getByRole("button", { name: "加入对话" }));

    const annotation = useAppStore.getState().browserAnnotations[0];
    expect(annotation.note).toBe("调整这里的圆角");
    expect(annotation.xPercent).toBeCloseTo(0.2);
    expect(annotation.yPercent).toBeCloseTo(0.2);
    expect(annotation.widthPercent).toBeCloseTo(0.2);
    expect(annotation.heightPercent).toBeCloseTo(0.2);
    expect(useAppStore.getState().selectedMentions[0]?.kind).toBe("browser_annotation");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.focused)?.kind).toBe("chat");
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "composer:focus" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "主对话输入" })));
    expect((screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement).value).toBe("https://example.com/");
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
    const address = screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement;
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
    const address = screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement;
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
    const address = screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement;
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
    const address = screen.getByRole("textbox", { name: "地址栏" }) as HTMLInputElement;
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

  it("does not carry an annotation or pending page selection into another tab", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("a", "Page A", "https://a.example/", true),
      page("b", "Page B", "https://b.example/"),
    ]);
    const selection = pending<{ ok: boolean; value: unknown }>();
    runtimeMocks.inspect.mockReturnValueOnce(selection.promise);
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "Only for page A" } });
    fireEvent.click(screen.getByRole("button", { name: "选择元素" }));
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "a", "element"));
    fireEvent.click(screen.getByRole("tab", { name: "Page B" }));
    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));

    expect((screen.getByRole("textbox", { name: "批注内容" }) as HTMLTextAreaElement).value).toBe("");
    expect((screen.getByRole("button", { name: "选择元素" }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => selection.resolve({ ok: true, value: { selector: "#page-a", text: "Old A", rect: { x: 1, y: 2, width: 3, height: 4 }, viewport: { width: 100, height: 100 } } }));

    fireEvent.click(screen.getByText("高级元素信息"));
    expect((screen.getByRole("textbox", { name: "元素选择器" }) as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "Only for page B" } });
    fireEvent.click(screen.getByRole("button", { name: "加入对话" }));
    expect(useAppStore.getState().browserAnnotations[0]).toMatchObject({ targetId: "b", url: "https://b.example/", note: "Only for page B" });
    expect(useAppStore.getState().browserAnnotations[0].selector).toBeUndefined();
  });

  it("keeps diagnostics bound to the selected kind and reports retryable read failures", async () => {
    runtimeMocks.list.mockResolvedValueOnce([page("a", "Page A", "https://a.example/", true)]);
    const consoleResult = pending<{ ok: boolean; value: unknown[] }>();
    runtimeMocks.inspect.mockReturnValueOnce(consoleResult.promise).mockResolvedValueOnce({ ok: true, value: [{ url: "https://current-network.example/", statusCode: 200 }] });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    openBrowserAction("打开页面诊断");
    fireEvent.click(screen.getByRole("tab", { name: "网络" }));
    await screen.findByText("https://current-network.example/");

    await act(async () => consoleResult.resolve({ ok: true, value: [{ url: "https://stale-console.example/", message: "Old console" }] }));

    expect(screen.getByText("https://current-network.example/")).toBeTruthy();
    expect(screen.queryByText("https://stale-console.example/")).toBeNull();
    runtimeMocks.inspect.mockRejectedValueOnce(new Error("Diagnostics unavailable"));
    fireEvent.click(within(screen.getByRole("region", { name: "页面诊断" })).getByRole("button", { name: "刷新" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Diagnostics unavailable");
  });

  it("ignores a site's settings after the user moves to another tab", async () => {
    runtimeMocks.list.mockResolvedValueOnce([
      page("a", "Page A", "https://a.example/", true),
      page("b", "Page B", "https://b.example/"),
    ]);
    const oldSettings = pending<{ downloadPolicy: "block"; origin: string; permissions: string[] }>();
    runtimeMocks.getSettings.mockReturnValueOnce(oldSettings.promise).mockResolvedValueOnce({ downloadPolicy: "block", origin: "https://b.example", permissions: [] });
    render(<BrowserPanel />);
    await screen.findByRole("tab", { name: "Page A" });
    openBrowserAction("打开站点设置");
    fireEvent.click(screen.getByRole("tab", { name: "Page B" }));
    openBrowserAction("打开站点设置");
    await screen.findByText("https://b.example");

    await act(async () => oldSettings.resolve({ downloadPolicy: "block", origin: "https://a.example", permissions: ["geolocation"] }));

    expect(screen.queryByText("https://a.example")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "位置" }));
    await waitFor(() => expect(runtimeMocks.setSettings).toHaveBeenCalledWith({ origin: "https://b.example", permission: "geolocation", allowed: true }));
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

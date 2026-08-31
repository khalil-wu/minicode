/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserPanel, normalizeBrowserInput } from "./BrowserPanel";
import { __resetOpenWebInBrowserForTests, openWebInBrowser } from "../chat/openWebInBrowser";

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
  onEvent: vi.fn(() => () => {}),
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
  disconnect() {}
}

describe("BrowserPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetOpenWebInBrowserForTests();
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    useAppStore.setState({
      conversationId: "conv-browser",
      permissionMode: "bypass",
      browserAnnotations: [],
      selectedMentions: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("normalizes domains, local addresses, and search queries", () => {
    expect(normalizeBrowserInput("example.com/docs")).toBe("https://example.com/docs");
    expect(normalizeBrowserInput("localhost:5173")).toBe("http://localhost:5173");
    expect(normalizeBrowserInput("MiniCode browser")).toBe("https://www.bing.com/search?q=MiniCode%20browser");
  });

  it("renders a native-browser shell and opens typed addresses in the embedded view", async () => {
    render(<BrowserPanel />);

    expect(screen.getByText("开始浏览")).toBeTruthy();
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

    fireEvent.click(screen.getByRole("button", { name: "打开页面诊断" }));
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

    fireEvent.click(screen.getByRole("button", { name: "打开站点设置" }));
    await waitFor(() => expect(runtimeMocks.getSettings).toHaveBeenCalledWith("https://example.com/"));
    fireEvent.click(screen.getByRole("button", { name: "下载策略，当前：阻止" }));
    fireEvent.click(screen.getByRole("option", { name: "每次询问" }));

    await waitFor(() => expect(runtimeMocks.setSettings).toHaveBeenCalledWith({ downloadPolicy: "ask" }));
  });

  it("adds a dragged page region to the next agent turn", async () => {
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
    render(<BrowserPanel />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /Example/ })).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "添加页面批注" }));
    fireEvent.click(screen.getByRole("button", { name: "框选区域" }));
    await waitFor(() => expect(runtimeMocks.inspect).toHaveBeenCalledWith("conv-browser", "agent_browser", "region"));
    fireEvent.change(screen.getByRole("textbox", { name: "批注内容" }), { target: { value: "调整这里的圆角" } });
    fireEvent.click(screen.getByRole("button", { name: "加入智能体上下文" }));

    const annotation = useAppStore.getState().browserAnnotations[0];
    expect(annotation.note).toBe("调整这里的圆角");
    expect(annotation.xPercent).toBeCloseTo(0.2);
    expect(annotation.yPercent).toBeCloseTo(0.2);
    expect(annotation.widthPercent).toBeCloseTo(0.2);
    expect(annotation.heightPercent).toBeCloseTo(0.2);
    expect(useAppStore.getState().selectedMentions[0]?.kind).toBe("browser_annotation");
  });
});

// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { BrowserDownload } from "../desktop/runtime";
import { copyText } from "../lib/clipboard";
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

type MenuPageEvent = { id: string; conversationId: string; type: string; findDirection?: "next" | "previous"; zoomFactor?: number };

const native = vi.hoisted(() => ({ pageCommand: vi.fn(async () => ({ requestId: 1, zoomFactor: 1, hostname: "example.com", origin: "https://example.com", secure: true, cookieCount: 2 })), downloads: vi.fn(async () => []),
  onDownloadsChanged: vi.fn((listener: (records: BrowserDownload[]) => void) => { native.downloadListeners.add(listener); return () => native.downloadListeners.delete(listener); }), downloadAction: vi.fn(async () => ""),
  getSitePolicy: vi.fn(async () => ({ categories: [], defaults: {}, sites: [], devices: [] })),
  setSettings: vi.fn(async () => ({})), getSettings: vi.fn(async () => ({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false, downloadPolicy: "allow", origin: "https://example.com", permissions: [] })), onSettingsChanged: vi.fn(() => () => {}), clearSiteData: vi.fn(async () => true), pageListeners: new Set<(event: MenuPageEvent) => void>(), downloadListeners: new Set<(records: BrowserDownload[]) => void>() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ embeddedBrowser: native }), onEmbeddedBrowserEvent: (listener: (event: MenuPageEvent) => void) => { native.pageListeners.add(listener); return () => native.pageListeners.delete(listener); }, isDesktop: () => true,
  embeddedBrowserGetSettings: native.getSettings, embeddedBrowserSetSettings: native.setSettings, embeddedBrowserClearSiteData: native.clearSiteData, onEmbeddedBrowserSettingsChanged: native.onSettingsChanged }));
import { BrowserDeviceToolbar, BrowserDownloads, BrowserFindBar, BrowserPageActions, BrowserSiteInfo, DEFAULT_DEVICE_VIEWPORT } from "./BrowserPageActions";

afterEach(() => { cleanup(); native.pageListeners.clear(); native.downloadListeners.clear(); vi.clearAllMocks(); });
const menu = () => render(<BrowserPageActions conversationId="owner" tabId="native-page" url="https://example.com/" position={{ x: 30, y: 30 }}
  viewportEnabled={false} onViewportChange={vi.fn()} onClose={vi.fn()} />);

describe("observed Codex browser menu", () => {
  it("starts a native finding session before navigating its next and previous matches", async () => {
    render(<BrowserFindBar conversationId="owner" tabId="native-page" />);
    fireEvent(window, new CustomEvent("browser:find", { detail: "native-page" }));
    fireEvent.change(screen.getByRole("textbox", { name: "在页面中查找" }), { target: { value: "needle" } });
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: true, forward: true }));
    fireEvent.click(screen.getByRole("button", { name: "下一个匹配项" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: false, forward: true }));
    fireEvent.click(screen.getByRole("button", { name: "上一个匹配项" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: false, forward: false }));
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent(window, new CustomEvent("browser:find", { detail: "native-page" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: true, forward: true }));
    native.pageCommand.mockResolvedValueOnce({ matches: 0 } as never);
    fireEvent.change(screen.getByRole("textbox", { name: "在页面中查找" }), { target: { value: "" } });
    await waitFor(() => expect(screen.getByText("0/0")).toBeTruthy());
    expect(screen.getByRole("textbox", { name: "在页面中查找" })).toBeTruthy();
  });

  it("uses native page commands and does not introduce an element picker or diagnostics entry", async () => {
    menu();
    expect(screen.queryByText("选择元素")).toBeNull();
    expect(screen.queryByText("打开页面诊断")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "打印" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenCalledWith({ id: "native-page", conversationId: "owner", action: "print" }));
  });
  it("keeps page zoom in the menu and passes its actual target identity", async () => {
    menu();
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenCalledWith({ id: "native-page", conversationId: "owner", action: "zoom", zoomFactor: 1.1 }));
  });
  it("uses the installed percentage ladder for repeated page-menu zoom", async () => {
    menu();
    await waitFor(() => expect(native.pageCommand).toHaveBeenCalledWith({ id: "native-page", conversationId: "owner", action: "zoom" }));
    native.pageCommand.mockResolvedValueOnce({ zoomFactor: 1.1 } as never);
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await screen.findByText("110%");
    native.pageCommand.mockResolvedValueOnce({ zoomFactor: 1.25 } as never);
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "zoom", zoomFactor: 1.25 }));
  });
  it("routes native Find Next and Previous through the same query and page owner", async () => {
    render(<BrowserFindBar conversationId="owner" tabId="native-page" />);
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "find-request" })));
    fireEvent.change(screen.getByRole("textbox", { name: "在页面中查找" }), { target: { value: "needle" } });
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: true, forward: true }));
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "other-owner", type: "find-request", findDirection: "next" })));
    expect(native.pageCommand.mock.calls.at(-1)?.[0]).toMatchObject({ findNext: true });
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "find-request", findDirection: "next" })));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: false, forward: true }));
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "find-request", findDirection: "previous" })));
    await waitFor(() => expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "find", text: "needle", findNext: false, forward: false }));
  });
  it("updates device zoom from the application menu's real native-page event", async () => {
    render(<BrowserDeviceToolbar conversationId="owner" tabId="native-page" viewport={DEFAULT_DEVICE_VIEWPORT} onChange={vi.fn()} />);
    await waitFor(() => expect(native.pageCommand).toHaveBeenCalledWith({ id: "native-page", conversationId: "owner", action: "zoom" }));
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "updated", zoomFactor: 1.25 })));
    expect((screen.getByRole("combobox", { name: "浏览器缩放" }) as HTMLSelectElement).value).toBe("125");
  });
  it("persists the observed new-tab device preference independently of the current tab", async () => {
    menu();
    fireEvent.mouseEnter(screen.getByRole("menuitem", { name: "设备工具栏" }));
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "始终在新标签页中显示" }));
    await waitFor(() => expect(native.setSettings).toHaveBeenCalledWith({ deviceToolbarAlwaysOnNewTabs: true }));
  });
  it("disables the current-page device action on a blank tab while keeping the new-tab preference usable", async () => {
    render(<BrowserPageActions conversationId="owner" tabId="blank-page" url="" position={{ x: 30, y: 30 }} viewportEnabled={false} onViewportChange={vi.fn()} onClose={vi.fn()} />);
    fireEvent.mouseEnter(screen.getByRole("menuitem", { name: "设备工具栏" }));
    expect((await screen.findByRole("menuitem", { name: "显示设备工具栏" })).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("menuitemcheckbox", { name: "始终在新标签页中显示" }).getAttribute("aria-disabled")).not.toBe("true");
  });
  it("uses the installed Codex device presets and passes viewport changes through the same target", async () => {
    const change = vi.fn();
    render(<BrowserDeviceToolbar conversationId="owner" tabId="native-page" viewport={DEFAULT_DEVICE_VIEWPORT} onChange={change} />);
    expect(screen.getByRole("combobox", { name: "设备视口" }).querySelectorAll("option")).toHaveLength(13);
    expect(screen.getByRole("button", { name: "旋转视口" }).compareDocumentPosition(screen.getByRole("combobox", { name: "浏览器缩放" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(screen.getByRole("combobox", { name: "设备视口" }), { target: { value: "iphone-15-pro" } });
    expect(change).toHaveBeenCalledWith({ width: 393, height: 852, mobile: false, presetId: "iphone-15-pro" });
    await waitFor(() => expect(native.pageCommand).toHaveBeenCalledWith({ id: "native-page", conversationId: "owner", action: "zoom", zoomFactor: 1 }));
    fireEvent.click(screen.getByRole("button", { name: "退出设备工具栏模式" }));
    expect(change).toHaveBeenLastCalledWith(null);
  });
  it("commits device dimensions on Enter or blur using the source width and height limits", () => {
    const change = vi.fn();
    render(<BrowserDeviceToolbar conversationId="owner" tabId="native-page" viewport={DEFAULT_DEVICE_VIEWPORT} onChange={change} />);
    const width = screen.getByRole("spinbutton", { name: "视口宽度" });
    width.focus();
    fireEvent.change(width, { target: { value: "100" } });
    fireEvent.keyDown(width, { key: "Enter" });
    expect(change).toHaveBeenLastCalledWith({ ...DEFAULT_DEVICE_VIEWPORT, width: 240 });
    const height = screen.getByRole("spinbutton", { name: "视口高度" });
    height.focus();
    fireEvent.change(height, { target: { value: "5000" } });
    fireEvent.blur(height);
    expect(change).toHaveBeenLastCalledWith({ ...DEFAULT_DEVICE_VIEWPORT, height: 4096 });
  });
  it("opens site information and connects its nested data action to the same real page", async () => {
    render(<BrowserSiteInfo conversationId="owner" tabId="native-page" url="https://example.com/" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cookie 和网站数据" }));
    expect(screen.getByText("使用中的 Cookie：2")).toBeTruthy();
    expect(screen.queryByLabelText("下载策略")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "删除网站数据" }));
    await waitFor(() => expect(native.clearSiteData).toHaveBeenCalledWith("owner", "native-page"));
    await screen.findByText("使用中的 Cookie：0");
  });

  it("keeps newer native page zoom over a late initial read and an older command reply", async () => {
    let initial!: (value: { zoomFactor: number }) => void;
    native.pageCommand.mockReturnValueOnce(new Promise(resolve => { initial = resolve; }) as never);
    menu();
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "another-owner", type: "updated", zoomFactor: 3 })));
    expect(screen.getByText("100%")).toBeTruthy();
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "updated", zoomFactor: 1.5 })));
    await act(async () => initial({ zoomFactor: 1 }));
    expect(screen.getByText("150%")).toBeTruthy();
    let oldCommand!: (value: { zoomFactor: number }) => void;
    native.pageCommand.mockReturnValueOnce(new Promise(resolve => { oldCommand = resolve; }) as never);
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    expect(native.pageCommand).toHaveBeenLastCalledWith({ id: "native-page", conversationId: "owner", action: "zoom", zoomFactor: 1.75 });
    act(() => native.pageListeners.forEach(listener => listener({ id: "native-page", conversationId: "owner", type: "updated", zoomFactor: 2 })));
    await act(async () => oldCommand({ zoomFactor: 1.75 }));
    expect(screen.getByText("200%")).toBeTruthy();
  });

  it("does not apply an old tab's pending zoom reply to a newly selected page", async () => {
    let finish!: (value: { zoomFactor: number }) => void;
    native.pageCommand.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }) as never);
    const view = menu();
    native.pageCommand.mockResolvedValueOnce({ zoomFactor: 1.25 } as never);
    view.rerender(<BrowserPageActions conversationId="owner" tabId="next-page" url="https://next.example/" position={{ x: 30, y: 30 }} viewportEnabled={false} onViewportChange={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText("125%");
    await act(async () => finish({ zoomFactor: 3 }));
    expect(screen.getByText("125%")).toBeTruthy();
  });

  it("uses native download pushes and current menu records instead of late or captured snapshots", async () => {
    let initial!: (records: BrowserDownload[]) => void;
    native.downloads.mockReturnValueOnce(new Promise(resolve => { initial = resolve; }) as never);
    const record: BrowserDownload = { id: "live", filename: "live.txt", url: "https://example.com/live.txt", path: "C:/audit/live.txt", state: "progressing",
      receivedBytes: 10, totalBytes: 100, startedAt: 1, canPause: true, canResume: false, canCancel: true, fileExists: false };
    render(<BrowserDownloads onClose={vi.fn()} />);
    act(() => native.downloadListeners.forEach(listener => listener([record])));
    await act(async () => initial([{ ...record, id: "stale", filename: "stale.txt" }]));
    expect(screen.queryByRole("button", { name: /stale.txt/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "live.txt 下载操作" }));
    expect(screen.getByRole("menuitem", { name: "打开", exact: true }).getAttribute("aria-disabled")).toBe("true");
    const completed = { ...record, filename: "actual-file.txt", path: "C:/audit/actual-file.txt", state: "completed", canPause: false, canCancel: false, fileExists: true, receivedBytes: 100 };
    act(() => native.downloadListeners.forEach(listener => listener([completed])));
    expect(screen.getByRole("menuitem", { name: "打开", exact: true }).getAttribute("aria-disabled")).not.toBe("true");
    expect(screen.getByRole("menuitem", { name: "从列表中移除" }).getAttribute("aria-disabled")).not.toBe("true");
    fireEvent.click(screen.getByRole("menuitem", { name: "复制路径" }));
    expect(copyText).toHaveBeenLastCalledWith(completed.path, "路径");
    fireEvent.click(screen.getByRole("button", { name: "actual-file.txt 下载操作" }));
    act(() => native.downloadListeners.forEach(listener => listener([])));
    expect(screen.queryByRole("menu", { name: "下载操作" })).toBeNull();
  });
});

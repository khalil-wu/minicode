// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserPanel } from "./BrowserPanel";
import { __resetOpenWebInBrowserForTests, openNewBrowserTab } from "../chat/openWebInBrowser";
import { draftBrowserOwnerId, readBrowserWorkbench, writeBrowserWorkbench } from "../stores/browser-workbench";
import type { ConversationDraftContext } from "../stores/types";
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null, error: "", update: vi.fn() }) }));

const native = vi.hoisted(() => ({
  list: vi.fn(), navigate: vi.fn(), bounds: vi.fn(),
}));
vi.mock("../desktop/runtime", async load => ({
  ...await load<typeof import("../desktop/runtime")>(),
  desktop: () => null,
  isDesktop: () => true,
  embeddedBrowserList: native.list,
  embeddedBrowserGetSettings: async () => ({ browserEnabled: true, deviceToolbarAlwaysOnNewTabs: false }),
  embeddedBrowserNavigate: native.navigate,
  embeddedBrowserSetBounds: native.bounds,
  embeddedBrowserActivate: async () => true,
  onEmbeddedBrowserEvent: () => () => {},
  onEmbeddedBrowserSettingsChanged: () => () => {},
}));
vi.mock("../components/BrandIcon", () => ({ BrandIcon: () => null }));

const initial = useAppStore.getState();
beforeEach(() => {
  localStorage.clear();
  __resetOpenWebInBrowserForTests();
  native.list.mockReset().mockResolvedValue([]);
  native.navigate.mockReset().mockImplementation(async (conversationId, id, url) => ({ conversationId, id, url,
    title: "Actual draft page", type: "updated", loading: false, canGoBack: false, canGoForward: false }));
  native.bounds.mockReset().mockResolvedValue(true);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  useAppStore.setState({ ...initial, conversationId: null, activeConversationDraftId: null, conversationDrafts: {}, workingDirectory: "C:/Workspace/B",
    runtimeSession: { active_conversation_id: "latest-owner" } as never }, true);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("browser owner entry", () => {
  const draft = (id: string) => ({ id, workspaceRoot: "", bindWorkspace: false, appMode: "cowork",
    permissionMode: "confirm", permissionEdited: false, modelSelection: { mode: "default" },
    selectionLoading: false, selectionError: null, preferencesLoading: false } as ConversationDraftContext);

  it("opens and navigates a real draft-owned page without creating an Agent conversation", async () => {
    const create = vi.fn();
    useAppStore.setState({ conversationId: null, activeConversationDraftId: "home", conversationDrafts: { home: draft("home") },
      draft: "Keep my unsent message", createConversation: create });
    act(() => openNewBrowserTab());
    const view = render(<BrowserPanel />);
    const address = await view.findByRole("textbox", { name: "搜索或输入网址" });
    expect(view.getAllByRole("tab")).toHaveLength(1);
    fireEvent.change(address, { target: { value: "https://example.com/draft" } });
    fireEvent.submit(address.closest("form")!);
    await waitFor(() => expect(native.navigate).toHaveBeenCalledWith(draftBrowserOwnerId("home"), expect.any(String), "https://example.com/draft"));
    await view.findByRole("tab", { name: "Actual draft page" });
    expect(native.list).toHaveBeenCalledWith(draftBrowserOwnerId("home"));
    expect(create).not.toHaveBeenCalled();
    expect(useAppStore.getState()).toMatchObject({ conversationId: null, activeConversationDraftId: "home", draft: "Keep my unsent message" });
    expect(readBrowserWorkbench(draftBrowserOwnerId("home"))?.tabs[0].url).toBe("https://example.com/draft");
  });

  it("restores draft tabs and selected address after switching away and remounting", async () => {
    const owner = draftBrowserOwnerId("home");
    writeBrowserWorkbench(owner, { tabs: [{ id: "saved-blank", title: "Saved blank", url: "", draftUrl: "unsent address" },
      { id: "saved-page", title: "Saved page", url: "https://example.com/saved", draftUrl: "https://example.com/saved" }], activeId: "saved-blank" });
    useAppStore.setState({ activeConversationDraftId: "home", conversationDrafts: { home: draft("home"), other: draft("other") } });
    const view = render(<BrowserPanel />);
    await waitFor(() => expect((view.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("unsent address"));
    expect(native.navigate).not.toHaveBeenCalled();
    expect(view.getAllByRole("tab")).toHaveLength(2);
    act(() => useAppStore.setState({ activeConversationDraftId: "other" }));
    await view.findByRole("textbox", { name: "搜索或输入网址" });
    expect(view.queryByRole("tab", { name: "Saved page" })).toBeNull();
    view.unmount();
    useAppStore.setState({ activeConversationDraftId: "home" });
    const restored = render(<BrowserPanel />);
    await waitFor(() => expect((restored.getByRole("textbox", { name: "搜索或输入网址" }) as HTMLInputElement).value).toBe("unsent address"));
    fireEvent.click(restored.getByRole("tab", { name: "Saved page" }));
    await waitFor(() => expect(native.navigate).toHaveBeenCalledWith(owner, "saved-page", "https://example.com/saved"));
    expect(readBrowserWorkbench(owner)?.activeId).toBe("saved-page");
    expect(useAppStore.getState().conversationId).toBeNull();
  });

  it("opens project draft Changes through its existing workspace without materializing an Agent", async () => {
    useAppStore.setState({ activeConversationDraftId: "project", conversationDrafts: { project: draft("project") },
      workingDirectory: "C:/Projects/draft", draft: "Keep project draft" });
    const view = render(<BrowserPanel />);
    const changes = await view.findByRole("button", { name: "变更" });
    expect(changes.hasAttribute("disabled")).toBe(false);
    expect(view.getByRole("button", { name: "终端" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(changes);
    expect(useAppStore.getState()).toMatchObject({ conversationId: null, activeConversationDraftId: "project",
      workingDirectory: "C:/Projects/draft", draft: "Keep project draft", rightStackTab: "diff" });
    expect(native.navigate).not.toHaveBeenCalled();
  });

  it("restores a saved URL once while switching tabs during its pending native navigation", async () => {
    const owner = draftBrowserOwnerId("home");
    writeBrowserWorkbench(owner, { tabs: [{ id: "saved-page", title: "Saved page", url: "https://example.com/saved", draftUrl: "https://example.com/saved" },
      { id: "saved-blank", title: "Saved blank", url: "", draftUrl: "" }], activeId: "saved-page" });
    useAppStore.setState({ activeConversationDraftId: "home", conversationDrafts: { home: draft("home") } });
    let finish!: (target: unknown) => void;
    native.navigate.mockReturnValueOnce(new Promise(done => { finish = done; }));
    const view = render(<BrowserPanel />);
    await waitFor(() => expect(native.navigate).toHaveBeenCalledExactlyOnceWith(owner, "saved-page", "https://example.com/saved"));
    fireEvent.click(view.getByRole("tab", { name: "Saved blank" }));
    fireEvent.click(view.getByRole("tab", { name: "Saved page" }));
    expect(native.navigate).toHaveBeenCalledTimes(1);
    await act(async () => finish({ id: "saved-page", conversationId: owner, title: "Loaded page", url: "https://example.com/saved",
      type: "updated", loading: false, canGoBack: false, canGoForward: false }));
    await view.findByRole("tab", { name: "Loaded page" });
    expect(native.navigate).toHaveBeenCalledTimes(1);
  });

  it("offers a keyboard-readable empty state instead of fake navigation for nil owner", async () => {
    const view = render(<BrowserPanel />);
    await act(async () => {});
    const status = view.getByRole("status");
    expect(status.textContent).toContain("请先选择会话，再使用浏览器");
    expect(status.textContent).toContain("从侧边栏选择一个会话");
    expect(status.tabIndex).toBe(0);
    status.focus();
    expect(document.activeElement).toBe(status);
    expect(view.queryByLabelText("搜索或输入网址")).toBeNull();
    expect(view.container.querySelector(".mc-browser-spin")).toBeNull();
    expect(native.list).not.toHaveBeenCalled();
    expect(native.navigate).not.toHaveBeenCalled();
    expect(useAppStore.getState().conversationId).toBeNull();
  });

  it("returns to the empty state and hides the exact previous owner's native view", async () => {
    native.list.mockResolvedValue([{ id: "native-a", conversationId: "A", title: "A page",
      url: "https://example.com/", active: true, loading: false, canGoBack: false, canGoForward: false }]);
    useAppStore.setState({ conversationId: "A" });
    const view = render(<BrowserPanel />);
    await waitFor(() => expect(view.getByLabelText("搜索或输入网址")).toBeTruthy());
    act(() => useAppStore.setState({ conversationId: null }));
    expect(view.getByRole("status").textContent).toContain("请先选择会话");
    expect(view.queryByLabelText("搜索或输入网址")).toBeNull();
    expect(native.bounds).toHaveBeenCalledWith({ id: "native-a", conversationId: "A", x: 0, y: 0, width: 0, height: 0 });
    expect(native.navigate).not.toHaveBeenCalled();
  });

  it("keeps the nil-owner prompt visible when the old owner's hydration finishes late", async () => {
    let resolve!: (value: unknown[]) => void;
    native.list.mockReturnValue(new Promise((done) => { resolve = done; }));
    useAppStore.setState({ conversationId: "A" });
    const view = render(<BrowserPanel />);
    act(() => useAppStore.setState({ conversationId: null }));
    await act(async () => resolve([{ id: "late-a", conversationId: "A", title: "old page", url: "https://example.com/" }]));
    expect(view.getByRole("status").textContent).toContain("请先选择会话");
    expect(view.queryByLabelText("搜索或输入网址")).toBeNull();
    expect(view.container.querySelector(".mc-browser-spin")).toBeNull();
  });

  it("restores the normal address entry after an actual conversation is selected", async () => {
    const view = render(<BrowserPanel />);
    act(() => useAppStore.setState({ conversationId: "chosen-owner" }));
    await waitFor(() => expect(view.getByLabelText("搜索或输入网址")).toBeTruthy());
    expect(native.list).toHaveBeenCalledWith("chosen-owner");
    expect(view.queryByText("请先选择会话，再使用浏览器")).toBeNull();
    expect(native.navigate).not.toHaveBeenCalled();
  });
});

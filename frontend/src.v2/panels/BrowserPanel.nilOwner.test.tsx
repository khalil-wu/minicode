// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { BrowserPanel } from "./BrowserPanel";

const native = vi.hoisted(() => ({
  list: vi.fn(), navigate: vi.fn(), bounds: vi.fn(),
}));
vi.mock("../desktop/runtime", () => ({
  isDesktop: () => true,
  embeddedBrowserList: native.list,
  embeddedBrowserNavigate: native.navigate,
  embeddedBrowserSetBounds: native.bounds,
  embeddedBrowserActivate: async () => true,
  onEmbeddedBrowserEvent: () => () => {},
}));
vi.mock("../components/BrandIcon", () => ({ BrandIcon: () => null }));

const initial = useAppStore.getState();
beforeEach(() => {
  native.list.mockReset().mockResolvedValue([]);
  native.navigate.mockReset();
  native.bounds.mockReset().mockResolvedValue(true);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  useAppStore.setState({ ...initial, conversationId: null, workingDirectory: "C:/Workspace/B",
    runtimeSession: { active_conversation_id: "latest-owner" } as never }, true);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("browser owner entry", () => {
  it("offers a keyboard-readable empty state instead of fake navigation for nil owner", async () => {
    const view = render(<BrowserPanel />);
    await act(async () => {});
    const status = view.getByRole("status");
    expect(status.textContent).toContain("请先选择会话，再使用浏览器");
    expect(status.textContent).toContain("从侧边栏选择一个会话");
    expect(status.tabIndex).toBe(0);
    status.focus();
    expect(document.activeElement).toBe(status);
    expect(view.queryByLabelText("地址栏")).toBeNull();
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
    await waitFor(() => expect(view.getByLabelText("地址栏")).toBeTruthy());
    act(() => useAppStore.setState({ conversationId: null }));
    expect(view.getByRole("status").textContent).toContain("请先选择会话");
    expect(view.queryByLabelText("地址栏")).toBeNull();
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
    expect(view.queryByLabelText("地址栏")).toBeNull();
    expect(view.container.querySelector(".mc-browser-spin")).toBeNull();
  });

  it("restores the normal address entry after an actual conversation is selected", async () => {
    const view = render(<BrowserPanel />);
    act(() => useAppStore.setState({ conversationId: "chosen-owner" }));
    await waitFor(() => expect(view.getByLabelText("地址栏")).toBeTruthy());
    expect(native.list).toHaveBeenCalledWith("chosen-owner");
    expect(view.queryByText("请先选择会话，再使用浏览器")).toBeNull();
    expect(native.navigate).not.toHaveBeenCalled();
  });
});

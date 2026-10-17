// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChangesToolbarActions, type ChangesToolbarActionsProps } from "./ChangesToolbarActions";

const props = (changes: Partial<ChangesToolbarActionsProps> = {}): ChangesToolbarActionsProps => ({
  width: 480, mode: "auto", onModeChange: vi.fn(), wrap: false, onWrapChange: vi.fn(),
  allExpanded: true, onToggleAll: vi.fn(), filesVisible: false, onToggleFiles: vi.fn(), onJumpToFile: vi.fn(),
  ...changes,
});
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Source Changes toolbar actions", () => {
  it("keeps jump and files visible while moving all wide actions into the compact options menu", () => {
    const callbacks = props({ onRefresh: vi.fn() });
    render(<ChangesToolbarActions {...callbacks} />);
    expect(screen.getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual(["“变更”选项", "跳转到文件", "显示文件"]);
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    const menu = screen.getByRole("menu", { name: "“变更”选项" });
    expect(Array.from(menu.querySelectorAll('[role^="menuitem"]')).map(item => item.textContent)).toEqual([
      "刷新", "自动换行", "自动差异视图：改用统一差异视图", "折叠全部差异", "加载完整文件", "渲染预览", "词级差异", "隐藏空白字符", "隐藏导入内容",
    ]);
    fireEvent.click(within(menu).getByRole("menuitemcheckbox", { name: "自动换行" }));
    expect(callbacks.onWrapChange).toHaveBeenCalledWith(true);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("switches to independent controls at exactly 625px and leaves five display preferences in the menu", () => {
    const callbacks = props({ width: 624, onRefresh: vi.fn() });
    const view = render(<ChangesToolbarActions {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    view.rerender(<ChangesToolbarActions {...callbacks} width={625} />);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual([
      "“变更”选项", "跳转到文件", "刷新", "自动换行", "折叠全部差异", "自动差异视图：改用统一差异视图", "显示文件",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(5);
    expect(screen.queryByRole("menuitem", { name: "刷新" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.getByRole("button", { name: "“变更”选项" })).toBe(document.activeElement);
  });

  it("uses the Source three-state cycle from each current layout and the real full-file preference", () => {
    const callbacks = props({ width: 700, mode: "unified" });
    const view = render(<ChangesToolbarActions {...callbacks} />);
    fireEvent.click(screen.getByRole("button", { name: "切换到拆分差异视图" }));
    expect(callbacks.onModeChange).toHaveBeenLastCalledWith("split");
    view.rerender(<ChangesToolbarActions {...callbacks} mode="split" />);
    fireEvent.click(screen.getByRole("button", { name: "将差异视图切换为自动布局" }));
    expect(callbacks.onModeChange).toHaveBeenLastCalledWith("auto");
    view.rerender(<ChangesToolbarActions {...callbacks} mode="auto" />);
    fireEvent.click(screen.getByRole("button", { name: "自动差异视图：改用统一差异视图" }));
    expect(callbacks.onModeChange).toHaveBeenLastCalledWith("unified");
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "加载完整文件" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "加载完整文件" }));
    expect(localStorage.getItem("load-full-files")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "加载完整文件" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "隐藏导入内容" }));
    expect(localStorage.getItem("hideDiffImports")).toBe("true");
  });

  it("projects true toggle states and passes the jump anchor without inventing a refresh callback", () => {
    const callbacks = props({ width: 800, filesVisible: true, wrap: true, allExpanded: false, jumpOpen: true });
    render(<ChangesToolbarActions {...callbacks} />);
    expect(screen.queryByRole("button", { name: "刷新" })).toBeNull();
    expect(screen.getByRole("button", { name: "隐藏文件" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "自动换行" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "隐藏文件" }));
    expect(callbacks.onToggleFiles).toHaveBeenCalledOnce();
    const jump = screen.getByRole("button", { name: "跳转到文件" });
    expect(jump.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(jump);
    expect(callbacks.onJumpToFile).toHaveBeenCalledWith(jump);
    fireEvent.click(screen.getByRole("button", { name: "展开全部差异" }));
    expect(callbacks.onToggleAll).toHaveBeenCalledOnce();
  });

  it("keeps busy and disabled refresh states in their actual compact and wide surfaces", () => {
    const callbacks = props({ onRefresh: vi.fn(), refreshing: true, refreshDisabled: true });
    const view = render(<ChangesToolbarActions {...callbacks} />);
    expect(screen.getByRole("button", { name: "“变更”选项" }).getAttribute("aria-busy")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    expect((screen.getByRole("menuitem", { name: "刷新" }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<ChangesToolbarActions {...callbacks} width={700} />);
    expect((screen.getByRole("button", { name: "刷新" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

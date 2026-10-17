// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChangesFileActions } from "./ChangesFileActions";

const clipboard = vi.fn();
beforeEach(() => {
  clipboard.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: clipboard } });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Source per-file Changes actions", () => {
  it("keeps the built-in tab and external editor separate without toggling the enclosing file", () => {
    const toggle = vi.fn(), open = vi.fn(), external = vi.fn(), header = vi.fn();
    render(<div onClick={header}><ChangesFileActions path="src/main.ts" open onToggle={toggle} onOpen={open} onOpenExternal={external} /></div>);
    fireEvent.click(screen.getByRole("button", { name: "打开位置" }));
    expect(open).toHaveBeenCalledOnce();
    expect(external).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "在编辑器中打开" }));
    expect(external).toHaveBeenCalledOnce();
    expect(open).toHaveBeenCalledOnce();
    expect(toggle).not.toHaveBeenCalled();
    expect(header).not.toHaveBeenCalled();
  });

  it("copies the actual displayed relative path and preserves the exact Source menu action order", async () => {
    const open = vi.fn(), external = vi.fn(), toggle = vi.fn(), header = vi.fn();
    render(<div onClick={header}><ChangesFileActions path="src/你好 file.ts" open onOpen={open} onToggle={toggle} onOpenExternal={external} /></div>);
    const more = screen.getByRole("button", { name: "文件操作" });
    fireEvent.click(more);
    const menu = screen.getByRole("menu", { name: "文件操作" });
    expect(within(menu).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["复制路径", "在编辑器中打开", "折叠文件"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "复制路径" }));
    await waitFor(() => expect(clipboard).toHaveBeenCalledWith("src/你好 file.ts"));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(header).not.toHaveBeenCalled();
    expect(toggle).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    expect(external).not.toHaveBeenCalled();
    fireEvent.click(more);
    fireEvent.click(screen.getByRole("menuitem", { name: "在编辑器中打开" }));
    expect(external).toHaveBeenCalledOnce();
    expect(header).not.toHaveBeenCalled();
  });

  it("omits the unavailable external editor and reflects the real expand/collapse state", () => {
    const toggle = vi.fn(), open = vi.fn(), header = vi.fn();
    const view = render(<div onClick={header}><ChangesFileActions path="lib/a.ts" open={false} onOpen={open} onToggle={toggle} /></div>);
    expect(screen.queryByRole("button", { name: "在编辑器中打开" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "文件操作" }));
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["复制路径", "展开文件"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "展开文件" }));
    expect(toggle).toHaveBeenCalledOnce();
    expect(header).not.toHaveBeenCalled();
    view.rerender(<div onClick={header}><ChangesFileActions path="lib/a.ts" open onOpen={open} onToggle={toggle} /></div>);
    fireEvent.click(screen.getByRole("button", { name: "文件操作" }));
    expect(screen.getByRole("menuitem", { name: "折叠文件" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "在编辑器中打开" })).toBeNull();
  });

  it("closes its menu with Escape and returns keyboard focus to the real file trigger", () => {
    const header = vi.fn();
    render(<div onClick={header}><ChangesFileActions path="a.ts" open onOpen={vi.fn()} onToggle={vi.fn()} /></div>);
    const trigger = screen.getByRole("button", { name: "文件操作" });
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(header).not.toHaveBeenCalled();
  });
});

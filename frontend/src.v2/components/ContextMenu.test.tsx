// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContextMenu } from "./ContextMenu";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

const menuLayout = (width: number, height: number) => {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(width);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(height);
};

describe("ContextMenu keyboard navigation", () => {
  it("typeaheads and returns through a five-level submenu without selecting a different level", () => {
    const action = vi.fn(), close = vi.fn();
    render(<ContextMenu position={{ x: 0, y: 0 }} onClose={close} items={[
      { label: "Archive" }, { label: "Copy", submenu: [{ label: "Copy source", submenu: [{ label: "Files", submenu: [{ label: "Nested", submenu: [
        { label: "Unavailable", disabled: true }, { label: "Alpha" }, { label: "Beta", onClick: action },
      ] }] }] }] },
    ]} />);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "c" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Copy" }));
    for (const label of ["Copy source", "Files", "Nested", "Alpha"]) {
      fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
      expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: label }));
    }
    fireEvent.keyDown(document.activeElement!, { key: "b" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Beta" }));
    for (const shiftKey of [false, true]) {
      expect(fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey })).toBe(false);
      expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Beta" }));
      expect(close).not.toHaveBeenCalled();
    }
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Nested" }));
    expect(action).not.toHaveBeenCalled();
  });
  it("opens a submenu after separators with Right, navigates only that menu, and returns with Left", () => {
    const close = vi.fn(), choose = vi.fn();
    render(<ContextMenu position={{ x: 20, y: 20 }} onClose={close} items={[
      { label: "Rename" }, { label: "", separator: true },
      { label: "Open in", submenu: [{ label: "VS Code", onClick: choose }, { label: "File Explorer" }] },
    ]} />);
    const parent = screen.getByRole("menuitem", { name: "Open in" });
    parent.focus();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "VS Code" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "File Explorer" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull();
    expect(document.activeElement).toBe(parent);
    expect(close).not.toHaveBeenCalled();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    fireEvent.click(screen.getByRole("menuitem", { name: "VS Code" }));
    expect(close).toHaveBeenCalledOnce();
    expect(choose).toHaveBeenCalledOnce();
  });
  it("stops at the first and last enabled items while skipping separators and disabled actions", () => {
    render(<ContextMenu position={{ x: 0, y: 0 }} onClose={vi.fn()} items={[
      { label: "Unavailable", disabled: true }, { label: "First" },
      { label: "", separator: true }, { label: "Last" },
    ]} />);
    const key = (value: string) => fireEvent.keyDown(document.activeElement!, { key: value });
    key("ArrowDown");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "First" }));
    key("ArrowDown");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Last" }));
    key("ArrowDown");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Last" }));
    key("End");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Last" }));
    key("Home");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "First" }));
    key("ArrowUp");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "First" }));
  });

  it("searches a multiword prefix without letting Space activate the focused native button", () => {
    const select = vi.fn();
    render(<ContextMenu position={{ x: 0, y: 0 }} onClose={vi.fn()} items={[
      { label: "Copy project", onClick: select }, { label: "Copy source", onClick: select },
    ]} />);
    for (const key of ["c", "o", "p", "y", " "]) {
      expect(fireEvent.keyDown(document.activeElement!, { key })).toBe(false);
    }
    fireEvent.keyDown(document.activeElement!, { key: "s" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Copy source" }));
    expect(select).not.toHaveBeenCalled();
  });

  it("waits 200ms before hovering open and cancels a pending flyout on leaving or keyboard input", () => {
    vi.useFakeTimers();
    render(<ContextMenu position={{ x: 20, y: 20 }} onClose={vi.fn()} items={[
      { label: "Help", submenu: [{ label: "Guide" }] },
    ]} />);
    const help = screen.getByRole("menuitem", { name: "Help" });
    fireEvent.mouseEnter(help);
    expect(document.activeElement).toBe(help);
    act(() => vi.advanceTimersByTime(199));
    expect(screen.queryByRole("menuitem", { name: "Guide" })).toBeNull();
    fireEvent.mouseLeave(help);
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole("menuitem", { name: "Guide" })).toBeNull();
    fireEvent.mouseEnter(help);
    fireEvent.keyDown(help, { key: "h" });
    act(() => vi.advanceTimersByTime(200));
    expect(screen.queryByRole("menuitem", { name: "Guide" })).toBeNull();
    fireEvent.mouseMove(help);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("menuitem", { name: "Guide" })).toBeTruthy();
    expect(document.activeElement).toBe(help);
  });

  it("cancels pending and open children when their actual parent becomes disabled", () => {
    vi.useFakeTimers();
    const view = render(<ContextMenu position={{ x: 20, y: 20 }} onClose={vi.fn()} items={[{ label: "Open", submenu: [{ label: "Editor" }] }]} />);
    const parent = screen.getByRole("menuitem", { name: "Open" });
    fireEvent.mouseEnter(parent);
    view.rerender(<ContextMenu position={{ x: 20, y: 20 }} onClose={vi.fn()} items={[{ label: "Open", disabled: true, submenu: [{ label: "Editor" }] }]} />);
    act(() => vi.advanceTimersByTime(200));
    expect(screen.queryByRole("menuitem", { name: "Editor" })).toBeNull();
    fireEvent.mouseMove(parent); fireEvent.click(parent);
    expect(screen.queryByRole("menuitem", { name: "Editor" })).toBeNull();
    view.rerender(<ContextMenu position={{ x: 20, y: 20 }} onClose={vi.fn()} items={[{ label: "Open", submenu: [{ label: "Editor" }] }]} />);
    parent.focus(); fireEvent.keyDown(parent, { key: "ArrowRight" });
    expect(screen.getByRole("menuitem", { name: "Editor" })).toBeTruthy();
    view.rerender(<ContextMenu position={{ x: 20, y: 20 }} onClose={vi.fn()} items={[{ label: "Open", disabled: true, submenu: [{ label: "Editor" }] }]} />);
    expect(screen.queryByRole("menuitem", { name: "Editor" })).toBeNull();
  });

  it.each(["right", "left"] as const)("keeps a diagonal path to a %s flyout from being intercepted by its sibling", side => {
    vi.useFakeTimers();
    menuLayout(180, 126);
    const left = side === "right" ? 100 : window.innerWidth - 224;
    const childLeft = side === "right" ? left + 184 : left - 184;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      if (this.getAttribute("role") === "menu") {
        const x = this.getAttribute("aria-label") === "Open" ? childLeft : left;
        return { left: x, right: x + 180, top: 94, bottom: 220, width: 180, height: 126 } as DOMRect;
      }
      return { left: left + 7, right: left + 173, top: 100, bottom: 134, width: 166, height: 34 } as DOMRect;
    });
    render(<ContextMenu position={{ x: left, y: 50 }} onClose={vi.fn()} items={[
      { label: "Open", submenu: [{ label: "Editor" }] }, { label: "Sibling" },
    ]} />);
    const parent = screen.getByRole("menuitem", { name: "Open" });
    const sibling = screen.getByRole("menuitem", { name: "Sibling" });
    fireEvent.click(parent);
    const child = screen.getByRole("menu", { name: "Open" });
    expect(child.dataset.side).toBe(side);
    const leaveX = side === "right" ? left + 150 : left + 25;
    const siblingX = leaveX + (side === "right" ? 10 : -10);
    fireEvent.mouseMove(parent, { clientX: leaveX + (side === "right" ? -20 : 20), clientY: 120 });
    fireEvent.mouseMove(parent, { clientX: leaveX, clientY: 132 });
    fireEvent.mouseLeave(parent, { clientX: leaveX, clientY: 132 });
    fireEvent.mouseEnter(sibling, { clientX: siblingX, clientY: 145 });
    expect(screen.getByRole("menuitem", { name: "Editor" })).toBeTruthy();
    expect(document.activeElement).toBe(parent);
    act(() => vi.advanceTimersByTime(300));
    fireEvent.mouseMove(sibling, { clientX: siblingX, clientY: 145 });
    expect(screen.queryByRole("menuitem", { name: "Editor" })).toBeNull();
    expect(document.activeElement).toBe(sibling);
    fireEvent.keyDown(sibling, { key: "ArrowUp" });
    expect(document.activeElement).toBe(parent);
  });

  it("bottom-aligns Help using layout dimensions rather than its transformed paint bounds", () => {
    menuLayout(180, 126);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      if (this.getAttribute("role") === "menu") return { left: 100, right: 278.2, top: 100, bottom: 224.74, width: 178.2, height: 124.74 } as DOMRect;
      return { left: 107, right: 273, top: 330, bottom: 364, width: 166, height: 34 } as DOMRect;
    });
    render(<ContextMenu position={{ x: 100, y: 100 }} onClose={vi.fn()} items={[
      { label: "Help", alignToParentBottom: true, submenu: [{ label: "Guide" }] },
    ]} />);
    fireEvent.click(screen.getByRole("menuitem", { name: "Help" }));
    const child = screen.getByRole("menu", { name: "Help" });
    expect(child.style.left).toBe("277px");
    expect(Number.parseFloat(child.style.top) + 126).toBe(368);
  });

  it("anchors the profile to the rail's actual end edge and keeps sizing local to the root", () => {
    menuLayout(228, 100);
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue({ left: 16, right: 48, top: 600, bottom: 632, width: 32, height: 32 } as DOMRect);
    render(<ContextMenu position={{ x: 0, y: 0 }} anchor={trigger} side="right" align="end" alignOffset={-4}
      style={{ width: 228 }} onClose={vi.fn()} items={[{ label: "Help", submenuClassName: "opaque-help", submenu: [{ label: "Guide" }] }]} />);
    const root = screen.getByRole("menu");
    expect(root.style.left).toBe("52px");
    expect(root.style.top).toBe("536px");
    fireEvent.click(screen.getByRole("menuitem", { name: "Help" }));
    const child = screen.getByRole("menu", { name: "Help" });
    expect(child.classList.contains("opaque-help")).toBe(true);
    expect(child.style.width).toBe("");
    trigger.remove();
  });

  it("closes on Escape without sending it to the global interrupt handler", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const close = vi.fn();
    const interrupt = vi.fn();
    window.addEventListener("keydown", interrupt);
    try {
      render(<ContextMenu position={{ x: 0, y: 0 }} onClose={close} items={[{ label: "Open" }]} />);
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
      expect(close).toHaveBeenCalledOnce();
      expect(interrupt).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(trigger);
    } finally {
      window.removeEventListener("keydown", interrupt);
      trigger.remove();
    }
  });
  it("restores the trigger after a keyboard action and allows the action's own focus target to take precedence", () => {
    const trigger = document.createElement("button");
    const target = document.createElement("input");
    document.body.append(trigger, target);
    const close = vi.fn();
    trigger.focus();
    const view = render(<ContextMenu position={{ x: 0, y: 0 }} onClose={close} items={[{ label: "Copy" }, { label: "Open", onClick: () => target.focus() }]} />);
    screen.getByRole("menuitem", { name: "Copy" }).focus();
    fireEvent.click(screen.getByRole("menuitem", { name: "Copy" }));
    expect(document.activeElement).toBe(trigger);
    screen.getByRole("menuitem", { name: "Open" }).focus();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    expect(document.activeElement).toBe(target);
    expect(close).toHaveBeenCalledTimes(2);
    view.unmount(); trigger.remove(); target.remove();
  });
  it("keeps a menu on screen after a negative launcher coordinate and a viewport resize", () => {
    menuLayout(200, 180);
    const box = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 200, height: 180 } as DOMRect);
    const view = render(<ContextMenu position={{ x: -50, y: window.innerHeight - 4 }} onClose={vi.fn()} items={[{ label: "Open" }]} />);
    const menu = screen.getByRole("menu");
    expect(menu.style.left).toBe("8px");
    expect(Number.parseFloat(menu.style.top) + 180).toBeLessThanOrEqual(window.innerHeight - 8);
    const previousHeight = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 300 });
    fireEvent(window, new Event("resize"));
    expect(Number.parseFloat(menu.style.top) + 180).toBeLessThanOrEqual(292);
    Object.defineProperty(window, "innerHeight", { configurable: true, value: previousHeight });
    view.unmount(); box.mockRestore();
  });
  it("closes when a new overlay takes focus without moving that focus back to the old trigger", async () => {
    const trigger = document.createElement("button");
    const nextInput = document.createElement("input");
    document.body.append(trigger, nextInput); trigger.focus();
    const close = vi.fn();
    const view = render(<ContextMenu position={{ x: 20, y: 20 }} onClose={close} items={[{ label: "Open" }]} />);
    await act(async () => new Promise<void>((resolve) => window.setTimeout(resolve, 0)));
    act(() => nextInput.focus());
    expect(close).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(nextInput);
    view.unmount(); trigger.remove(); nextInput.remove();
  });
  it("keeps both menu levels anchored while the underlying pane scrolls without closing or stealing focus", async () => {
    menuLayout(180, 100);
    const pane = document.createElement("div");
    const trigger = document.createElement("button");
    pane.appendChild(trigger);
    document.body.appendChild(pane);
    let triggerTop = 100;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      if (this === trigger) return { left: 150, right: 250, top: triggerTop, bottom: triggerTop + 34, width: 100, height: 34 } as DOMRect;
      if (this.getAttribute("role") === "menu") {
        const left = Number.parseFloat(this.style.left), top = Number.parseFloat(this.style.top);
        return { left, right: left + 180, top, bottom: top + 100, width: 180, height: 100 } as DOMRect;
      }
      const parent = this.closest<HTMLElement>('[role="menu"]')!;
      const left = Number.parseFloat(parent.style.left) + 4, top = Number.parseFloat(parent.style.top) + 4;
      return { left, right: left + 172, top, bottom: top + 34, width: 172, height: 34 } as DOMRect;
    });
    const close = vi.fn();
    const view = render(<ContextMenu position={{ x: 20, y: 20 }} anchor={trigger} onClose={close} items={[{ label: "Open", submenu: [{ label: "Editor" }] }]} />);
    await act(async () => new Promise<void>((resolve) => window.setTimeout(resolve, 0)));
    const root = screen.getByRole("menu");
    expect(root.style.top).toBe("140px");
    fireEvent.click(screen.getByRole("menuitem", { name: "Open" }));
    const child = screen.getByRole("menu", { name: "Open" });
    expect(child.style.top).toBe("140px");
    const focused = document.activeElement;
    fireEvent.scroll(child);
    expect(close).not.toHaveBeenCalled();
    triggerTop = 40;
    fireEvent.scroll(pane);
    expect(close).not.toHaveBeenCalled();
    expect(root.style.top).toBe("80px");
    expect(child.style.top).toBe("80px");
    expect(document.activeElement).toBe(focused);
    view.unmount(); pane.remove();
  });
});

// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContextMenu } from "./ContextMenu";

afterEach(cleanup);

describe("ContextMenu keyboard navigation", () => {
  it("wraps through enabled items around separators and disabled actions", () => {
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
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "First" }));
    key("End");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Last" }));
    key("Home");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "First" }));
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
  it("allows its own long menu to scroll but closes when the underlying pane scrolls", async () => {
    const pane = document.createElement("div");
    document.body.appendChild(pane);
    const close = vi.fn();
    const view = render(<ContextMenu position={{ x: 20, y: 20 }} onClose={close} items={[{ label: "Open" }]} />);
    await act(async () => new Promise<void>((resolve) => window.setTimeout(resolve, 0)));
    fireEvent.scroll(screen.getByRole("menu"));
    expect(close).not.toHaveBeenCalled();
    fireEvent.scroll(pane);
    expect(close).toHaveBeenCalledOnce();
    view.unmount(); pane.remove();
  });
});

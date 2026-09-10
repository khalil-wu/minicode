// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
});

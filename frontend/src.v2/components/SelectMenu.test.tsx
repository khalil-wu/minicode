// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SelectMenu } from "./SelectMenu";
import { useFocusTrap } from "../hooks/useFocusTrap";

const ControlledSelect = ({ disabled = false }: { disabled?: boolean }) => {
  const [value, setValue] = useState("auto");
  return (
    <SelectMenu ariaLabel="推理强度" value={value} disabled={disabled} onValueChange={setValue}>
      <option value="auto">自动</option>
      <option value="low">低</option>
      <option value="high">高</option>
    </SelectMenu>
  );
};

describe("SelectMenu", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("portals out of clipping parents, bounds the menu to the viewport, and retains model typography", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 950, right: 1150, top: 100, bottom: 138, width: 200, height: 38 } as DOMRect);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(600);
    const { container } = render(<div style={{ overflow: "hidden" }}><SelectMenu ariaLabel="Model" value="one" className="mc-select-menu-mono" onValueChange={vi.fn()}><option value="one">One</option></SelectMenu></div>);
    fireEvent.click(screen.getByRole("button", { name: "Model，当前：One" }));
    const menu = screen.getByRole("listbox");
    expect(menu.parentElement).toBe(document.body);
    expect(container.contains(menu)).toBe(false);
    expect(menu.classList.contains("mc-select-menu-mono")).toBe(true);
    expect(Number.parseFloat(menu.style.left) + Number.parseFloat(menu.style.width)).toBeLessThanOrEqual(window.innerWidth - 8);
    expect(Number.parseFloat(menu.style.top) + Number.parseFloat(menu.style.maxHeight)).toBeLessThanOrEqual(window.innerHeight - 8);
    fireEvent.scroll(menu);
    expect(screen.queryByRole("listbox")).toBe(menu);
    fireEvent.scroll(container.firstElementChild!);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("allows Escape when all options are disabled", () => {
    render(<SelectMenu ariaLabel="Model" value="old" onValueChange={vi.fn()}><option value="old" disabled>Old</option></SelectMenu>);
    const trigger = screen.getByRole("button", { name: "Model，当前：Old" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it.each([false, true])("returns portal option focus before a dialog handles Tab (reverse=%s)", async (reverse) => {
    function Dialog() {
      const ref = useFocusTrap(true);
      return <div role="dialog" ref={ref} tabIndex={-1}><button>Other</button><ControlledSelect /></div>;
    }
    render(<Dialog />);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Other" })));
    const trigger = screen.getByRole("button", { name: "推理强度，当前：自动" });
    trigger.focus();
    fireEvent.click(trigger);
    const option = screen.getByRole("option", { name: "自动" });
    await waitFor(() => expect(document.activeElement).toBe(option));
    const propagate = fireEvent.keyDown(option, { key: "Tab", shiftKey: reverse });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(reverse ? trigger : screen.getByRole("button", { name: "Other" }));
    expect(propagate).toBe(reverse);
  });

  it("opens a themed menu and updates the controlled value", () => {
    const { container } = render(<ControlledSelect />);

    fireEvent.click(screen.getByRole("button", { name: "推理强度，当前：自动" }));
    fireEvent.click(screen.getByRole("option", { name: "高" }));

    expect(screen.getByRole("button", { name: "推理强度，当前：高" })).toBeTruthy();
    expect((container.querySelector("select") as HTMLSelectElement).value).toBe("high");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("does not open while disabled", () => {
    render(<ControlledSelect disabled />);

    const trigger = screen.getByRole("button", { name: "推理强度，当前：自动" });
    expect((trigger as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes with Escape and restores focus to the trigger", async () => {
    render(<ControlledSelect />);
    const trigger = screen.getByRole("button", { name: "推理强度，当前：自动" });

    fireEvent.click(trigger);
    const selected = screen.getByRole("option", { name: "自动" });
    fireEvent.keyDown(selected, { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("skips a disabled selected group and keeps Escape inside the select", async () => {
    const parentKey = vi.fn();
    render(<div onKeyDown={parentKey}>
      <SelectMenu ariaLabel="Model" value="old" onValueChange={vi.fn()}>
        <optgroup label="Unavailable" disabled><option value="old">Old</option></optgroup>
        <option value="new">New</option>
      </SelectMenu>
    </div>);
    fireEvent.click(screen.getByRole("button", { name: "Model，当前：Old" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "New" })));
    expect((screen.getByRole("option", { name: "Old" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(parentKey).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes when focus leaves or the select becomes disabled", async () => {
    const view = render(<><ControlledSelect /><button>Next</button></>);
    fireEvent.click(screen.getByRole("button", { name: "推理强度，当前：自动" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "自动" })));
    fireEvent.blur(document.activeElement!, { relatedTarget: screen.getByRole("button", { name: "Next" }) });
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "推理强度，当前：自动" }));
    view.rerender(<><ControlledSelect disabled /><button>Next</button></>);
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

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
    expect(Number.parseFloat(menu.style.left) + Number.parseFloat(menu.style.minWidth)).toBeLessThanOrEqual(window.innerWidth - 8);
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

  it("anchors an intrinsically wider option menu at the trigger end instead of the selected label width", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      return this.classList.contains("mc-select-popover")
        ? { left: 0, right: 380, top: 0, bottom: 80, width: 380, height: 80 } as DOMRect
        : { left: 600, right: 720, top: 100, bottom: 132, width: 120, height: 32 } as DOMRect;
    });
    render(<SelectMenu ariaLabel="实际选项" align="end" value="short" onValueChange={vi.fn()}>
      <option value="short">Short</option><option value="long">完整的已安装应用和真实版本名称</option>
    </SelectMenu>);
    fireEvent.click(screen.getByRole("button", { name: "实际选项，当前：Short" }));
    const menu = screen.getByRole("listbox");
    expect(menu.style.left).toBe("340px");
    expect(menu.style.minWidth).toBe("220px");
    expect(screen.getByRole("option", { name: "完整的已安装应用和真实版本名称" })).toBeTruthy();
  });

  it("uses the same actual option icon in the selected trigger and menu without changing the option value", async () => {
    const change = vi.fn();
    render(<SelectMenu ariaLabel="文件打开位置" value="vscode" onValueChange={change}
      renderOptionIcon={(value) => <img src={`apps/${value}.svg`} alt="" />}>
      <option value="vscode">VS Code</option><option value="visualStudio">Visual Studio</option>
    </SelectMenu>);
    const trigger = screen.getByRole("button", { name: "文件打开位置，当前：VS Code" });
    expect(trigger.querySelector("img")?.getAttribute("src")).toBe("apps/vscode.svg");
    fireEvent.click(trigger);
    const selected = screen.getByRole("option", { name: "VS Code" });
    expect(selected.querySelector("img")?.getAttribute("src")).toBe("apps/vscode.svg");
    expect(selected.getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("option", { name: "Visual Studio" }));
    expect(change).toHaveBeenCalledExactlyOnceWith("visualStudio");
    await waitFor(() => expect(document.activeElement).toBe(trigger));
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

  it("typeaheads through actual enabled labels, cycles repeated letters and uses the hovered option for keyboard navigation", async () => {
    const change = vi.fn();
    render(<SelectMenu ariaLabel="Open target" value="alpha" onValueChange={change}>
      <option value="alpha">Alpha</option><option value="archive">Archive</option>
      <optgroup label="Unavailable" disabled><option value="disabled-beta">Beta unavailable</option></optgroup>
      <option value="beta">Beta</option><option value="bravo">Bravo</option>
    </SelectMenu>);
    fireEvent.click(screen.getByRole("button", { name: "Open target，当前：Alpha" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "Alpha" })));
    fireEvent.keyDown(document.activeElement!, { key: "b" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Beta", exact: true }));
    fireEvent.keyDown(document.activeElement!, { key: "b" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Bravo" }));
    fireEvent.mouseMove(screen.getByRole("option", { name: "Archive" }));
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Archive" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Beta", exact: true }));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(change).not.toHaveBeenCalled();
  });

  it("matches a multiword label incrementally without selecting on its space", async () => {
    const change = vi.fn();
    render(<SelectMenu ariaLabel="Target" value="file" onValueChange={change}>
      <option value="file">File Explorer</option><option value="visual-code">Visual Code</option><option value="visual-studio">Visual Studio</option>
    </SelectMenu>);
    fireEvent.click(screen.getByRole("button", { name: "Target，当前：File Explorer" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "File Explorer" })));
    for (const key of "visual s") fireEvent.keyDown(document.activeElement!, { key });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Visual Studio" }));
    expect(change).not.toHaveBeenCalled();
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });
    expect(change).toHaveBeenCalledExactlyOnceWith("visual-studio");
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

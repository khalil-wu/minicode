/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFocusTrap } from "./useFocusTrap";
import { createRef, type ReactNode, type RefObject } from "react";

const Harness = ({ active }: { active: boolean }) => {
  const ref = useFocusTrap(active);
  return (
    <div ref={ref} tabIndex={-1}>
      <button type="button">First</button>
      <button type="button">Last</button>
    </div>
  );
};

describe("useFocusTrap", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("focuses the first control, wraps Tab, and restores the trigger", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 0;
    });
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();

    const view = render(<Harness active />);

    const [first, last] = within(view.container).getAllByRole("button");
    expect(document.activeElement).toBe(first);

    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    view.rerender(<Harness active={false} />);
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it("excludes hidden controls and negative tab indexes from the tab order", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
    const Controls = () => {
      const ref = useFocusTrap(true);
      return <div ref={ref} tabIndex={-1}>
        <select aria-hidden="true" tabIndex={-1}><option>Hidden proxy</option></select>
        <div hidden><button>Hidden ancestor</button></div>
        <button tabIndex={-1}>Roving item</button>
        <button>Visible first</button><button>Visible last</button>
      </div>;
    };
    const view = render(<Controls />);
    const first = within(view.container).getByText("Visible first");
    const last = within(view.container).getByText("Visible last");
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("keeps scroll locked until the last overlapping dialog closes", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
    const Trap = ({ children }: { children: ReactNode }) => {
      const ref = useFocusTrap(true);
      return <div ref={ref} tabIndex={-1}>{children}</div>;
    };
    const outer = render(<Trap><button>Outer</button></Trap>);
    const inner = render(<Trap><button>Inner first</button><button>Inner last</button></Trap>);
    const buttons = within(inner.container).getAllByRole("button");
    buttons[1].focus();
    fireEvent.keyDown(buttons[1], { key: "Tab" });
    expect(document.activeElement).toBe(buttons[0]);
    outer.unmount();
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.activeElement).toBe(buttons[0]);
    inner.unmount();
    expect(document.body.style.overflow).toBe("");
  });

  it("preserves a new navigation focus when its old dialog deactivates", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
    const view = render(<><button>Next settings page</button><Harness active /></>);
    const destination = within(view.container).getByText("Next settings page");
    destination.focus();
    view.rerender(<><button>Next settings page</button><Harness active={false} /></>);
    expect(document.activeElement).toBe(destination);
    expect(document.body.style.overflow).toBe("");
  });

  it("restores the remaining dialog when a nested trigger was removed", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
    const Trap = ({ children }: { children: ReactNode }) => {
      const ref = useFocusTrap(true);
      return <div ref={ref} tabIndex={-1}>{children}</div>;
    };
    const outer = render(<Trap><button>Remaining control</button><button>Nested trigger</button></Trap>);
    const trigger = within(outer.container).getByText("Nested trigger");
    trigger.focus();
    const inner = render(<Trap><button>Inner control</button></Trap>);
    outer.rerender(<Trap><button>Remaining control</button></Trap>);
    inner.unmount();
    expect(document.activeElement).toBe(within(outer.container).getByText("Remaining control"));
    expect(document.body.style.overflow).toBe("hidden");
    outer.unmount();
  });

  it("restores the explicit parent trigger after a nested autofocus input captured previous focus", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 0; });
    const trigger = createRef<HTMLButtonElement>();
    const ParentTrap = () => {
      const ref = useFocusTrap(true);
      return <div ref={ref} tabIndex={-1}><button>Return to app</button><button ref={trigger}>Import theme</button></div>;
    };
    const ChildTrap = ({ returnTo }: { returnTo: RefObject<HTMLButtonElement> }) => {
      const ref = useFocusTrap(true, returnTo);
      return <div ref={ref} tabIndex={-1}><input aria-label="Theme share string" autoFocus /><button>Import</button></div>;
    };
    const parent = render(<ParentTrap />);
    const firstParentControl = within(parent.container).getByText("Return to app");
    expect(document.activeElement).toBe(firstParentControl);
    trigger.current!.focus();
    const child = render(<ChildTrap returnTo={trigger} />);
    expect(document.activeElement).toBe(within(child.container).getByRole("textbox"));
    child.unmount();
    expect(document.activeElement).toBe(trigger.current);
    expect(document.body.style.overflow).toBe("hidden");
    parent.unmount();
    expect(document.body.style.overflow).toBe("");
  });
});

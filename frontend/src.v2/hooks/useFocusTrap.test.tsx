/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFocusTrap } from "./useFocusTrap";

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
});

/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RollingNumber } from "./RollingNumber";

describe("RollingNumber", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("rolls when the value changes and clears the previous value after animation", () => {
    vi.useFakeTimers();
    const { container, rerender } = render(<RollingNumber value={12} prefix="+" />);

    expect(screen.getByLabelText("+12")).toBeTruthy();
    expect(container.querySelector(".rolling-number")?.getAttribute("data-animating")).toBe("false");

    rerender(<RollingNumber value={18} prefix="+" />);

    const rolling = container.querySelector(".rolling-number");
    expect(rolling?.getAttribute("data-animating")).toBe("true");
    expect(rolling?.getAttribute("data-direction")).toBe("up");
    expect(screen.getByLabelText("+18")).toBeTruthy();

    const oldValue = container.querySelector(".rolling-number-old");
    expect(oldValue?.textContent).toBe("+12");

    act(() => {
      vi.advanceTimersByTime(240);
    });

    expect(container.querySelector(".rolling-number")?.getAttribute("data-animating")).toBe("false");
    expect(container.querySelector(".rolling-number-old")).toBeNull();
  });

  it("can roll in from zero on mount for newly surfaced live stats", () => {
    vi.useFakeTimers();
    const { container } = render(<RollingNumber value={116} prefix="+" animateOnMount />);

    const rolling = container.querySelector(".rolling-number");
    expect(rolling?.getAttribute("data-animating")).toBe("true");
    expect(rolling?.getAttribute("data-direction")).toBe("up");
    expect(screen.getByLabelText("+116")).toBeTruthy();
    expect(container.querySelector(".rolling-number-old")?.textContent).toBe("0");

    act(() => {
      vi.advanceTimersByTime(240);
    });

    expect(container.querySelector(".rolling-number")?.getAttribute("data-animating")).toBe("false");
  });
});

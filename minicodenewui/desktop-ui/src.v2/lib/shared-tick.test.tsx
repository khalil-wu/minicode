/* @vitest-environment jsdom */

import { act, cleanup, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useSharedSecondTick } from "./shared-tick";

describe("shared-tick (useSharedSecondTick)", () => {
  it("updates now each second while active and freezes when inactive", () => {
    vi.useFakeTimers();
    const captured: number[] = [];
    function Probe({ active }: { active: boolean }) {
      const now = useSharedSecondTick(active);
      captured.push(now);
      return null;
    }
    const { rerender } = render(<Probe active={true} />);
    const initial = captured[captured.length - 1];

    // Wrap timer advances in act so the setState from the shared tick flushes.
    act(() => {
      vi.advanceTimersByTime(1500);
    });
    const afterTick = captured[captured.length - 1];
    expect(afterTick).toBeGreaterThan(initial);

    // Deactivate — the shared tick unsubscribes; advancing time must NOT push.
    rerender(<Probe active={false} />);
    const beforeInactive = captured.length;
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(captured.length).toBe(beforeInactive);

    cleanup();
    vi.useRealTimers();
  });
});

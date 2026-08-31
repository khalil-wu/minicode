import { describe, expect, it, vi } from "vitest";
import { subscribeSecondTick } from "./shared-tick";

describe("shared-tick (subscribeSecondTick)", () => {
  it("delivers the same 1s pulse to every subscriber and stops when empty", () => {
    vi.useFakeTimers();
    const a = vi.fn();
    const b = vi.fn();
    const unsubA = subscribeSecondTick(a);
    const unsubB = subscribeSecondTick(b);
    vi.advanceTimersByTime(1000);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    unsubA();
    vi.advanceTimersByTime(1000);
    // Only the remaining subscriber gets the second pulse.
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
    unsubB();
    vi.useRealTimers();
  });
});

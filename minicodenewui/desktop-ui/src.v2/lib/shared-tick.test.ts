/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSharedSecondTick } from "./shared-tick";

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("shared second clock", () => {
  it("updates active readers together and stops after the last reader leaves", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1000);
    const a = renderHook(() => useSharedSecondTick(true));
    const b = renderHook(() => useSharedSecondTick(true));
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1000));
    expect(a.result.current).toBe(2000);
    expect(b.result.current).toBe(2000);
    a.unmount();
    act(() => vi.advanceTimersByTime(1000));
    expect(b.result.current).toBe(3000);
    expect(vi.getTimerCount()).toBe(1);
    b.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});

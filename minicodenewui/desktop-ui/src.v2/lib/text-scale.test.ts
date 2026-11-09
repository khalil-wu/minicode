import { describe, expect, it } from "vitest";
import * as fc from "fast-check";
import { TEXT_SCALE_MAX, TEXT_SCALE_MIN, clampTextScale } from "./text-scale";

describe("clampTextScale", () => {
  it("clamps any finite number into [MIN, MAX]", () => {
    fc.assert(
      fc.property(fc.double({ noNaN: true, noDefaultInfinity: true }), (x) => {
        const v = clampTextScale(x);
        return v >= TEXT_SCALE_MIN && v <= TEXT_SCALE_MAX;
      }),
    );
  });

  it("treats NaN and ±Infinity as TEXT_SCALE_MIN", () => {
    expect(clampTextScale(Number.NaN)).toBe(TEXT_SCALE_MIN);
    expect(clampTextScale(Number.POSITIVE_INFINITY)).toBe(TEXT_SCALE_MIN);
    expect(clampTextScale(Number.NEGATIVE_INFINITY)).toBe(TEXT_SCALE_MIN);
  });

  it("is idempotent: clamp(clamp(x)) === clamp(x)", () => {
    fc.assert(
      fc.property(fc.double({ noNaN: true, noDefaultInfinity: true }), (x) => {
        const once = clampTextScale(x);
        const twice = clampTextScale(once);
        return once === twice;
      }),
    );
  });

  it("is identity on the canonical 1.0", () => {
    expect(clampTextScale(1.0)).toBe(1.0);
  });
});

import { describe, expect, it } from "vitest";
import { safeJsonParse } from "./safe-parse";

describe("safe JSON parsing", () => {
  it("returns the fallback for malformed persisted data", () => {
    expect(safeJsonParse("{malformed", { ok: false })).toEqual({ ok: false });
  });

  it("preserves valid JSON values, including null", () => {
    expect(safeJsonParse("[1,2,3]", [])).toEqual([1, 2, 3]);
    expect(safeJsonParse("null", "fallback")).toBeNull();
  });

});

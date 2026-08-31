import { describe, expect, it } from "vitest";
import { safeJsonParse, safeJsonParseWithValidation } from "./safe-parse";

describe("safe JSON parsing", () => {
  it("returns the fallback for malformed persisted data", () => {
    expect(safeJsonParse("{malformed", { ok: false })).toEqual({ ok: false });
  });

  it("preserves valid JSON values, including null", () => {
    expect(safeJsonParse("[1,2,3]", [])).toEqual([1, 2, 3]);
    expect(safeJsonParse("null", "fallback")).toBeNull();
  });

  it("requires the caller's shape validator before accepting data", () => {
    const isStringArray = (value: unknown): value is string[] => (
      Array.isArray(value) && value.every((item) => typeof item === "string")
    );

    expect(safeJsonParseWithValidation("[\"a\",\"b\"]", isStringArray, [])).toEqual(["a", "b"]);
    expect(safeJsonParseWithValidation("[1]", isStringArray, ["fallback"])).toEqual(["fallback"]);
  });
});

import { describe, expect, it } from "vitest";
import { initialDiffReviewPatch } from "./diffReviewState";

describe("initialDiffReviewPatch", () => {
  it("uses the selected file patch instead of joining every patch", () => {
    const files = [
      { path: "a.ts", patch: "patch-a" },
      { path: "b.ts", patch: "patch-b" },
    ];

    expect(initialDiffReviewPatch(files, "b.ts")).toBe("patch-b");
  });

  it("falls back to the first available patch", () => {
    const files = [
      { path: "a.ts", patch: "" },
      { path: "b.ts", patch: "patch-b" },
    ];

    expect(initialDiffReviewPatch(files, "a.ts")).toBe("patch-b");
  });
});

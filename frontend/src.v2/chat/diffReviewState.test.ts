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

  it("keeps an empty selected-file patch instead of showing another file", () => {
    const files = [
      { path: "a.ts", patch: "" },
      { path: "b.ts", patch: "patch-b" },
    ];

    expect(initialDiffReviewPatch(files, "a.ts")).toBe("");
  });
});

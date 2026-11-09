import { expect, it } from "vitest";
import { reviewNeedsSingleFile } from "./review-file-layout";

it("uses Source's strict global file, changed-line and byte boundaries independently", () => {
  expect(reviewNeedsSingleFile(128, 9000, 12 * 1024 * 1024)).toBe(false);
  expect(reviewNeedsSingleFile(129, 0, 0)).toBe(true);
  expect(reviewNeedsSingleFile(1, 9001, 0)).toBe(true);
  expect(reviewNeedsSingleFile(1, 1, 12 * 1024 * 1024 + 1)).toBe(true);
});

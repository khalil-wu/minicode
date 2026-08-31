/* @vitest-environment jsdom */

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InlineDiff } from "./InlineDiff";

describe("InlineDiff", () => {
  it("keeps one visible line-number column ordered across concatenated patches", () => {
    const { container } = render(
      <InlineDiff
        patch={[
          "diff --git a/file.ts b/file.ts",
          "@@ -10 +10 @@",
          "-old one",
          "+new one",
          "@@ -10 +10 @@",
          "-old two",
          "+new two",
        ].join("\n")}
      />,
    );

    const numbers = [...container.querySelectorAll<HTMLElement>(".inline-diff-number")]
      .map((node) => Number(node.textContent))
      .filter((value) => Number.isFinite(value));
    expect(numbers).toEqual([10, 10, 11, 11]);
    expect(container.querySelectorAll(".inline-diff-line:first-child .inline-diff-number")).toHaveLength(1);
  });
});
